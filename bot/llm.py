"""Second opinion from a free LLM, routed through your self-hosted freellmapi.

The LLM never invents trades. It only gets to APPROVE or VETO a setup the indicators
already found, after reading the chart numbers and recent headlines. That keeps a
flaky free model from doing anything the rules wouldn't allow anyway.
"""
import json
import logging
import re

from openai import APIStatusError, OpenAI, RateLimitError

log = logging.getLogger("llm")

SYSTEM = (
    "You are a cautious intraday trading risk reviewer. A rules-based system has found a setup. "
    "Decide whether to APPROVE or VETO it using only the data given. Veto if headlines suggest "
    "event risk (earnings today, halts, lawsuits, guidance cuts for a long; strong positive catalysts "
    "for a short), if the chart numbers contradict the setup, or if anything is unclear. "
    "Do NOT think out loud or explain your steps. Your entire reply must be one JSON object: "
    '{"decision": "approve"|"veto", "confidence": 0.0-1.0, "reason": "<one sentence>"}'
)


class Reviewer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = OpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key or "none",
                             timeout=45, max_retries=1) if cfg.llm_api_key else None
        self.json_mode = True     # ask for JSON-only replies; switched off if the router rejects it

    def _create(self, model, messages):
        kw = dict(model=model, temperature=0.1, max_tokens=1500, messages=messages)
        if self.json_mode:
            try:
                return self.client.chat.completions.create(response_format={"type": "json_object"}, **kw)
            except APIStatusError as e:          # any refusal except 'slow down' -> stop using JSON mode
                if isinstance(e, RateLimitError):
                    raise
                log.info("JSON mode not supported (%s); asking without it", str(e)[:80])
                self.json_mode = False
        return self.client.chat.completions.create(**kw)

    def review(self, signal, chart: dict, headlines: list[str]) -> dict:
        if self.client is None:
            return self._fallback("no LLM_API_KEY set")
        payload = {
            "setup": signal.to_dict(),
            "chart_5min": chart,
            "recent_headlines": headlines[:8] or ["(none in the last 24h)"],
        }
        models = self.cfg.llm_models or [self.cfg.llm_model]
        last_err = "no models configured"
        for model in models:                      # first readable answer wins
            try:
                resp = self._create(model, [{"role": "system", "content": SYSTEM},
                                            {"role": "user", "content": json.dumps(payload)}])
                text = resp.choices[0].message.content or ""
                out = parse(text)
                log.info("LLM %s on %s %s (%s): %s", out["decision"], signal.side, signal.symbol, model, out["reason"])
                return out
            except Exception as e:  # network, rate limit, unreadable reply -> try the next model
                last_err = str(e)[:120]
                log.warning("LLM %s unusable for %s: %s", model, signal.symbol, last_err)
        return self._fallback(last_err)

    def _fallback(self, why):
        decision = "veto" if self.cfg.llm_required else "approve"
        return {"decision": decision, "confidence": 0.0, "reason": f"LLM skipped ({why})"}


def _clean(text: str) -> str:
    text = re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S | re.I)   # inline reasoning
    return text.replace("```json", "").replace("```", "")


def last_json(t: str, key: str):
    """The LAST complete {...} holding `key` (thinking models write notes before the answer)."""
    for m in reversed(list(re.finditer(r"\{[^{}]*\}", t))):
        try:
            d = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(d, dict) and key in d:
            return d
    try:
        d = json.loads(t.strip())
        return d if isinstance(d, dict) and key in d else None
    except ValueError:
        return None


def last_field(t: str, key: str, values: str):
    """Last `"key": "value"` in the text, ignoring the model echoing the schema ("approve"|"veto")."""
    hits = [m for m in re.finditer(rf'"{key}"\s*:\s*"({values})(?!"\s*\|)', t, re.I)]
    return hits[-1] if hits else None


def parse(text: str) -> dict:
    """Read the AI's verdict, even if the reply is wrapped in code fences, has notes first, or got cut off."""
    t = _clean(text)
    data = last_json(t, "decision")
    if data is None:   # cut-off reply: pull the fields out directly
        d = last_field(t, "decision", "approve|veto")
        if not d:
            raise ValueError(f"no verdict in LLM reply: {t.strip()[:80]!r}")
        c = re.search(r'"confidence"\s*:\s*([0-9.]+)', t)
        r = re.search(r'"reason"\s*:\s*"([^"]*)', t)
        data = {"decision": d.group(1), "confidence": c.group(1) if c else 0,
                "reason": (r.group(1) if r else "") + " (reply was cut off)"}
    decision = str(data.get("decision", "veto")).lower()
    if decision not in ("approve", "veto"):
        decision = "veto"
    try:
        conf = float(data.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return {"decision": decision, "confidence": conf, "reason": str(data.get("reason", ""))[:300]}
