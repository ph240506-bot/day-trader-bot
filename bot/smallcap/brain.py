"""Ask several free models at once; the first valid answer wins (and the rest are dropped)."""
import asyncio
import json
import logging
import re

from openai import AsyncOpenAI

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
    'Reply with JSON only: {"verdict":"strong"|"weak"|"trap","confidence":0.0-1.0,"reason":"<12 words max>"}'
)


def parse(text: str) -> dict:
    t = re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S | re.I).replace("```json", "").replace("```", "")
    m = re.search(r"\{.*\}", t, re.S)
    d = None
    if m:
        try:
            d = json.loads(m.group(0))
        except ValueError:
            d = None
    if d is None:
        v = re.search(r'"verdict"\s*:\s*"(strong|weak|trap)', t, re.I)
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

    async def _ask(self, model: str, payload: str) -> dict:
        r = await self.client.chat.completions.create(
            model=model, temperature=0, max_tokens=400,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": payload}])
        out = parse(r.choices[0].message.content)
        out["model"] = model
        return out

    async def judge(self, symbol: str, text: str, context: dict) -> dict | None:
        """Returns the first valid verdict, or None if nobody answered in time."""
        if not self.client:
            return None
        payload = json.dumps({"symbol": symbol, "text": text, "market": context})
        tasks = [asyncio.create_task(self._ask(m, payload)) for m in self.cfg.llm_models]
        try:
            for fut in asyncio.as_completed(tasks, timeout=self.cfg.llm_timeout_sec):
                try:
                    return await fut
                except Exception as e:
                    log.debug("model failed: %s", e)
        except asyncio.TimeoutError:
            log.info("AI timed out after %.1fs for %s", self.cfg.llm_timeout_sec, symbol)
        finally:
            for t in tasks:
                t.cancel()
        return None
