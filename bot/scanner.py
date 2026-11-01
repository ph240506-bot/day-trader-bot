"""Finds today's biggest movers so the bot isn't stuck on a fixed list.

Uses Alpaca's free screener (top % gainers and losers), then throws out the junk that
burns day traders: penny stocks, warrants/units, and anything Alpaca won't let you trade.
"""
import logging
import time

from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import MarketMoversRequest

log = logging.getLogger("scanner")


class MoverScanner:
    def __init__(self, cfg, trading_client):
        self.cfg = cfg
        self.trading = trading_client
        self.screener = ScreenerClient(cfg.alpaca_key, cfg.alpaca_secret)
        self.symbols: list[str] = []
        self.shortable: set[str] = set()
        self._last = 0.0
        self._asset_cache: dict[str, object] = {}

    def refresh(self, force=False) -> list[str]:
        if not force and time.time() - self._last < self.cfg.scan_every_minutes * 60:
            return self.symbols
        self._last = time.time()
        try:
            movers = self.screener.get_market_movers(MarketMoversRequest(top=50))
            raw = list(movers.gainers) + list(movers.losers)
        except Exception as e:
            log.warning("mover scan failed, keeping previous list: %s", e)
            return self.symbols
        picked = pick_movers(raw, self.cfg, self._asset)
        self.symbols = [m.symbol for m in picked]
        self.shortable = {m.symbol for m in picked if getattr(self._asset(m.symbol), "easy_to_borrow", False)}
        log.info("Top movers: %s", ", ".join(f"{m.symbol} {m.percent_change:+.1f}%" for m in picked) or "none")
        return self.symbols

    def _asset(self, symbol):
        if symbol not in self._asset_cache:
            try:
                self._asset_cache[symbol] = self.trading.get_asset(symbol)
            except Exception:
                self._asset_cache[symbol] = None
        return self._asset_cache[symbol]


def pick_movers(raw, cfg, get_asset) -> list:
    """Filter and rank screener results. get_asset(symbol) -> Alpaca asset or None."""
    seen, keep = set(), []
    for m in sorted(raw, key=lambda m: abs(m.percent_change), reverse=True):
        sym = m.symbol
        if sym in seen:
            continue
        seen.add(sym)
        if not (sym.isalpha() and len(sym) <= 5):      # skips warrants, units, preferreds
            continue
        if m.price < cfg.min_price or m.price > cfg.max_price:
            continue
        if abs(m.percent_change) < cfg.min_move_pct:
            continue
        a = get_asset(sym)
        if a is None or not getattr(a, "tradable", False) or str(getattr(a, "status", "")).lower().endswith("inactive"):
            continue
        keep.append(m)
        if len(keep) >= cfg.top_movers:
            break
    return keep
