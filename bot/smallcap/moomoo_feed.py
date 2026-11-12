"""Live US stock prices from moomoo's OpenAPI (via the OpenD gateway container).

Why: Alpaca's free feed is IEX, about 2.5% of US trading, so for small caps it often has no recent trade
at all. moomoo's free feed (Nasdaq Basic) sees most of the market, pre-market included.

Safety: a moomoo price is only used when its timestamp proves it is fresh. Anything doubtful (gateway
down, odd timestamp, halted stock) is ignored and the bot carries on with IEX/Finnhub as before.
"""
import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("moomoo")
NY = ZoneInfo("America/New_York")


def _f(v) -> float:
    try:
        x = float(v)
        return x if x == x else 0.0          # NaN -> 0
    except (TypeError, ValueError):
        return 0.0


def row_to_tick(row: dict, now: datetime | None = None) -> dict | None:
    """One snapshot row -> {price, prev_close, bid, ask, age_sec, shares, premarket}. None if unusable."""
    now = now or datetime.now(NY)
    try:
        ts = datetime.strptime(str(row.get("update_time", ""))[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=NY)
    except ValueError:
        return None
    age = (now - ts).total_seconds()
    if age < -120:                            # timestamp "in the future": wrong clock or time zone, don't trust
        return None
    if str(row.get("suspension", "")).lower() in ("true", "1"):
        return None
    premarket = now.time() < datetime.strptime("09:30", "%H:%M").time()
    price = _f(row.get("pre_price")) if premarket and _f(row.get("pre_price")) > 0 else _f(row.get("last_price"))
    if price <= 0:
        return None
    return {"price": price, "prev_close": _f(row.get("prev_close_price")), "bid": _f(row.get("bid_price")),
            "ask": _f(row.get("ask_price")), "age_sec": max(0.0, age),
            "shares": _f(row.get("outstanding_shares")) or _f(row.get("issued_shares")), "premarket": premarket}


class MoomooFeed:
    def __init__(self, host="opend", port=11111, ctx_factory=None):
        self.host, self.port = host, port
        self._factory = ctx_factory
        self._ctx = None
        self._lock = threading.Lock()
        self._down_until = 0.0
        self._calls: list[float] = []         # moomoo allows 60 snapshot requests per 30 s
        self.ok_count = self.fail_count = 0

    def _context(self):
        if self._ctx is None:
            if self._factory:
                self._ctx = self._factory()
            else:
                from moomoo import OpenQuoteContext, SysConfig
                try:
                    SysConfig.set_all_thread_daemon(True)
                except Exception:
                    pass
                self._ctx = OpenQuoteContext(host=self.host, port=self.port, is_async_connect=True)
        return self._ctx

    def snapshot(self, symbols: list[str]) -> dict[str, dict]:
        """{symbol: tick}. Empty on any problem: the caller just keeps the prices it already has."""
        if not symbols or time.time() < self._down_until:
            return {}
        out = {}
        with self._lock:
            now = time.time()
            self._calls = [t for t in self._calls if now - t < 30]
            if len(self._calls) >= 50:
                return {}
            try:
                ctx = self._context()
                for i in range(0, len(symbols), 400):
                    self._calls.append(time.time())
                    ret, data = ctx.get_market_snapshot([f"US.{s}" for s in symbols[i:i + 400]])
                    if ret != 0:
                        raise RuntimeError(str(data)[:150])
                    for row in data.to_dict("records"):
                        tick = row_to_tick(row)
                        if tick:
                            out[str(row["code"]).split(".", 1)[-1]] = tick
                self.ok_count += 1
            except Exception as e:
                self.fail_count += 1
                self._down_until = time.time() + 60          # don't slow every check while the gateway is down
                log.warning("moomoo prices unavailable (%s). Using IEX/Finnhub for the next minute.", str(e)[:150])
                return {}
        return out
