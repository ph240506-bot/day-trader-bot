"""Early-mover study: do chart markers spot small-cap runners BEFORE the move, more often than they cry wolf?

Run on the Pi (it runs in the background, ~20-40 min):   botstudy          (last 120 days)
                                                         DAYS=180 botstudy
Read the result when it's done:                          botstudyresult

How it works
  1. Every small cap ($1-$20, normally trades 100k+ shares a day, not a fund) on every day of the period.
  2. "Movers" = days the stock rose 30%+ above the previous close at some point.
  3. Minute-by-minute history for ALL movers plus a random sample of the other small caps (the sample is
     re-weighted to stand for all of them, so false alarms are counted at their true rate).
  4. Four markers, each using only what was known at that minute:
       volume_before_price  5 minutes of 10x normal volume while the stock is still up less than 10%
       premarket_high_break gapped up 5%+ before the open, then breaks the pre-market high after 9:30
       vwap_reclaim         up 5%+ on the day, below VWAP for 10+ minutes, then closes back above it
       tight_range_breakout 15 minutes inside a 2% band, then breaks out on double the volume
  5. For each signal: was it early on a real mover, how often was it a false alarm, and what would a trade
     have made: buy the next minute, stop 4% (min 4 cents) below, target +8%, out after 30 min, 0.5% costs.

Data: Alpaca's historical whole-market (SIP) feed, free when more than 15 minutes old. Regular-hours signals
only (the bot can't get reliable pre-market prices). Delisted stocks are missing (survivorship bias).
"""
import os
import random
import time
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")
MARKERS = ["volume_before_price", "premarket_high_break", "vwap_reclaim", "tight_range_breakout",
           # popular small-cap day-trader setups (rules from the setups traders teach, e.g. Warrior-style toolkits)
           "red_to_green", "one_min_orb", "bull_flag", "flat_top_breakout", "micro_pullback", "hod_break",
           "whole_dollar_break", "print_surge", "day2_continuation", "first_green_day"]
MOVE = 0.30            # a "mover" day: high at least 30% above the previous close
EARLY = 0.10           # a signal counts as early if the stock was still up less than 10% when it fired
COST_PCT = 0.5         # round-trip spread + slippage on a small cap
STOP_PCT, STOP_CENTS, TARGET_PCT, HOLD_MIN = 4.0, 0.04, 8.0, 30


# ============================ markers (pure functions) ============================
def _t(h, m):
    return dtime(h, m)


def detect_markers(m1: pd.DataFrame, prev_close: float, avg_daily_vol: float, ctx: dict | None = None) -> dict[str, int]:
    """m1 = one stock-day of 1-minute bars (NY time index, 04:00-16:00). Returns {marker: bar index} for the
    FIRST bar each marker fired on (regular hours only). Uses only bars up to and including that bar."""
    if m1.empty or prev_close <= 0 or avg_daily_vol <= 0:
        return {}
    tod = np.array([t.time() for t in m1.index])
    o, h, l, c, v = (m1[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    chg = c / prev_close - 1
    reg = (tod >= _t(9, 30)) & (tod < _t(16, 0))
    pre = (tod >= _t(4, 0)) & (tod < _t(9, 30))
    out: dict[str, int] = {}
    n = len(m1)

    # 1. volume before price
    norm5 = avg_daily_vol / 390 * 5
    roll5 = pd.Series(v).rolling(5, min_periods=5).sum().to_numpy()
    for i in range(n):
        if reg[i] and _t(9, 31) <= tod[i] < _t(15, 30) and roll5[i] >= 10 * norm5 and 0 < chg[i] < EARLY:
            out["volume_before_price"] = i
            break

    # 2. pre-market high break
    if pre.any():
        pm_idx = np.where(pre)[0]
        pm_high, pm_last = h[pm_idx].max(), c[pm_idx[-1]]
        if pm_last / prev_close - 1 >= 0.05:
            for i in range(n):
                if reg[i] and tod[i] < _t(11, 0) and c[i] > pm_high:
                    out["premarket_high_break"] = i
                    break

    # 3. VWAP reclaim (session VWAP from 9:30)
    ri = np.where(reg)[0]
    if len(ri):
        tp = (h[ri] + l[ri] + c[ri]) / 3
        cv = np.cumsum(v[ri])
        vwap = np.where(cv > 0, np.cumsum(tp * v[ri]) / np.maximum(cv, 1e-9), tp)
        below = 0
        for k, i in enumerate(ri):
            if tod[i] >= _t(15, 0):
                break
            if c[i] < vwap[k]:
                below += 1
                continue
            if below >= 10 and tod[i] >= _t(9, 40) and chg[i] >= 0.05:
                out["vwap_reclaim"] = i
                break
            below = 0

    # 4. tight range, then breakout on volume
    for i in range(15, n):
        if not (reg[i] and _t(9, 45) <= tod[i] < _t(15, 0)) or not reg[i - 15]:
            continue
        hi, lo = h[i - 15:i].max(), l[i - 15:i].min()
        if c[i] <= 0 or (hi - lo) / c[i] > 0.02:
            continue
        if c[i] > hi and v[i] >= 2 * max(v[i - 15:i].mean(), 1) and chg[i] >= 0:
            out["tight_range_breakout"] = i
            break

    out.update(_setup_markers(m1, tod, reg, o, h, l, c, v, chg, prev_close, ctx or {}))
    return out


def _first(n, cond):
    for i in range(n):
        if cond(i):
            return i
    return None


def _setup_markers(m1, tod, reg, o, h, l, c, v, chg, prev_close, ctx) -> dict[str, int]:
    """The setups small-cap day traders teach, as exact rules. Each fires at most once a day, on the first bar
    that qualifies, and looks only at bars up to and including that bar."""
    n, out = len(m1), {}
    green = c > o
    ri = np.where(reg)[0]
    if not len(ri):
        return out
    first = ri[0]
    in_hours = lambda i, a=(9, 31), b=(15, 0): reg[i] and _t(*a) <= tod[i] < _t(*b)

    # red to green: opened below yesterday's close, then crosses above it (before 11:00)
    if o[first] < prev_close:
        i = _first(n, lambda i: i > first and in_hours(i, (9, 31), (11, 0)) and c[i] > prev_close)
        if i is not None:
            out["red_to_green"] = i

    # 1-minute opening range breakout: gapped up 3%+, then closes above the first minute's high (by 10:30)
    if o[first] / prev_close - 1 >= 0.03:
        orh = h[first]
        i = _first(n, lambda i: i > first and in_hours(i, (9, 31), (10, 30)) and c[i] > orh)
        if i is not None:
            out["one_min_orb"] = i

    # bull flag: 3+ green bars (the pole, up 3%+), 2-5 red bars retracing at most half, then a break of the flag high
    for i in range(8, n):
        if not (in_hours(i, (9, 35)) and chg[i] >= 0.05 and green[i]):
            continue
        done = False
        for p_len in range(2, 6):
            fs, fe = i - p_len, i - 1                                   # flag bars
            if fs - 3 < 0 or not all(not green[k] for k in range(fs, fe + 1)):
                continue
            ps = fs - 1
            while ps - 1 >= 0 and green[ps - 1]:
                ps -= 1
            if fs - ps < 3:
                continue
            pole_lo, pole_hi = l[ps], h[ps:fs].max()
            if pole_hi / max(pole_lo, 1e-9) - 1 < 0.03:
                continue
            flag_lo, flag_hi = l[fs:fe + 1].min(), h[fs:fe + 1].max()
            if (pole_hi - flag_lo) > 0.5 * (pole_hi - pole_lo):
                continue
            if c[i] > flag_hi:
                out["bull_flag"], done = i, True
                break
        if done:
            break

    # flat top breakout: 2+ touches of the same high (within 0.3%) in the last 10 bars, then a close above it
    for i in range(10, n):
        if not (in_hours(i, (9, 40)) and chg[i] >= 0.05):
            continue
        top = h[i - 10:i].max()
        touches = int((h[i - 10:i] >= top * 0.997).sum())
        if touches >= 2 and c[i] > top and v[i] > 1.5 * max(v[i - 10:i].mean(), 1):
            out["flat_top_breakout"] = i
            break

    # micro pullback: 3+ green bars, 1-3 red bars retracing at most half, a green bar breaks the pullback high
    for i in range(6, n):
        if not (in_hours(i, (9, 35)) and chg[i] >= 0.10 and green[i]):
            continue
        hit = False
        for p_len in range(1, 4):
            fs, fe = i - p_len, i - 1
            if fs - 3 < 0 or not all(not green[k] for k in range(fs, fe + 1)):
                continue
            if not all(green[k] for k in range(fs - 3, fs)):
                continue
            run_lo, run_hi = l[fs - 3], h[fs - 3:fs].max()
            if run_hi - l[fs:fe + 1].min() > 0.5 * (run_hi - run_lo):
                continue
            if c[i] > h[fs:fe + 1].max():
                out["micro_pullback"], hit = i, True
                break
        if hit:
            break

    # high-of-day break: a new high of the day after 15+ minutes without one, on a stock up 10%+
    hod, hod_at = h[first], first
    for i in range(first + 1, n):
        if not reg[i]:
            continue
        if h[i] > hod:
            if in_hours(i, (9, 45)) and chg[i] >= 0.10 and i - hod_at >= 15 and c[i] > hod:
                out["hod_break"] = i
                break
            hod, hod_at = h[i], i

    # whole / half dollar break: closes through a .00 or .50 level from below, on a stock up 5%+
    for i in range(first + 1, n):
        if in_hours(i, (9, 35)) and chg[i] >= 0.05 and c[i] >= 1:
            level = np.floor(c[i] * 2) / 2
            if c[i - 1] < level <= c[i]:
                out["whole_dollar_break"] = i
                break

    # print surge: 5 minutes with 10x the normal number of trades, while the stock is still up less than 10%
    tc = m1["trade_count"].to_numpy(float) if "trade_count" in m1 else None
    avg_trades = ctx.get("avg_daily_trades") or 0
    if tc is not None and avg_trades > 0:
        roll = pd.Series(tc).rolling(5, min_periods=5).sum().to_numpy()
        norm = avg_trades / 390 * 5
        i = _first(n, lambda i: in_hours(i, (9, 31), (15, 30)) and roll[i] >= 10 * norm and 0 < chg[i] < EARLY)
        if i is not None:
            out["print_surge"] = i

    # day-2 continuation: yesterday ran 30%+, today breaks yesterday's high (by 11:00)
    if ctx.get("prev_gain", 0) >= MOVE and ctx.get("prev_high"):
        i = _first(n, lambda i: in_hours(i, (9, 31), (11, 0)) and c[i] > ctx["prev_high"])
        if i is not None:
            out["day2_continuation"] = i

    # first green day: 3+ red days down 15%+, today turns up 3% (by noon)
    if ctx.get("red_days", 0) >= 3 and ctx.get("drop", 0) >= 0.15:
        i = _first(n, lambda i: in_hours(i, (9, 31), (12, 0)) and chg[i] >= 0.03)
        if i is not None:
            out["first_green_day"] = i
    return out


def simulate(m1: pd.DataFrame, i: int) -> dict | None:
    """Buy at the next bar's open; stop 4% (min 4 cents) below, target +8%, out after 30 min or at 15:55.
    Stop checked before target in the same bar (the cautious assumption). Returns % results, costs taken off."""
    e = i + 1
    if e >= len(m1) or m1.index[e].time() >= _t(15, 55):
        return None
    o, h, l, c = (m1[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    entry = o[e]
    if entry <= 0:
        return None
    stop = min(entry * (1 - STOP_PCT / 100), entry - STOP_CENTS)
    target = entry * (1 + TARGET_PCT / 100)
    last = min(len(m1) - 1, e + HOLD_MIN)
    exit_px, why = None, "time"
    for j in range(e, last + 1):
        if m1.index[j].time() >= _t(15, 55):
            exit_px, why = o[j], "close"
            break
        if l[j] <= stop:
            exit_px, why = (min(stop, o[j]) if j > e else stop), "stop"
            break
        if h[j] >= target:
            exit_px, why = target, "target"
            break
    if exit_px is None:
        exit_px = c[last]
    window = slice(e, min(len(m1), e + HOLD_MIN + 1))
    return {"net_pct": (exit_px / entry - 1) * 100 - COST_PCT, "exit": why,
            "mfe_pct": (h[window].max() / entry - 1) * 100, "mae_pct": (l[window].min() / entry - 1) * 100}


def edge_check(traded: pd.DataFrame, half_day) -> tuple[float, float, float, float]:
    """Weighted average result, how many standard errors it sits from zero, and the average in each half of
    the period. With 14 markers tested, one can look good by luck: only trust t above ~3 AND both halves > 0."""
    w = traded["weight"].to_numpy(float)
    x = traded["net_pct"].to_numpy(float)
    mean = float((w * x).sum() / w.sum())
    var = float((w * (x - mean) ** 2).sum() / w.sum())
    n_eff = w.sum() ** 2 / (w ** 2).sum()
    t = mean / (np.sqrt(var / n_eff) + 1e-12)
    halves = []
    for part in (traded[traded["day"] <= half_day], traded[traded["day"] > half_day]):
        pw = part["weight"].to_numpy(float)
        halves.append(float((pw * part["net_pct"]).sum() / pw.sum()) if pw.sum() > 0 else float("nan"))
    return mean, t, halves[0], halves[1]


def summarize(events: pd.DataFrame, n_days: int, movers_total: int) -> list[str]:
    """events: one row per (stock-day, marker) with weight, is_mover, early, net_pct, ..."""
    lines = []
    if events.empty:
        return ["  No signals at all in the sample."]
    for mk in MARKERS:
        ev = events[events["marker"] == mk]
        if ev.empty:
            lines.append(f"  {mk:<22} never fired")
            continue
        w = ev["weight"].to_numpy(float)
        per_day = w.sum() / max(n_days, 1)
        precision = (w * ev["is_mover"]).sum() / w.sum() * 100
        caught = ev[(ev["is_mover"]) & (ev["early"])]["key"].nunique()
        recall = caught / max(movers_total, 1) * 100
        traded = ev.dropna(subset=["net_pct"])
        verdict = ""
        if len(traded):
            tw = traded["weight"].to_numpy(float)
            avg = float((tw * traded["net_pct"]).sum() / tw.sum())
            win = float((tw * (traded["net_pct"] > 0)).sum() / tw.sum() * 100)
            if "day" in traded and len(traded) >= 30:
                days_sorted = sorted(events["day"].unique())
                _, t, h1, h2 = edge_check(traded, days_sorted[len(days_sorted) // 2 - 1])
                verdict = f" | sure? t={t:+.1f}, halves {h1:+.2f}/{h2:+.2f}"
                if avg > 0.1 and t > 3 and h1 > 0 and h2 > 0:
                    verdict += "  <<< WORTH A CLOSER LOOK"
        else:
            avg = win = float("nan")
        lines.append(f"  {mk:<22} fires ~{per_day:5.1f}/day | {precision:4.1f}% were real movers | "
                     f"caught {recall:4.1f}% of movers early | trade: win {win:3.0f}%, avg {avg:+.2f}% after costs "
                     f"({len(ev)} signals sampled){verdict}")
    return lines


# ============================ data ============================
def _split_bars(df) -> dict[str, pd.DataFrame]:
    """Like market._split, but keeps the trade count (number of prints) when Alpaca sends it."""
    if df is None or df.empty:
        return {}
    cols = [k for k in ("open", "high", "low", "close", "volume", "trade_count") if k in df.columns]
    out = {}
    for sym in df.index.get_level_values(0).unique():
        d = df.xs(sym, level=0)[cols].astype(float)
        d.index = pd.to_datetime(d.index).tz_convert("America/New_York")
        out[sym] = d
    return out


def _daily_frame(df):
    out = {}
    for sym, d in _split_bars(df).items():
        d = d.copy()
        d.index = [t.date() for t in d.index]
        out[sym] = d
    return out


def run_study(days: int, out_dir: str, sample_per_day: int = 40, seed: int = 7) -> list[str]:
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest
    from .config import Config
    from .funds import looks_like_fund
    from .market import _split
    from .smallcap.catalysts import is_common_stock_symbol

    cfg = Config()
    rng = random.Random(seed)
    data = StockHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)
    trading = TradingClient(cfg.alpaca_key, cfg.alpaca_secret, paper=cfg.paper)
    end = datetime.now(timezone.utc) - timedelta(minutes=20)       # SIP is free once it's 15+ minutes old
    start = end - timedelta(days=days + 45)                        # +45: 20-day average volume warm-up
    log = lambda s: print(s, flush=True)

    feed = DataFeed.SIP
    try:
        data.get_stock_bars(StockBarsRequest(symbol_or_symbols="AAPL", timeframe=TimeFrame.Day,
                                             start=end - timedelta(days=5), end=end, feed=feed))
    except Exception as e:
        log(f"WARNING: whole-market history refused ({str(e)[:80]}). Falling back to IEX: small-cap results "
            "will be much less reliable.")
        feed = DataFeed.IEX

    assets = trading.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY))
    major = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"}
    syms = [a.symbol for a in assets if a.tradable and is_common_stock_symbol(a.symbol)
            and str(getattr(a.exchange, "value", a.exchange)).upper() in major
            and not looks_like_fund(getattr(a, "name", ""))]
    log(f"Universe: {len(syms)} common stocks. Downloading daily bars ({feed.value})...")

    daily = {}
    for k in range(0, len(syms), 200):
        chunk = syms[k:k + 200]
        try:
            df = data.get_stock_bars(StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                                                      start=start, end=end, feed=feed)).df
            daily.update(_daily_frame(df))
        except Exception as e:
            log(f"  skipped {chunk[0]}..{chunk[-1]}: {str(e)[:80]}")
        if (k // 200) % 5 == 0:
            log(f"  daily bars: {min(k + 200, len(syms))}/{len(syms)}")
        time.sleep(0.4)

    # per day: the small-cap population, and which of them were movers
    cutoff = (end - timedelta(days=days)).astimezone(NY).date()
    by_day: dict = {}
    for sym, d in daily.items():
        if len(d) < 25:
            continue
        prev_close = d["close"].shift()
        avg_vol = d["volume"].shift().rolling(20, min_periods=15).mean()
        avg_tr = (d["trade_count"].shift().rolling(20, min_periods=15).mean() if "trade_count" in d
                  else pd.Series(0.0, index=d.index))
        prev_high, prev_gain = d["high"].shift(), d["high"].shift() / d["close"].shift(2) - 1
        red = (d["close"] < d["open"]).astype(int)
        red_run = red.groupby((red == 0).cumsum()).cumsum().shift().fillna(0)
        drop3 = 1 - d["close"].shift() / d["close"].shift(4)
        for k, (day, pc, av, hi) in enumerate(zip(d.index, prev_close, avg_vol, d["high"])):
            if day < cutoff or not (pc and av) or np.isnan(pc) or np.isnan(av):
                continue
            if not (cfg_min_price <= pc <= cfg_max_price) or av < 100_000:
                continue
            ctx = {"avg_daily_trades": float(avg_tr.iloc[k] or 0) if not np.isnan(avg_tr.iloc[k]) else 0.0,
                   "prev_high": float(prev_high.iloc[k]), "prev_gain": float(np.nan_to_num(prev_gain.iloc[k])),
                   "red_days": int(red_run.iloc[k]), "drop": float(np.nan_to_num(drop3.iloc[k]))}
            by_day.setdefault(day, {"movers": [], "others": []})[
                "movers" if hi / pc - 1 >= MOVE else "others"].append((sym, float(pc), float(av), ctx))
    days_list = sorted(by_day)
    movers_total = sum(len(v["movers"]) for v in by_day.values())
    log(f"{len(days_list)} trading days, {movers_total} mover-days. Downloading minute bars...")

    rows = []
    for n_day, day in enumerate(days_list, 1):
        pop = by_day[day]
        sample = rng.sample(pop["others"], min(sample_per_day, len(pop["others"])))
        weight_other = len(pop["others"]) / max(len(sample), 1)
        picks = [(s, pc, av, cx, True, 1.0) for s, pc, av, cx in pop["movers"]] + \
                [(s, pc, av, cx, False, weight_other) for s, pc, av, cx in sample]
        t0 = datetime.combine(day, dtime(4, 0), NY)
        t1 = datetime.combine(day, dtime(16, 0), NY)
        mins = {}
        for k in range(0, len(picks), 60):
            group = [p[0] for p in picks[k:k + 60]]
            try:
                df = data.get_stock_bars(StockBarsRequest(symbol_or_symbols=group, timeframe=TimeFrame.Minute,
                                                          start=t0, end=t1, feed=feed)).df
                mins.update(_split_bars(df))
            except Exception as e:
                log(f"  {day}: minute bars failed ({str(e)[:60]})")
            time.sleep(0.4)
        for sym, pc, av, cx, is_mover, w in picks:
            m1 = mins.get(sym)
            if m1 is None or len(m1) < 30:
                continue
            for mk, i in detect_markers(m1, pc, av, cx).items():
                sim = simulate(m1, i) or {}
                rows.append({"day": day, "symbol": sym, "key": f"{sym}|{day}", "marker": mk, "is_mover": is_mover,
                             "weight": w, "time": m1.index[i].strftime("%H:%M"),
                             "chg_at_signal": round((m1["close"].iloc[i] / pc - 1) * 100, 1),
                             "early": m1["close"].iloc[i] / pc - 1 < EARLY, **sim})
        if n_day % 10 == 0 or n_day == len(days_list):
            log(f"  {n_day}/{len(days_list)} days done, {len(rows)} signals so far")

    events = pd.DataFrame(rows)
    os.makedirs(out_dir, exist_ok=True)
    if not events.empty:
        events.to_csv(os.path.join(out_dir, "study_events.csv"), index=False)
    head = [f"Early-mover study: {len(days_list)} trading days, {movers_total} days a small cap ran {MOVE:.0%}+ "
            f"({movers_total / max(len(days_list), 1):.1f} a day). Data: {feed.value}.",
            f"Trade test: buy next minute, stop {STOP_PCT:.0f}% (min {STOP_CENTS * 100:.0f}c), target +{TARGET_PCT:.0f}%, "
            f"out after {HOLD_MIN} min, {COST_PCT}% costs.", ""]
    return head + summarize(events, len(days_list), movers_total) + [
        "", "How to read it: 'real movers' = how often a signal was followed by a 30%+ day (the rest are false",
        "alarms). 'avg after costs' must be clearly above 0 for a marker to be worth adding to the bot.",
        "'sure?': t is how far the average sits from zero in units of luck. With 14 markers tested, expect one to",
        "reach about t=2 by chance alone, so only t above 3 with BOTH halves of the period positive counts."]


cfg_min_price, cfg_max_price = 1.0, 20.0


def main():
    days = int(os.getenv("DAYS", "120"))
    out_dir = os.getenv("STUDY_DIR", "journal")
    started = time.time()
    lines = run_study(days, out_dir)
    lines.append(f"(took {(time.time() - started) / 60:.0f} min)")
    text = "\n".join(lines)
    print(text, flush=True)
    with open(os.path.join(out_dir, "study_report.txt"), "w") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
