"""Instant headline triage (microseconds) before the slower AI check.

Traps are things that usually sink a small cap: the company selling more shares
(offerings, ATM programs, warrants), reverse splits, delisting notices.
"""
import re

TRAPS = [
    r"\b(public|registered|direct|underwritten|best[- ]efforts|secondary|follow[- ]on)\s+offering",
    r"\bpric(es|ed|ing)\b.{0,40}\boffering", r"\bat[- ]the[- ]market\b", r"\bATM (program|facility|offering)",
    r"\bwarrants?\b.{0,30}\b(exercise|inducement|offering)", r"\bshelf registration\b", r"\bdilut",
    r"\breverse (stock )?split\b", r"\bdelist", r"\bnon[- ]?compliance\b", r"\bgoing concern\b",
    r"\bbankrupt", r"\bchapter 11\b", r"\bconvertible (note|debenture)", r"\bequity line\b",
    r"\bclass action\b", r"\bsubpoena\b", r"\bhalts? (enrollment|trial)\b", r"\bfail(s|ed)? to meet\b",
    r"\bmisses?\b.{0,20}\b(estimates|expectations)", r"\bguidance cut\b", r"\blowers? guidance\b",
]
STRONG = [
    r"\bFDA (approv|clear|grant)", r"\bapproval\b", r"\bbreakthrough (therapy|device) designation",
    r"\bfast track\b", r"\bpositive (topline|top-line|phase|results|data)", r"\bmet (its )?primary endpoint",
    r"\b(definitive )?(merger|acquisition) agreement\b", r"\bto be acquired\b", r"\bacquired by\b",
    r"\btender offer\b", r"\b(awarded|wins?|secures?|signs?)\b.{0,40}\b(contract|order|deal|agreement)",
    r"\bpartnership\b", r"\bstrategic (alliance|collaboration)\b", r"\blicens(e|ing) agreement\b",
    r"\b(record|surging) (revenue|sales)\b", r"\bbeats?\b.{0,20}\b(estimates|expectations)",
    r"\braises? (guidance|outlook)\b", r"\bshare (buyback|repurchase)\b", r"\buplist",
    r"\bpatent (granted|issued|award)",
]
_TRAPS = [re.compile(p, re.I) for p in TRAPS]
_STRONG = [re.compile(p, re.I) for p in STRONG]

# SEC form types: what each usually means for a small cap
TRAP_FORMS = {"S-1", "S-1/A", "S-3", "S-3/A", "F-1", "F-3", "424B1", "424B3", "424B4", "424B5", "424B7", "EFFECT"}
CATALYST_FORMS = {"8-K", "6-K"}


def triage(text: str) -> tuple[str, str]:
    """Returns (verdict, matched phrase). verdict: trap | strong | neutral. Traps win ties."""
    for rx in _TRAPS:
        m = rx.search(text)
        if m:
            return "trap", m.group(0)
    for rx in _STRONG:
        m = rx.search(text)
        if m:
            return "strong", m.group(0)
    return "neutral", ""


def triage_form(form: str) -> str:
    form = form.upper().strip()
    if form in TRAP_FORMS:
        return "trap"
    if form in CATALYST_FORMS:
        return "neutral"   # an 8-K can be anything; the AI reads the title/items
    return "ignore"
