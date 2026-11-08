"""Opening Range Breakout on 'stocks in play' - the setup with the best published evidence.

Source: Zarattini, Barbon & Aziz (2024), "A Profitable Day Trading Strategy for the U.S. Equity Market":
  * each morning pick the ~20 stocks whose first-5-minute volume is the highest RELATIVE to their own
    normal first-5-minute volume (that's what "in play" means: unusual attention today),
  * if the first 5-minute candle closed up, buy when price breaks its high; closed down, short the low,
  * stop = 10% of the stock's 14-day daily ATR from the entry, exit at the close if not stopped,
  * risk a fixed share of the account on every trade.
An independent replication showed the same breakout on indices (SPY/QQQ) is worth nothing after costs,
so the whole edge is in WHICH stocks you trade, and in not letting costs eat a tiny stop.

Everything here is pure (no network) so the live bot and the backtest run the same code.
"""
from dataclasses import dataclass
from datetime import time as dtime

import numpy as np
import pandas as pd

OPEN = dtime(9, 30)


@dataclass
class OpeningRange:
    symbol: str
    high: float
    low: float
    open: float
    close: float
    volume: float
    atr_d: float          # 14-day daily ATR (in $)
    rel_volume: float     # today's opening-range volume / typical opening-range volume
    price_ref: float      # last close in the range (for % based floors)

    @property
    def direction(self) -> str | None:
        """'buy' if the range candle closed up, 'sell' if down, None if flat."""
        if self.close > self.open:
            return "buy"
        if self.close < self.open:
            return "sell"
        return None


def regular_session(df: pd.DataFrame) -> pd.DataFrame:
    """Only 09:30-16:00 New York bars (IEX returns pre/post-market bars too)."""
    t = df.index.time
    return df[(t >= OPEN) & (t < dtime(16, 0))]


def daily_atr_from_intraday(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """14-day ATR built from intraday bars, indexed by date. Uses regular-session bars only."""
    r = regular_session(df)
    if r.empty:
        return pd.Series(dtype=float)
    day = pd.Series(r.index.date, index=r.index)
    d = pd.DataFrame({"high": r["high"].groupby(day).max(), "low": r["low"].groupby(day).min(),
                      "close": r["close"].groupby(day).last()})
    prev = d["close"].shift()
    tr = pd.concat([d["high"] - d["low"], (d["high"] - prev).abs(), (d["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=5).mean()


def opening_ranges(df: pd.DataFrame, minutes: int = 5, bar_minutes: int = 5, lookback_days: int = 14) -> dict:
    """For every day in df: the opening range (first `minutes`), its relative volume and the daily ATR
    as of the PREVIOUS day (nothing from the future). Returns {date: OpeningRange-ish dict}."""
    r = regular_session(df)
    if r.empty:
        return {}
    nbars = max(1, minutes // bar_minutes)
    end = dtime(9, 30 + minutes) if minutes < 30 else dtime(9 + (30 + minutes) // 60, (30 + minutes) % 60)
    orb = r[r.index.time < end]
    day = pd.Series(orb.index.date, index=orb.index)
    g = orb.groupby(day)
    agg = pd.DataFrame({"high": g["high"].max(), "low": g["low"].min(), "open": g["open"].first(),
                        "close": g["close"].last(), "volume": g["volume"].sum(), "n": g["close"].count()})
    agg = agg[agg["n"] >= 1]
    typical = agg["volume"].shift().rolling(lookback_days, min_periods=5).mean()
    atr = daily_atr_from_intraday(df).shift()           # yesterday's ATR is what you know at 9:35
    out = {}
    for d, row in agg.iterrows():
        tv, a = typical.get(d, np.nan), atr.get(d, np.nan)
        if np.isnan(tv) or tv <= 0 or np.isnan(a) or a <= 0:
            continue
        out[d] = {"high": float(row["high"]), "low": float(row["low"]), "open": float(row["open"]),
                  "close": float(row["close"]), "volume": float(row["volume"]), "atr_d": float(a),
                  "rel_volume": float(row["volume"] / tv), "nbars": int(row["n"]), "expected_bars": nbars}
    return out


def pick_in_play(candidates: dict[str, dict], cfg) -> list[tuple[str, dict]]:
    """candidates = {symbol: opening-range dict for today}. Keep the top N by relative volume,
    subject to the paper's liquidity/price/volatility filters. Highest relative volume first."""
    keep = []
    for sym, o in candidates.items():
        if o["close"] < cfg.min_price or o["close"] > cfg.max_price:
            continue
        if o["atr_d"] < cfg.orb_min_atr_dollars:
            continue
        if o["rel_volume"] < cfg.orb_min_rel_volume:
            continue
        if o["high"] <= o["low"]:
            continue
        keep.append((sym, o))
    keep.sort(key=lambda kv: kv[1]["rel_volume"], reverse=True)
    return keep[: cfg.orb_top_n]


def stop_distance(entry: float, o: dict, cfg) -> float:
    """Paper: 10% of daily ATR. Floors: a share of the price (so costs can't eat the stop) and,
    optionally, the range itself. Never wider than the max stop."""
    d = cfg.orb_stop_atr_frac * o["atr_d"]
    d = max(d, entry * cfg.orb_min_stop_pct / 100)
    if cfg.orb_stop_mode == "range":
        d = max(d, o["high"] - o["low"])
    return min(d, entry * cfg.orb_max_stop_pct / 100)


def breakout_side(o: dict, price: float, allow_short: bool = True) -> str | None:
    """What to do at `price` given the range: 'buy' above a bullish range, 'sell' below a bearish one."""
    if o["close"] > o["open"] and price > o["high"]:
        return "buy"
    if o["close"] < o["open"] and price < o["low"] and allow_short:
        return "sell"
    return None
