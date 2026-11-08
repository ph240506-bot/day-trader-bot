"""Replay strategies over months of real 5-minute bars, net of costs, and say which one to run.

Run on the Pi:   botbacktest              (last 90 days,  ~3 min)
                 DAYS=180 botbacktest     (last 180 days, ~6 min, more trustworthy)

Two strategies are compared on the same data:
  * ORB  - opening range breakout on "stocks in play" (highest relative volume this morning). This is the
           setup with published, peer-reviewed, net-of-commission evidence (Zarattini, Barbon & Aziz 2024).
  * EMA  - the bot's original EMA-cross score (tested on the in-play stocks AND on the fixed list).

Results are in R (1R = the amount risked on a trade). Costs: BACKTEST_COST_PCT per side (default 0.05%)
for slippage + spread, which is what killed the same breakout on SPY/QQQ in independent tests.
Leaves out: the AI check, halts, borrow availability for shorts. Treat it as a test of the rules.
"""
import os
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone

import numpy as np
import pandas as pd

from .indicators import add_all
from .orb import breakout_side, opening_ranges, pick_in_play, regular_session, stop_distance
from .strategy import signal_from_rows

OPEN, CLOSE = dtime(9, 30), dtime(16, 0)
FLATTEN = dtime(15, 55)


# ============================ ORB ============================
@dataclass
class Trade:
    symbol: str
    side: str
    time: pd.Timestamp
    r: float
    entry: float
    dist: float          # $ per share risked
    strategy: str


def simulate_orb_day(sym, day_df, o, cfg, cost, stop_frac, stop_mode, target_r, window_min, minutes):
    """One stock, one day. Returns a Trade or None."""
    start = dtime(9, 30 + minutes) if minutes < 30 else dtime(10, minutes - 30)
    deadline = (datetime.combine(day_df.index[0].date(), OPEN) + timedelta(minutes=window_min)).time()
    after = day_df[day_df.index.time >= start]
    if after.empty:
        return None
    o_, h, l, c = (after[k].to_numpy() for k in ("open", "high", "low", "close"))
    idx = after.index
    entry = side = None
    for i in range(len(after)):
        if idx[i].time() >= deadline or idx[i].time() >= FLATTEN:
            return None
        s = breakout_side(o, h[i]) if o["close"] > o["open"] else breakout_side(o, l[i])
        if s is None:
            continue
        # buy-stop at the range high: fills at the high (or at the open if it gapped past it), plus slippage
        raw = max(o["high"], o_[i]) if s == "buy" else min(o["low"], o_[i])
        entry = raw * (1 + cost / 100) if s == "buy" else raw * (1 - cost / 100)
        side, e = s, i
        break
    if entry is None:
        return None
    cfg_local = _StopCfg(cfg, stop_frac, stop_mode)
    dist = stop_distance(entry, o, cfg_local)
    sign = 1 if side == "buy" else -1
    stop = entry - sign * dist
    target = entry + sign * target_r * dist if target_r > 0 else None
    exit_px = None
    for j in range(e, len(after)):
        if idx[j].time() >= FLATTEN:
            exit_px = o_[j]
            break
        # Entry bar included: if its low touched the stop we count the stop, even though the dip may have
        # come BEFORE the break. Pessimistic on purpose: on random data this rule loses a little, so a
        # positive result on real data is not a gift from the simulator.
        lo, hi = l[j], h[j]
        hit_stop = lo <= stop if sign > 0 else hi >= stop
        hit_tgt = target is not None and (hi >= target if sign > 0 else lo <= target)
        if hit_stop:
            if j == e:
                exit_px = stop
            else:                                    # gapped through the stop? fill at the worse open
                exit_px = min(stop, o_[j]) if sign > 0 else max(stop, o_[j])
            break
        if hit_tgt:
            exit_px = target
            break
    if exit_px is None:
        exit_px = c[-1]
    exit_px = exit_px * (1 - cost / 100) if sign > 0 else exit_px * (1 + cost / 100)
    return Trade(sym, side, idx[e], sign * (exit_px - entry) / dist, entry, dist, "orb")


class _StopCfg:
    """cfg view with the stop settings of one grid cell."""
    def __init__(self, cfg, frac, mode):
        self.orb_stop_atr_frac, self.orb_stop_mode = frac, mode
        self.orb_min_stop_pct, self.orb_max_stop_pct = cfg.orb_min_stop_pct, cfg.orb_max_stop_pct


def run_orb(frames, cfg, cost, minutes, stop_frac, stop_mode, target_r, window_min, top_n):
    """Every day: rank the universe by opening relative volume, trade the top N."""
    ranges = {s: opening_ranges(df, minutes, cfg.bar_minutes) for s, df in frames.items()}
    days = sorted({d for r in ranges.values() for d in r})
    sel_cfg = _SelCfg(cfg, top_n)
    trades, picked_per_day = [], []
    for d in days:
        today = {s: r[d] for s, r in ranges.items() if d in r}
        picks = pick_in_play(today, sel_cfg)
        picked_per_day.append(len(picks))
        for sym, o in picks:
            df = frames[sym]
            day_df = regular_session(df[df.index.date == d])
            t = simulate_orb_day(sym, day_df, o, cfg, cost, stop_frac, stop_mode, target_r, window_min, minutes)
            if t:
                trades.append(t)
    return trades, (np.mean(picked_per_day) if picked_per_day else 0)


class _SelCfg:
    def __init__(self, cfg, top_n):
        self.min_price, self.max_price = cfg.min_price, cfg.max_price
        self.orb_min_atr_dollars, self.orb_min_rel_volume, self.orb_top_n = (
            cfg.orb_min_atr_dollars, cfg.orb_min_rel_volume, top_n)


# ============================ EMA (original rules) ============================
def ema_trades(sym, df, spy, cfg, cost, stop_mult, rr, min_score, in_play_days=None):
    ind = add_all(df)
    spy_ind = add_all(spy) if spy is not None else None
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    idx = df.index
    out, busy_until = [], None
    for i in range(30, len(ind) - 1):
        t = idx[i] + timedelta(minutes=cfg.bar_minutes)
        if t.weekday() >= 5 or not (OPEN <= t.time() < CLOSE):
            continue
        if in_play_days is not None and idx[i].date() not in in_play_days:
            continue
        since_open = (t - t.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60
        to_close = (t.replace(hour=16, minute=0, second=0, microsecond=0) - t).total_seconds() / 60
        if since_open < cfg.open_delay_min or to_close < cfg.no_entry_before_close_min:
            continue
        if busy_until is not None and idx[i] < busy_until:
            continue
        sig = signal_from_rows(sym, ind.iloc[i - 1], ind.iloc[i], allow_short=True)
        if not sig or sig.atr <= 0 or sig.score < min_score:
            continue
        if cfg.market_filter and spy_ind is not None and idx[i] in spy_ind.index:
            row = spy_ind.loc[idx[i]]
            regime = "up" if row["close"] >= row["vwap"] else "down"
            if (sig.side == "buy" and regime == "down") or (sig.side == "sell" and regime == "up"):
                continue
        e = i + 1
        if e >= len(df) or idx[e].date() != idx[i].date():
            continue
        sign = 1 if sig.side == "buy" else -1
        entry = o[e] * (1 + sign * cost / 100)
        dist = stop_mult * sig.atr
        stop, target = entry - sign * dist, entry + sign * rr * dist
        exit_px, j = None, e
        for j in range(e, len(df)):
            if idx[j].date() != idx[e].date() or idx[j].time() >= FLATTEN:
                exit_px = o[j] if idx[j].date() == idx[e].date() else c[j - 1]
                break
            if (l[j] <= stop if sign > 0 else h[j] >= stop):
                exit_px = min(stop, o[j]) if sign > 0 else max(stop, o[j])
                break
            if (h[j] >= target if sign > 0 else l[j] <= target):
                exit_px = target
                break
        if exit_px is None:
            exit_px, j = c[-1], len(df) - 1
        exit_px = exit_px * (1 - sign * cost / 100)
        out.append(Trade(sym, sig.side, idx[e], sign * (exit_px - entry) / dist, entry, dist, "ema"))
        busy_until = idx[j] + timedelta(minutes=cfg.reentry_cooldown_min)
    return out


# ============================ scoring ============================
def stats(trades):
    rs = np.array([t.r for t in trades]) if trades else np.array([])
    if len(rs) == 0:
        return {"n": 0, "win": 0.0, "avg": 0.0, "total": 0.0, "pf": 0.0, "months_pos": "0/0", "h1": 0.0, "h2": 0.0}
    gl = -rs[rs <= 0].sum()
    by_month = {}
    for t in trades:
        by_month.setdefault(t.time.strftime("%Y-%m"), []).append(t.r)
    months = [sum(v) for _, v in sorted(by_month.items())]
    half = len(trades) // 2
    ordered = sorted(trades, key=lambda t: t.time)
    return {"n": len(rs), "win": (rs > 0).mean() * 100, "avg": rs.mean(), "total": rs.sum(),
            "pf": rs[rs > 0].sum() / gl if gl > 0 else float("inf"),
            "months_pos": f"{sum(m > 0 for m in months)}/{len(months)}",
            "h1": sum(t.r for t in ordered[:half]), "h2": sum(t.r for t in ordered[half:])}


def dollars(trades, account, risk_pct, max_pos_pct):
    """Realistic $ result with NO leverage: risk is capped by how many shares the position cap buys."""
    total = 0.0
    for t in trades:
        risk = min(account * risk_pct / 100, account * max_pos_pct / 100 / t.entry * t.dist)
        total += t.r * risk
    return total


def fmt(name, st):
    pf = "inf" if st["pf"] == float("inf") else f"{st['pf']:.2f}"
    return (f"  {name:<58} {st['n']:5d} trades  win {st['win']:3.0f}%  avg {st['avg']:+.3f}R  "
            f"total {st['total']:+7.1f}R  PF {pf}  months+ {st['months_pos']:>5}  halves {st['h1']:+.1f}/{st['h2']:+.1f}")


def robust(st):
    return st["n"] >= 40 and st["avg"] > 0.03 and st["h1"] > 0 and st["h2"] > 0 and st["pf"] >= 1.1


def since(trades, cutoff):
    return [t for t in trades if cutoff is None or t.time >= cutoff]


def report(frames, cfg, days, cost, cutoff=None) -> list[str]:
    lines = [f"Backtest: last {days} days, {len(frames)} symbols, {cfg.bar_minutes}-min IEX bars, "
             f"costs {cost:.2f}% per side. 1R = the amount risked per trade.", ""]
    results = []
    # --- ORB grid ---
    lines.append("ORB on stocks in play (top by opening relative volume), long + short:")
    for minutes in (5, 15):
        for stop_frac, stop_mode in ((0.10, "atr"), (0.25, "atr"), (0.10, "range")):
            for target_r in (0, 3, 10):
                trades, avg_picks = run_orb(frames, cfg, cost, minutes, stop_frac, stop_mode, target_r,
                                            cfg.orb_entry_window_min, cfg.orb_top_n)
                trades = since(trades, cutoff)
                name = (f"ORB {minutes:>2}m | stop {stop_mode} {stop_frac:.2f}ATR | "
                        f"target {'close' if target_r == 0 else f'{target_r}R'}")
                st = stats(trades)
                results.append((name, st, trades, {"STRATEGY": "orb", "ORB_MINUTES": minutes,
                                                   "ORB_STOP_ATR_FRAC": stop_frac, "ORB_STOP_MODE": stop_mode,
                                                   "ORB_TARGET_R": target_r}))
                lines.append(fmt(name, st))
    longs = [t for t in results[0][2] if t.side == "buy"]
    shorts = [t for t in results[0][2] if t.side == "sell"]
    lines.append(f"    (paper settings, longs only: {fmt('', stats(longs)).strip()})")
    lines.append(f"    (paper settings, shorts only: {fmt('', stats(shorts)).strip()})")
    lines.append(f"    stocks picked per day: {avg_picks:.1f} of {cfg.orb_top_n} wanted")
    lines.append("")
    # --- EMA for comparison ---
    lines.append("Original EMA-cross rules (stop 1.5 ATR, score 5), for comparison:")
    spy = frames.get("SPY")
    in_play_days = {}
    ranges = {s: opening_ranges(df, 5, cfg.bar_minutes) for s, df in frames.items()}
    for d in sorted({d for r in ranges.values() for d in r}):
        for sym, _ in pick_in_play({s: r[d] for s, r in ranges.items() if d in r}, _SelCfg(cfg, cfg.orb_top_n)):
            in_play_days.setdefault(sym, set()).add(d)
    for rr, label in ((1.0, "target 1R"), (2.0, "target 2R")):
        t_play = since([t for s in frames for t in ema_trades(s, frames[s], spy, cfg, cost, 1.5, rr, 5,
                                                              in_play_days.get(s, set()))], cutoff)
        t_fixed = since([t for s in cfg.stocks if s in frames
                         for t in ema_trades(s, frames[s], spy, cfg, cost, 1.5, rr, 5)], cutoff)
        st_play, st_fixed = stats(t_play), stats(t_fixed)
        results.append((f"EMA score5 {label} | stocks in play", st_play, t_play,
                        {"STRATEGY": "ema", "MIN_SIGNAL_SCORE": 5, "STOP_ATR_MULT": 1.5, "REWARD_RISK": rr}))
        results.append((f"EMA score5 {label} | fixed list", st_fixed, t_fixed,
                        {"STRATEGY": "ema", "MIN_SIGNAL_SCORE": 5, "STOP_ATR_MULT": 1.5, "REWARD_RISK": rr,
                         "SCAN_MOVERS": "false"}))
        lines.append(fmt(f"EMA score5 {label} | stocks in play", st_play))
        lines.append(fmt(f"EMA score5 {label} | fixed list", st_fixed))
    lines.append("")
    # --- verdict ---
    good = [r for r in results if robust(r[1])]
    lines.append(f"Robust (40+ trades, avg > +0.03R net, profitable in both halves, PF >= 1.1): {len(good)} of {len(results)}")
    if not good:
        lines.append("  none. No tested rule set beat costs reliably on this data: don't trade the main bot's stocks yet.")
        return lines
    good.sort(key=lambda r: (r[1]["avg"] * min(r[1]["n"], 300) ** 0.5), reverse=True)   # edge x confidence
    for name, st, _, _ in good[:6]:
        lines.append(fmt(name, st))
    name, st, trades, env = good[0]
    lines += ["", f"RECOMMENDED: {name}",
              f"  Over these {days} days, with no leverage, that would have been about:"]
    for acct, risk, cap, label in ((88000, 1.0, 12.0, "$88k paper account, 1% risk, 12% position cap"),
                                   (1000, 1.0, 100.0, "$1,000 cash account, 1% risk, 1 position at a time")):
        lines.append(f"    {dollars(trades, acct, risk, cap):+,.0f} USD on the {label}")
    lines.append("  Caveats: no AI check, ideal fills at the breakout price plus the cost above, and the best of "
                 f"{len(results)} rule sets is always a bit lucky. Expect less live.")
    lines.append("  To switch, put these in .env on the Pi, then run botrestart:")
    for k, v in env.items():
        lines.append(f"    {k}={v}")
    return lines


# ============================ data ============================
DEFAULT_UNIVERSE = (
    "SPY QQQ TSLA NVDA AMD AAPL MSFT META AMZN GOOGL NFLX AVGO MU INTC PLTR COIN MSTR SMCI MARA RIOT CLSK "
    "SOFI HOOD RIVN NIO LCID UBER SHOP PYPL ROKU SNAP ARM CRWD SNOW DKNG PLUG AFRM UPST CVNA GME AMC BA DIS "
    "BABA PDD XPEV LI F GM CCL AAL UAL DAL OXY XOM LLY PFE MRNA NVAX ENPH FSLR RUN XYZ IONQ RGTI QUBT SOUN "
    "BBAI APLD RKLB ASTS LUNR ACHR JOBY NBIS CRWV OKLO SMR VST CEG TTD NET DDOG MDB ZS PANW ORCL CRM ADBE "
    "QCOM TSM AMAT LRCX MRVL ON WDC STX DELL HPE IBM WOLF HUT BITF TEM RDDT DJT TTWO EA U RBLX").split()


def fetch(cfg, symbols, days):
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from .market import _split
    client = StockHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)
    start = datetime.now(timezone.utc) - timedelta(days=days + 25)    # +25: warm-up for 14-day ATR/volume
    frames = {}
    for i in range(0, len(symbols), 25):
        chunk = symbols[i:i + 25]
        try:
            df = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=chunk, timeframe=TimeFrame(cfg.bar_minutes, TimeFrameUnit.Minute),
                start=start, feed=DataFeed.IEX)).df
            frames.update(_split(df))
        except Exception as e:
            print(f"  (skipping {chunk[0]}..{chunk[-1]}: {e})", flush=True)
        print(f"  {min(i + 25, len(symbols))}/{len(symbols)} symbols", flush=True)
    return frames


def cutoff_for(days):
    """Keep the warm-up out of the scored period: trades only in the last `days` days."""
    return pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=days)


def main():
    from .config import Config
    cfg = Config()
    days = int(os.getenv("DAYS", "90"))
    cost = float(os.getenv("BACKTEST_COST_PCT", "0.05"))
    universe = os.getenv("BACKTEST_UNIVERSE", "").split() or DEFAULT_UNIVERSE
    universe = list(dict.fromkeys(universe + list(cfg.stocks) + ["SPY"]))
    print(f"Downloading {days} days (+25 warm-up) of 5-min bars for {len(universe)} symbols...", flush=True)
    frames = fetch(cfg, universe, days)
    frames = {s: df for s, df in frames.items() if len(df) > 500}
    # warm-up bars stay in the frame (ATR/relative volume need them) but no trade may start before the cutoff
    lines = report(frames, cfg, days, cost, cutoff_for(days))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
