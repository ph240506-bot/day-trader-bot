"""Spot ETFs / ETNs / leveraged funds from the asset name Alpaca gives us.

Why it matters: a 2x fund on a stock "gaps 30%" whenever the stock gaps 15%. It has no news, no share
count and no opening-range edge of its own (index breakouts are worth nothing after costs), and several
funds on one theme (SOXL + SOXX + NVDX) are one bet placed three times.
"""
import re

_FUND = re.compile(
    r"\bETF\b|\bETN\b|\bETP\b|\b[1-4](\.\d+)?X\b|"
    r"\b(Direxion|ProShares|iShares|SPDR|Vanguard|Invesco|GraniteShares|T-?Rex|Defiance|Tradr|YieldMax|"
    r"Roundhill|WisdomTree|VanEck|KraneShares|Leverage Shares|REX Shares|Global X|First Trust|"
    r"Daily Target|Index Fund|Select Sector|Bitcoin Trust|Ethereum Trust|Gold Trust|Silver Trust)\b|"
    r"\bFund\b",
    re.I)


def looks_like_fund(name: str | None) -> bool:
    return bool(name and _FUND.search(name))
