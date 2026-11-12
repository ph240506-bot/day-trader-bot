"""Thin wrapper over Alpaca (+ optional Finnhub, SEC) so the engine can be tested with a fake."""
import json
import logging
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("broker")
NY = ZoneInfo("America/New_York")
MAJOR_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"}


@dataclass
class Quote:
    price: float
    prev_close: float
    bid: float
    ask: float
    day_volume: float | None
    prev_volume: float | None
    source: str = "alpaca-iex"
    age_sec: float | None = None      # how old the last trade is
    market_spread_pct: float | None = None   # whole-market spread ~15 min ago (IEX's own book is far wider)
    volume_source: str = "iex"

    @property
    def gap_pct(self):
        return (self.price / self.prev_close - 1) * 100 if self.prev_close else 0.0

    @property
    def iex_spread_pct(self):
        if self.bid > 0 and self.ask > 0 and self.ask >= self.bid:
            return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 100
        return None

    @property
    def spread_pct(self):
        """Best evidence of the real spread. IEX is one small exchange, so its spread is always at least as
        wide as the whole market's: when we also have the whole-market figure, the tighter one is the truth."""
        known = [s for s in (self.iex_spread_pct, self.market_spread_pct) if s is not None]
        return min(known) if known else None

    @property
    def rel_volume(self):
        """Today's volume vs the pace expected by now from yesterday's full day.
        At 10:30am only ~1/6 of the session has passed, so 20% of yesterday's volume = 1.2x pace."""
        if self.day_volume and self.prev_volume:
            return self.day_volume / (self.prev_volume * session_fraction())
        return None


def session_fraction(now=None) -> float:
    """Share of the regular session elapsed. Floor 0.05: the first few minutes really do carry about 5% of
    a day's volume (trading is busiest at the open), so a stock isn't called quiet at 9:35 for not having
    done 10% of yesterday's volume yet."""
    now = now or datetime.now(NY)
    mins = (now - now.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60
    return min(1.0, max(0.05, mins / 390))


def retry_once(fn):
    """Retry a broker call once on a dropped connection (the SHEL error from the first night)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            if not any(w in type(e).__name__ + str(e) for w in ("Connection", "Timeout", "RemoteDisconnected", "reset")):
                raise
            time.sleep(0.5)
            return fn(*a, **kw)
    return wrapper


def bigger_pool(client, size=32):
    """alpaca-py keeps 10 connections; the small-cap bot runs up to 32 checks at once."""
    try:
        from requests.adapters import HTTPAdapter
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=size)
        client._session.mount("https://", adapter)
        client._session.mount("http://", adapter)
    except Exception as e:      # private attribute: never let this stop the bot
        log.debug("pool resize skipped: %s", e)
    return client


def round_price(p: float) -> float:
    return round(p, 2) if p >= 1 else round(p, 4)


class RateLimiter:
    """Allow at most `n` calls per `per` seconds (thread-safe). allow() never blocks."""

    def __init__(self, n, per=60.0):
        self.n, self.per, self.calls, self.lock = n, per, deque(), threading.Lock()

    def allow(self) -> bool:
        now = time.time()
        with self.lock:
            while self.calls and now - self.calls[0] > self.per:
                self.calls.popleft()
            if len(self.calls) >= self.n:
                return False
            self.calls.append(now)
            return True


def snapshot_to_quote(snap, today=None) -> Quote | None:
    """Convert an alpaca-py Snapshot into a Quote. Works pre-market (no bar for today yet)."""
    if snap is None:
        return None
    today = today or datetime.now(NY).date()
    daily, prev = snap.daily_bar, snap.previous_daily_bar
    daily_is_today = daily is not None and daily.timestamp.astimezone(NY).date() == today
    prev_close = prev.close if (daily_is_today and prev) else (daily.close if daily else 0.0)
    lt, lq = snap.latest_trade, snap.latest_quote
    price = lt.price if lt else (daily.close if daily else 0.0)
    age = (datetime.now(timezone.utc) - lt.timestamp).total_seconds() if lt else None
    return Quote(
        price=float(price or 0), prev_close=float(prev_close or 0),
        bid=float(lq.bid_price or 0) if lq else 0.0, ask=float(lq.ask_price or 0) if lq else 0.0,
        day_volume=float(daily.volume) if daily_is_today else None,
        prev_volume=(float(prev.volume) if (daily_is_today and prev) else (float(daily.volume) if daily else None)),
        age_sec=age)


class AlpacaBroker:
    def __init__(self, cfg, trading_url=None, data_url=None, http_get=None):
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient
        self.cfg = cfg
        self.trading = bigger_pool(TradingClient(cfg.alpaca_key, cfg.alpaca_secret, paper=cfg.paper,
                                                 url_override=trading_url))
        self.data = bigger_pool(StockHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret, url_override=data_url))
        self._assets: dict[str, bool] = {}
        self._shares: dict[str, float | None] = {}
        self._cik: dict[str, int] | None = None
        self.finnhub_limit = RateLimiter(50, 60)       # free tier is 60/min; keep headroom
        self.delayed_ok = cfg.use_delayed_sip          # switched off for good if Alpaca refuses the feed
        self.delayed_limit = RateLimiter(60, 60)
        self.moomoo = None
        if getattr(cfg, "moomoo", False):
            try:
                from .moomoo_feed import MoomooFeed
                self.moomoo = MoomooFeed(cfg.moomoo_host, cfg.moomoo_port)
                log.info("moomoo live prices: on (%s:%s)", cfg.moomoo_host, cfg.moomoo_port)
            except Exception as e:
                log.warning("moomoo prices not available: %s", e)
        self._http_get = http_get or self._default_get

    @staticmethod
    def _default_get(url, headers=None, timeout=5):
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    # ---- market data ----
    def quotes(self, symbols: list[str], finnhub_for: set[str] = frozenset()) -> dict[str, Quote]:
        """One batched request for all symbols; Finnhub refresh for `finnhub_for` if allowed."""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockSnapshotRequest
        out: dict[str, Quote] = {}
        if not symbols:
            return out
        try:
            snaps = self.data.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=list(symbols), feed=DataFeed.IEX))
            for s in symbols:
                q = snapshot_to_quote(snaps.get(s))
                if q:
                    out[s] = q
        except Exception as e:
            log.debug("batch snapshot failed (%s); trying one by one", e)
            for s in symbols:
                try:
                    snaps = self.data.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=s, feed=DataFeed.IEX))
                    q = snapshot_to_quote(snaps.get(s))
                    if q:
                        out[s] = q
                except Exception as e2:
                    log.debug("snapshot %s failed: %s", s, e2)
        if self.moomoo:
            for s, t in self.moomoo.snapshot(list(symbols)).items():
                have = out.get(s)
                # take moomoo's price when it is fresher than what IEX gave us (it usually is for small caps)
                if have and have.age_sec is not None and have.age_sec <= t["age_sec"]:
                    continue
                q = have or Quote(0, 0, 0, 0, None, None)
                q.price, q.age_sec, q.source = t["price"], t["age_sec"], "moomoo"
                q.prev_close = t["prev_close"] or q.prev_close
                if t["bid"] > 0 and t["ask"] >= t["bid"]:
                    q.bid, q.ask = t["bid"], t["ask"]
                if t["shares"] > 0 and self._shares.get(s) is None:
                    self._shares[s] = t["shares"]
                out[s] = q
        for s in finnhub_for:
            have = out.get(s)
            if have and have.age_sec is not None and have.age_sec <= 20:
                continue              # Alpaca's price is fresh: save the Finnhub budget for stocks that need it
            fh = self._finnhub(s)
            if fh:
                q = out.get(s) or Quote(0, 0, 0, 0, None, None)
                q.price, q.prev_close, q.source, q.age_sec = fh["c"], fh.get("pc") or q.prev_close, "finnhub", \
                    max(0.0, time.time() - fh.get("t", time.time()))
                out[s] = q
        return out

    def quote(self, symbol: str) -> Quote | None:
        q = self.quotes([symbol], finnhub_for={symbol}).get(symbol)
        return self._add_market_view(symbol, q) if q else q

    def _add_market_view(self, symbol, q: Quote) -> Quote:
        """Spread and volume from the whole market, 15 minutes delayed (free). The live PRICE still comes
        from IEX/Finnhub: a 15-minute-old price is useless for trading, but a 15-minute-old spread or
        volume figure describes the stock far better than IEX's sliver of the market does."""
        if not self.delayed_ok or not self.delayed_limit.allow():
            return q
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockSnapshotRequest
        try:
            snaps = self.data.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbol, feed=DataFeed.DELAYED_SIP))
            d = snapshot_to_quote(snaps.get(symbol))
        except Exception as e:
            msg = str(e).lower()
            if any(w in msg for w in ("subscription", "forbidden", "403", "not permitted", "invalid feed", "422")):
                self.delayed_ok = False
                log.warning("Delayed whole-market feed not available on this plan (%s). Using IEX only.", str(e)[:100])
            else:
                log.debug("delayed snapshot %s failed: %s", symbol, e)
            return q
        if not d:
            return q
        q.market_spread_pct = d.iex_spread_pct           # same maths, whole-market quote
        if d.day_volume and d.prev_volume:
            q.day_volume, q.prev_volume, q.volume_source = d.day_volume, d.prev_volume, "market-delayed"
        return q

    def _finnhub(self, symbol):
        if not self.cfg.finnhub_key or not self.finnhub_limit.allow():
            return None
        try:
            d = self._http_get(f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={self.cfg.finnhub_key}")
            if d.get("c") and abs(time.time() - d.get("t", 0)) < 180:
                return d
        except Exception as e:
            log.debug("finnhub failed %s: %s", symbol, e)
        return None

    def intraday(self, symbol: str):
        """(VWAP, high of day) from today's 1-minute bars, pre-market included. (None, None) if unknown."""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        start = datetime.now(NY).replace(hour=4, minute=0, second=0, microsecond=0)
        try:
            bars = self.data.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start, feed=DataFeed.IEX)).data.get(symbol, [])
            if not bars:
                return None, None
            vol = sum(b.volume for b in bars)
            vwap = sum((b.vwap or b.close) * b.volume for b in bars) / vol if vol > 0 else None
            return vwap, max(b.high for b in bars)
        except Exception as e:
            log.debug("intraday %s failed: %s", symbol, e)
            return None, None

    def vwap(self, symbol: str) -> float | None:
        return self.intraday(symbol)[0]

    def shares_outstanding(self, symbol: str) -> float | None:
        """Shares outstanding (a stand-in for float). Finnhub first, then SEC filings. Cached."""
        if symbol in self._shares:
            return self._shares[symbol]
        val = None
        if self.cfg.finnhub_key and self.finnhub_limit.allow():
            try:
                d = self._http_get(f"https://finnhub.io/api/v1/stock/profile2?symbol={symbol}&token={self.cfg.finnhub_key}")
                if d.get("shareOutstanding"):
                    val = float(d["shareOutstanding"]) * 1_000_000      # Finnhub reports millions
            except Exception as e:
                log.debug("finnhub profile %s: %s", symbol, e)
        if val is None and self.cfg.edgar_user_agent:
            val = self._sec_shares(symbol)
        self._shares[symbol] = val
        return val

    def _sec_shares(self, symbol):
        headers = {"User-Agent": self.cfg.edgar_user_agent}
        try:
            if self._cik is None:
                data = self._http_get("https://www.sec.gov/files/company_tickers.json", headers, 10)
                self._cik = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
            cik = self._cik.get(symbol)
            if not cik:
                return None
            d = self._http_get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/dei/"
                               f"EntityCommonStockSharesOutstanding.json", headers, 10)
            facts = d.get("units", {}).get("shares", [])
            if not facts:
                return None
            latest = max(facts, key=lambda f: (f.get("end", ""), f.get("filed", "")))
            return float(latest["val"])
        except Exception as e:
            log.debug("SEC shares %s: %s", symbol, e)
            return None

    @retry_once
    def asset_ok(self, symbol: str) -> bool:
        if symbol not in self._assets:
            try:
                a = self.trading.get_asset(symbol)
                exch = str(getattr(a.exchange, "value", a.exchange)).upper()
                from ..funds import looks_like_fund
                self._assets[symbol] = (bool(a.tradable) and exch in MAJOR_EXCHANGES
                                        and not looks_like_fund(getattr(a, "name", "")))
            except Exception:
                self._assets[symbol] = False
        return self._assets[symbol]

    # ---- account / clock ----
    @retry_once
    def account(self):
        a = self.trading.get_account()
        return float(a.equity), float(a.last_equity)

    @retry_once
    def clock(self):
        c = self.trading.get_clock()
        return c.is_open, c.next_open, c.next_close, c.timestamp

    # ---- orders ----
    # no retry: re-sending an order after a dropped connection could place it twice
    def limit(self, symbol, qty, side, price, extended) -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest
        o = self.trading.submit_order(LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=round_price(price), extended_hours=extended))
        return str(o.id)

    # no retry: re-sending an order after a dropped connection could place it twice
    def market_sell(self, symbol, qty) -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        return str(self.trading.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)).id)

    @retry_once
    def order(self, order_id):
        o = self.trading.get_order_by_id(order_id)
        status = str(getattr(o.status, "value", o.status)).lower()
        return status, float(o.filled_qty or 0), float(o.filled_avg_price or 0)

    def cancel(self, order_id):
        try:
            self.trading.cancel_order_by_id(order_id)
        except Exception as e:
            log.debug("cancel %s: %s", order_id, e)
