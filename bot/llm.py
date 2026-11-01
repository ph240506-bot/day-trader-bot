"""Second opinion from a free LLM, routed through your self-hosted freellmapi.

The LLM never invents trades. It only gets to APPROVE or VETO a setup the indicators
already found, after reading the chart numbers and recent headlines. That keeps a
flaky free model from doing anything the rules wouldn't allow anyway.
"""
import json
import logging
import re

from openai import OpenAI

log = logging.getLogger("llm")

SYSTEM = (
    "You are a cautious intraday trading risk reviewer. A rules-based system has found a setup. "
    "Decide whether to APPROVE or VETO it using only the data given. Veto if headlines suggest "
    "event risk (earnings today, halts, lawsuits, guidance cuts for a long; strong positive catalysts "
    "for a short), if the chart numbers contradict the setup, or if anything is unclear. "
    'Reply with JSON only: {"decision": "approve"|"veto", "confidence": 0.0-1.0, "reason": "<one sentence>"}'
)


class Reviewer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = OpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key or "none",
                             timeout=45, max_retries=1) if cfg.llm_api_key else None

    def review(self, signal, chart: dict, headlines: list[str]) -> dict:
        if self.client is None:
            return self._fallback("no LLM_API_KEY set")
        payload = {
            "setup": signal.to_dict(),
            "chart_5min": chart,
            "recent_headlines": headlines[:8] or ["(none in the last 24h)"],
        }
        try:
            resp = self.client.chat.completions.create(
                model=self.cfg.llm_model,
                temperature=0.1,
                max_tokens=200,
                messages=[{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": json.dumps(payload)}],
            )
            text = resp.choices[0].message.content or ""
            out = parse(text)
            log.info("LLM %s on %s %s: %s", out["decision"], signal.side, signal.symbol, out["reason"])
            return out
        except Exception as e:  # network, rate limit, bad JSON
            log.warning("LLM unavailable: %s", e)
            return self._fallback(str(e)[:120])

    def _fallback(self, why):
        decision = "veto" if self.cfg.llm_required else "approve"
        return {"decision": decision, "confidence": 0.0, "reason": f"LLM skipped ({why})"}


def parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in LLM reply: {text[:80]!r}")
    data = json.loads(m.group(0))
    decision = str(data.get("decision", "veto")).lower()
    if decision not in ("approve", "veto"):
        decision = "veto"
    return {"decision": decision,
            "confidence": float(data.get("confidence", 0) or 0),
            "reason": str(data.get("reason", ""))[:300]}
