"""Ask several free models at once; the first valid answer wins (and the rest are dropped)."""
import asyncio
import json
import logging
import re

from openai import APIStatusError, AsyncOpenAI, RateLimitError

from ..llm import last_field, last_json

log = logging.getLogger("brain")

SYSTEM = (
    "You judge whether a news headline or SEC filing is a strong BULLISH catalyst for a small-cap US stock "
    "in the next 30 minutes. strong = concrete, material good news (FDA approval, positive trial data, "
    "buyout/merger at a premium, large contract relative to company size, big earnings beat). "
    "trap = anything that adds shares or signals distress (offerings, ATM, warrants, reverse split, "
    "delisting, going concern, convertible notes), or old/rehashed news. weak = vague PR, minor updates, "
    "conference appearances, 'exploring', 'intends', letters to shareholders. "
    "Text tagged [Scanner: ...] means the stock is already moving now and this is its latest headline; judge "
    "whether that headline explains the move (an hours-old real catalyst can still be strong). No news = weak. "
    "Text tagged [Pre-market news ...] is news from before the open being re-checked now that the stock is "
    "trading: judge the catalyst itself; hours old is fine if it is concrete and material. "
    "Do NOT think out loud or explain your steps. "
    'Reply with one JSON object only: {"verdict":"strong"|"weak"|"trap","confidence":0.0-1.0,"reason":"<12 words max>"}'
)


def parse(text: str) -> dict:
    t = re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S | re.I).replace("```json", "").replace("```", "")
    d = last_json(t, "verdict")
    if d is None:
        v = last_field(t, "verdict", "strong|weak|trap")
        if not v:
            raise ValueError("no verdict")
        c = re.search(r'"confidence"\s*:\s*([0-9.]+)', t)
        r = re.search(r'"reason"\s*:\s*"([^"]*)', t)
        d = {"verdict": v.group(1), "confidence": c.group(1) if c else 0, "reason": r.group(1) if r else ""}
    v = str(d.get("verdict", "")).lower()
    if v not in ("strong", "weak", "trap"):
        raise ValueError(f"bad verdict {v!r}")
    try:
        conf = float(d.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return {"verdict": v, "confidence": conf, "reason": str(d.get("reason", ""))[:120]}


class Brain:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = AsyncOpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key or "none",
                                  timeout=cfg.llm_timeout_sec, max_retries=0) if cfg.llm_api_key else None
        self.json_mode = True
        self.last_fail = ""       # "timeout" or "unreadable" after judge() returns None

    async def _ask(self, model: str, payload: str, timeout: float) -> dict:
        kw = dict(model=model, temperature=0, max_tokens=600, timeout=timeout + 1,   # the race below enforces the real limit
                  messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": payload}])
        r = None
        if self.json_mode:
            try:
                r = await self.client.chat.completions.create(response_format={"type": "json_object"}, **kw)
            except APIStatusError as e:
                if isinstance(e, RateLimitError):
                    raise
                self.json_mode = False
        if r is None:
            r = await self.client.chat.completions.create(**kw)
        out = parse(r.choices[0].message.content)
        out["model"] = model
        return out

    async def judge(self, symbol: str, text: str, context: dict, timeout: float | None = None) -> dict | None:
        """Returns the first valid verdict, or None (self.last_fail says whether it timed out or was unreadable)."""
        self.last_fail = "no AI key"
        if not self.client:
            return None
        timeout = timeout or self.cfg.llm_timeout_sec
        payload = json.dumps({"symbol": symbol, "text": text, "market": context})
        tasks = [asyncio.create_task(self._ask(m, payload, timeout)) for m in self.cfg.llm_models]
        self.last_fail = "unreadable"
        try:
            for fut in asyncio.as_completed(tasks, timeout=timeout):
                try:
                    return await fut
                except asyncio.TimeoutError:      # Python 3.11+ raises the race timeout here
                    raise
                except Exception as e:
                    log.info("AI model failed for %s: %s %s", symbol, type(e).__name__, str(e)[:100])
        except asyncio.TimeoutError:
            self.last_fail = "timeout"
            log.info("AI timed out after %.1fs for %s", timeout, symbol)
        finally:
            for t in tasks:
                t.cancel()
        return None
