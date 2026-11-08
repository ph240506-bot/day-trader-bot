"""Replay the main bot's entry rules over months of real 5-minute bars to see which settings would have paid.

Run on the Pi:   botbacktest              (last 90 days)
                 DAYS=180 botbacktest     (longer = more trustworthy)

Results are in R: 1R = the amount risked on a trade. +0.2R per trade with 0.5% risk per trade
is about +0.1% of the account per trade. It leaves out the AI check, the top-mover scanner
and the position limit, so treat it as a test of the chart rules, not a promise.
"""
import os
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone

import numpy as np
import pandas as pd

from .indicators import add_all
from .strategy import signal_from_rows

OPEN, CLOSE = dtime(9, 30), dtime(16, 0)


@dataclass
class Setup:
    symbol: str
    side: str
    score: int
    i: int          # index of the finished signal bar
    atr: float


def find_setups(sym: str, df: pd.DataFrame, spy: pd.DataFrame | None, cfg) -> list[Setup]:
    """Every bar where the live bot would have placed an order (before the AI check)."""
    ind = add_all(df)
    spy_ind = add_all(spy) if spy is not None else None
    out = []
    for i in range(30, len(ind) - 1):
        t = ind.index[i] + timedelta(minutes=cfg.bar_minutes)          # the bot sees the bar when it finishes
        if t.weekday() >= 5 or not (OPEN <= t.time() < CLOSE):
            continue
        since_open = (t - t.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60
        to_close = (t.replace(hour=16, minute=0, second=0, microsecond=0) - t).total_seconds() / 60
        if since_open < cfg.open_delay_min or to_close < cfg.no_entry_before_close_min:
            continue
        sig = signal_from_rows(sym, ind.iloc[i - 1], ind.iloc[i], allow_short=True)
        if not sig or sig.atr <= 0:
            continue
        if cfg.market_filter and spy_ind is not None and ind.index[i] in spy_ind.index:
            row = spy_ind.loc[ind.index[i]]
            regime = "up" if row["close"] >= row["vwap"] else "down"
            if (sig.side == "buy" and regime == "down") or (sig.side == "sell" and regime == "up"):
                continue
        out.append(Setup(sym, sig.side, sig.score, i, sig.atr))
    return out


def simulate(setups, df, stop_mult, rr, min_score, longs_only, cooldown_min, flatten_min=10):
    """Enter at the next bar's open; stop and target checked on every bar after (stop first if both hit)."""
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    idx = df.index
    results, busy_until = [], None
    for s in setups:
        if s.score < min_score or (longs_only and s.side == "sell"):
            continue
        e = s.i + 1
        if e >= len(df) or idx[e].date() != idx[s.i].date():
            continue
        if busy_until is not None and idx[e] < busy_until:
            continue
        entry, dist = o[e], stop_mult * s.atr
        sign = 1 if s.side == "buy" else -1
        stop, target = entry - sign * dist, entry + sign * rr * dist
        flat_at = idx[e].replace(hour=16, minute=0, second=0, microsecond=0) - timedelta(minutes=flatten_min)
        exit_px, j = None, e
        for j in range(e, len(df)):
            if idx[j].date() != idx[e].date() or idx[j] >= flat_at:
                exit_px = o[j] if idx[j].date() == idx[e].date() else c[j - 1]
                break
            hit_stop = l[j] <= stop if sign > 0 else h[j] >= stop
            hit_tgt = h[j] >= target if sign > 0 else l[j] <= target
            if hit_stop:                                   # gapped through? fill at the worse open
                exit_px = min(stop, o[j]) if sign > 0 else max(stop, o[j])
                break
            if hit_tgt:
                exit_px = target
                break
        if exit_px is None:
            exit_px, j = c[-1], len(df) - 1
        results.append({"symbol": s.symbol, "side": s.side, "score": s.score, "time": idx[e],
                        "r": sign * (exit_px - entry) / dist})
        busy_until = idx[j] + timedelta(minutes=cooldown_min)
    return results


def stats(rs: list[float]) -> dict:
    if not rs:
        return {"n": 0, "win": 0, "avg": 0, "total": 0, "pf": 0}
    a = np.array(rs)
    gl = -a[a <= 0].sum()
    return {"n": len(a), "win": (a > 0).mean() * 100, "avg": a.mean(), "total": a.sum(),
            "pf": a[a > 0].sum() / gl if gl > 0 else float("inf")}


def grid(all_setups, frames, cfg):
    rows = []
    for stop_mult in (1.0, 1.5, 2.5, 4.0):
        for rr in (1.0, 1.5, 2.0, 3.0):
            for min_score in (3, 4, 5):
                for longs_only in (False, True):
                    trades = []
                    for sym, setups in all_setups.items():
                        trades += simulate(setups, frames[sym], stop_mult, rr, min_score, longs_only,
                                           cfg.reentry_cooldown_min)
                    trades.sort(key=lambda t: t["time"])
                    half = len(trades) // 2
                    rows.append({"stop": stop_mult, "rr": rr, "score": min_score, "longs": longs_only,
                                 **stats([t["r"] for t in trades]),
                                 "first_half": sum(t["r"] for t in trades[:half]),
                                 "second_half": sum(t["r"] for t in trades[half:]),
                                 "trades": trades})
    return rows


def fmt(r):
    pf = "inf" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
    return (f"  stop {r['stop']:.1f} ATR | target {r['rr']:.1f}R | score>={r['score']} | "
            f"{'longs only ' if r['longs'] else 'long+short '}| {r['n']:4d} trades  win {r['win']:3.0f}%  "
            f"avg {r['avg']:+.2f}R  total {r['total']:+6.1f}R  PF {pf}  halves {r['first_half']:+.1f}R / "
            f"{r['second_half']:+.1f}R")


def report(rows, cfg, days, n_symbols) -> list[str]:
    cur = next((r for r in rows if r["stop"] == cfg.stop_atr_mult and r["rr"] == cfg.reward_risk
                and r["score"] == cfg.min_score and not r["longs"]), None)
    ok = [r for r in rows if r["n"] >= 30]
    best = sorted(ok, key=lambda r: r["total"], reverse=True)[:8]
    steady = [r for r in ok if r["first_half"] > 0 and r["second_half"] > 0 and r["avg"] > 0.05]
    lines = [f"Backtest: last {days} days, {n_symbols} symbols, {cfg.bar_minutes}-min bars (IEX). Results in R "
             f"(1R = the amount risked per trade).", ""]
    if cur:
        lines += ["Your current settings:", fmt(cur)]
        for side in ("buy", "sell"):
            st = stats([t["r"] for t in cur["trades"] if t["side"] == side])
            lines.append(f"    {'longs ' if side == 'buy' else 'shorts'}: {st['n']} trades, win {st['win']:.0f}%, "
                         f"avg {st['avg']:+.2f}R")
        by_sym = {}
        for t in cur["trades"]:
            by_sym.setdefault(t["symbol"], []).append(t["r"])
        lines.append("    by symbol: " + ", ".join(f"{s} {sum(v):+.1f}R" for s, v in sorted(by_sym.items())))
        lines.append("")
    lines += ["Best 8 combinations (at least 30 trades):"] + [fmt(r) for r in best] + [""]
    lines.append(f"Profitable in BOTH halves of the period with avg > +0.05R: {len(steady)} of {len(ok)}")
    lines += [fmt(r) for r in sorted(steady, key=lambda r: r["avg"], reverse=True)[:5]]
    if not steady:
        lines.append("  none: on this data the chart rules have no reliable edge at any setting.")
    return lines


def fetch(cfg, symbols, days):
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from .market import _split
    client = StockHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)
    df = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbols, timeframe=TimeFrame(cfg.bar_minutes, TimeFrameUnit.Minute),
        start=datetime.now(timezone.utc) - timedelta(days=days), feed=DataFeed.IEX)).df
    return _split(df)


def main():
    from .config import Config
    cfg = Config()
    days = int(os.getenv("DAYS", "90"))
    symbols = list(dict.fromkeys(list(cfg.stocks) + ["SPY"]))
    print(f"Downloading {days} days of bars for {len(symbols)} symbols (takes a minute)...", flush=True)
    frames = fetch(cfg, symbols, days)
    spy = frames.get("SPY")
    all_setups = {s: find_setups(s, frames[s], spy, cfg) for s in symbols if s in frames and len(frames[s]) > 50}
    print("\n".join(report(grid(all_setups, frames, cfg), cfg, days, len(all_setups))))


if __name__ == "__main__":
    main()
