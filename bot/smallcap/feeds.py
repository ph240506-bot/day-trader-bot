"""Free real-time-ish event sources.

- Alpaca news websocket (Benzinga headlines, free): pushed to us the instant it's published.
- SEC EDGAR latest filings (free): 8-Ks (news) and offering forms (traps), polled every ~15s.
- Nasdaq trade-halt RSS (free): which stocks are halted, polled once a minute (their limit).
"""
import asyncio
import gzip
import json
import logging
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .catalysts import is_common_stock_symbol, triage_form

log = logging.getLogger("feeds")

EDGAR_ATOM = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}"
              "&company=&dateb=&owner=include&start=0&count=40&output=atom")
EDGAR_TICKERS = "https://www.sec.gov/files/company_tickers.json"
HALTS_RSS = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"


@dataclass
class Event:
    symbol: str
    text: str                 # headline or filing title
    source: str               # news | edgar
    at: datetime              # when it was published
    form: str = ""            # SEC form type for edgar events
    received: float = field(default_factory=time.time)
    parked: bool = False      # pre-market news being re-checked at the open

    @property
    def key(self):
        return f"{self.symbol}|{self.source}|{self.text[:120]}"


def http_get(url: str, user_agent: str, timeout=10) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        return gzip.decompress(data) if r.headers.get("Content-Encoding") == "gzip" else data


# ---------------- SEC EDGAR ----------------
def load_cik_map(raw: bytes) -> dict[int, str]:
    data = json.loads(raw)
    return {int(v["cik_str"]): v["ticker"].upper() for v in data.values()}


def parse_edgar_atom(raw: bytes, cik_map: dict[int, str]) -> list[Event]:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(raw)
    out = []
    for entry in root.findall("a:entry", ns):
        title = (entry.findtext("a:title", "", ns) or "").strip()
        cat = entry.find("a:category", ns)
        form = (cat.get("term") if cat is not None else title.split(" - ")[0]).strip()
        m = re.search(r"\((\d{6,10})\)", title)
        if not m or triage_form(form) == "ignore":
            continue
        sym = cik_map.get(int(m.group(1)))
        if not sym:
            continue
        updated = entry.findtext("a:updated", "", ns)
        try:
            at = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            at = datetime.now(timezone.utc)
        summary = re.sub(r"<[^>]+>", " ", entry.findtext("a:summary", "", ns) or "")
        out.append(Event(sym, f"SEC {form}: {title}. {' '.join(summary.split())[:300]}", "edgar", at, form))
    return out


async def edgar_poller(queue: asyncio.Queue, cfg, stop: asyncio.Event):
    if not cfg.edgar_user_agent:
        log.info("SEC filings feed off (set SEC_CONTACT_EMAIL to turn it on)")
        return
    try:
        cik_map = load_cik_map(await asyncio.to_thread(http_get, EDGAR_TICKERS, cfg.edgar_user_agent))
    except Exception as e:
        log.warning("EDGAR ticker map unavailable, filings feed off: %s", e)
        return
    seen: set[str] = set()
    first = True
    while not stop.is_set():
        for form in ("8-K", "6-K", "424B5", "S-3", "S-1"):
            try:
                raw = await asyncio.to_thread(http_get, EDGAR_ATOM.format(form=form), cfg.edgar_user_agent)
                for ev in parse_edgar_atom(raw, cik_map):
                    if ev.key in seen:
                        continue
                    seen.add(ev.key)
                    if not first:          # don't replay the backlog on startup
                        await queue.put(ev)
            except Exception as e:
                log.debug("EDGAR %s poll failed: %s", form, e)
            await asyncio.sleep(0.3)       # stay far under SEC's 10 requests/second
        first = False
        if len(seen) > 5000:
            seen = set(list(seen)[-2000:])
        await asyncio.sleep(cfg.edgar_poll_sec)


# ---------------- Nasdaq halts ----------------
def parse_halts(raw: bytes) -> set[str]:
    """Symbols currently halted (no resumption trade time yet)."""
    root = ET.fromstring(raw)
    halted = set()
    for item in root.iter("item"):
        sym, resumed = "", ""
        for child in item:
            tag = child.tag.split("}")[-1]
            if tag == "IssueSymbol":
                sym = (child.text or "").strip().upper()
            elif tag == "ResumptionTradeTime":
                resumed = (child.text or "").strip()
        if sym and not resumed:
            halted.add(sym)
    return halted


class HaltWatch:
    def __init__(self):
        self.halted: set[str] = set()

    async def run(self, cfg, stop: asyncio.Event):
        ua = cfg.edgar_user_agent or "day-trader-bot"
        while not stop.is_set():
            try:
                self.halted = parse_halts(await asyncio.to_thread(http_get, HALTS_RSS, ua))
            except Exception as e:
                log.debug("halt feed failed: %s", e)
            await asyncio.sleep(61)        # Nasdaq asks for no more than once a minute


# ---------------- Top gainers scanner ----------------
def _ago(dt) -> str:
    mins = max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 60))
    return f"{mins} min ago" if mins < 120 else f"{mins // 60}h ago"


def latest_headline(news_client, symbol: str):
    """Most recent headline for a symbol in the last 24h, or None."""
    from datetime import timedelta
    from alpaca.data.requests import NewsRequest
    res = news_client.get_news(NewsRequest(symbols=symbol, start=datetime.now(timezone.utc) - timedelta(hours=24),
                                           limit=3, sort="desc"))
    items = res.data.get("news", []) if hasattr(res, "data") else []
    return items[0] if items else None


async def movers_scanner(queue: asyncio.Queue, cfg, stop: asyncio.Event, screener=None, news=None):
    """Every minute: today's biggest % gainers. Each new small-cap gainer becomes an event carrying its
    latest headline, so it goes through exactly the same checks (traps, price, volume, AI, pullback)."""
    from zoneinfo import ZoneInfo
    from alpaca.data.requests import MarketMoversRequest
    if screener is None:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.historical.screener import ScreenerClient
        screener = ScreenerClient(cfg.alpaca_key, cfg.alpaca_secret)
        news = NewsClient(cfg.alpaca_key, cfg.alpaca_secret)
    ny = ZoneInfo("America/New_York")
    seen: set[str] = set()
    day = None
    while not stop.is_set():
        today = datetime.now(ny).date()
        if day != today:
            day, seen = today, set()
        try:
            movers = await asyncio.to_thread(screener.get_market_movers, MarketMoversRequest(top=cfg.scan_top))
            for m in movers.gainers:
                sym = m.symbol.upper()
                if (sym in seen or not is_common_stock_symbol(sym)
                        or not (cfg.min_price <= m.price <= cfg.max_price) or m.percent_change < cfg.min_gap_pct):
                    continue
                seen.add(sym)
                try:
                    n = await asyncio.to_thread(latest_headline, news, sym)
                except Exception as e:
                    log.debug("news lookup %s failed: %s", sym, e)
                    n = None
                if n is not None:
                    at = n.created_at if n.created_at.tzinfo else n.created_at.replace(tzinfo=timezone.utc)
                    text = (f"{n.headline}. {getattr(n, 'summary', '') or ''} "
                            f"[Scanner: {sym} up {m.percent_change:+.0f}% today; this headline was published {_ago(at)}]")
                else:
                    text = f"[Scanner: {sym} up {m.percent_change:+.0f}% today; no news found in the last 24 hours]"
                await queue.put(Event(sym, text[:500], "scanner", datetime.now(timezone.utc)))
        except Exception as e:
            log.debug("movers scan failed: %s", e)
        await asyncio.sleep(cfg.scan_sec)


# ---------------- Alpaca news websocket ----------------
async def news_stream(queue: asyncio.Queue, cfg, stop: asyncio.Event):
    from alpaca.data.live.news import NewsDataStream

    async def on_news(n):
        at = getattr(n, "created_at", None) or datetime.now(timezone.utc)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        text = n.headline + (f". {n.summary}" if getattr(n, "summary", "") else "")
        for sym in (n.symbols or []):
            if is_common_stock_symbol(sym.upper()):
                await queue.put(Event(sym.upper(), text[:500], "news", at))

    delay = 5
    while not stop.is_set():
        stream = NewsDataStream(cfg.alpaca_key, cfg.alpaca_secret)
        stream.subscribe_news(on_news, "*")
        started = time.time()
        try:
            log.info("News stream connecting")
            await stream._run_forever()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("news stream dropped (%s); reconnecting in %ds", e, delay)
        delay = 5 if time.time() - started > 300 else min(delay * 2, 120)   # back off if it keeps failing
        await asyncio.sleep(delay)
