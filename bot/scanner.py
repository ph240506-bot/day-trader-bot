"""Finds today's biggest movers so the bot isn't stuck on a fixed list.

Uses Alpaca's free screener (top % gainers and losers), then throws out the junk that
burns day traders: penny stocks, warrants/units, and anything Alpaca won't let you trade.
"""
import logging
import time

from alpaca.data.enums import MostActivesBy
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import MarketMoversRequest, MostActivesRequest

from .funds import looks_like_fund

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


class InPlayScanner:
    """Builds the morning's candidate pool (most active + biggest movers), then ranks it by opening
    relative volume using the shared ORB code. Call pick() once the opening range has closed."""

    def __init__(self, cfg, market):
        self.cfg, self.mkt = cfg, market
        self.screener = ScreenerClient(cfg.alpaca_key, cfg.alpaca_secret)
        self.shortable: set[str] = set()
        self._asset_cache: dict[str, object] = {}

    def candidates(self) -> list[str]:
        pool = []
        try:
            for by in (MostActivesBy.VOLUME, MostActivesBy.TRADES):
                pool += [a.symbol for a in self.screener.get_most_actives(MostActivesRequest(top=100, by=by)).most_actives]
        except Exception as e:
            log.warning("most-actives scan failed: %s", e)
        try:
            mv = self.screener.get_market_movers(MarketMoversRequest(top=50))
            pool += [m.symbol for m in list(mv.gainers) + list(mv.losers)]
        except Exception as e:
            log.warning("movers scan failed: %s", e)
        out = []
        for sym in dict.fromkeys(pool):
            if not (sym.isalpha() and len(sym) <= 5):
                continue
            a = self._asset(sym)
            if a is None or not getattr(a, "tradable", False):
                continue
            if not self.cfg.orb_allow_funds and looks_like_fund(getattr(a, "name", "")):
                continue
            out.append(sym)
            if getattr(a, "easy_to_borrow", False) and getattr(a, "shortable", False):
                self.shortable.add(sym)
            if len(out) >= self.cfg.orb_candidates:
                break
        return out

    def pick(self, today) -> list[tuple[str, dict]]:
        """Today's stocks in play: [(symbol, opening-range dict)], highest relative volume first."""
        from .orb import opening_ranges, pick_in_play
        syms = self.candidates()
        if not syms:
            return []
        frames = self.mkt.bars(syms, days=25)
        today_ranges = {}
        for sym, df in frames.items():
            r = opening_ranges(df, self.cfg.orb_minutes, self.cfg.bar_minutes)
            if today in r and r[today]["nbars"] >= r[today]["expected_bars"]:
                today_ranges[sym] = r[today]
        picks = pick_in_play(today_ranges, self.cfg)
        log.info("Stocks in play (%d of %d candidates): %s", len(picks), len(syms),
                 ", ".join(f"{s} {o['rel_volume']:.1f}x" for s, o in picks) or "none")
        return picks

    def _asset(self, symbol):
        if symbol not in self._asset_cache:
            try:
                self._asset_cache[symbol] = self.mkt.trading.get_asset(symbol)
            except Exception:
                self._asset_cache[symbol] = None
        return self._asset_cache[symbol]
