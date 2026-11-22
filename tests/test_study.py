"""Early-mover study: markers fire when they should, never look ahead, and the trade test is honest."""
import numpy as np
import pandas as pd
import pytest

from bot.study import detect_markers, simulate, summarize


def day(prices, vols=None, start="2026-11-02 04:00"):
    """1-minute bars from a list of closes (open = previous close, high/low a hair around)."""
    idx = pd.date_range(start, periods=len(prices), freq="1min", tz="America/New_York")
    c = np.array(prices, float)
    o = np.r_[c[0], c[:-1]]
    v = np.full(len(c), 100.0) if vols is None else np.array(vols, float)
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.0005, "low": np.minimum(o, c) * 0.9995,
                         "close": c, "volume": v}, index=idx)


PM = 330          # bars from 04:00 to 09:29
REG = 390         # 09:30 to 15:59


def test_volume_before_price_fires_while_still_cheap():
    prices = [2.0] * PM + [2.04] * REG                         # +2% on the day
    vols = [10] * PM + [100] * REG
    vols[PM + 20:PM + 25] = [20_000] * 5                       # avg day 39k -> normal 5 min = 500; this is 100k
    out = detect_markers(day(prices, vols), prev_close=2.0, avg_daily_vol=39_000)
    assert out["volume_before_price"] == PM + 20               # first heavy minute already beats 10x pace


def test_volume_before_price_ignores_stocks_already_up_a_lot():
    prices = [2.0] * PM + [2.4] * REG                          # already +20%: not "before the move"
    vols = [10] * PM + [100] * REG
    vols[PM + 20:PM + 25] = [20_000] * 5
    assert "volume_before_price" not in detect_markers(day(prices, vols), 2.0, 39_000)


def test_premarket_high_break():
    pm = [2.0] * 200 + [2.20] * (PM - 200)                     # gapped to +10% pre-market, high ~2.201
    reg = [2.15] * 30 + [2.25] * (REG - 30)                    # dips at the open, breaks out at 10:00
    out = detect_markers(day(pm + reg), 2.0, 500_000)
    assert out["premarket_high_break"] == PM + 30
    flat_pm = [2.0] * PM + reg                                 # no pre-market gap: no signal
    assert "premarket_high_break" not in detect_markers(day(flat_pm), 2.0, 500_000)


def test_vwap_reclaim():
    reg = [2.30] * 10 + [2.15] * 20 + [2.40] * (REG - 30)      # high, below VWAP for 20 min, reclaims at 10:00
    out = detect_markers(day([2.0] * PM + reg), 2.0, 500_000)
    assert out["vwap_reclaim"] == PM + 30


def test_tight_range_breakout():
    reg = [2.10] * 40 + [2.20] + [2.20] * (REG - 41)
    vols = [10] * PM + [100] * REG
    vols[PM + 40] = 500                                        # breakout minute on 5x volume
    out = detect_markers(day([2.0] * PM + reg, vols), 2.0, 5_000_000)
    assert out["tight_range_breakout"] == PM + 40
    vols[PM + 40] = 120                                        # same breakout, no volume: ignored
    assert "tight_range_breakout" not in detect_markers(day([2.0] * PM + reg, vols), 2.0, 5_000_000)


def test_markers_never_use_future_bars():
    """Cut the day off right after each signal: the same signal must fire at the same minute."""
    rng = np.random.default_rng(3)
    for seed in range(15):
        p = 2.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.004, PM + REG)))
        vols = rng.lognormal(5, 1.2, PM + REG)
        full = day(p, vols)
        for mk, i in detect_markers(full, 2.0, 50_000).items():
            cut = detect_markers(full.iloc[:i + 1], 2.0, 50_000)
            assert cut.get(mk) == i, (seed, mk)


def test_trade_test_stop_target_and_costs():
    base = [2.0] * PM + [2.00] * 5
    win = day(base + [2.00, 2.05, 2.10, 2.20] + [2.20] * 300)   # buy 2.00 next minute, +8% at 2.16
    r = simulate(win, PM + 4)
    assert r["exit"] == "target" and r["net_pct"] == pytest.approx(8.0 - 0.5)
    lose = day(base + [2.00, 1.95, 1.85] + [1.85] * 300)        # stop = 2.00*0.96 = 1.92
    r = simulate(lose, PM + 4)
    assert r["exit"] == "stop" and r["net_pct"] < -4.0
    flat = day(base + [2.00] * 300)
    r = simulate(flat, PM + 4)
    assert r["exit"] == "time" and r["net_pct"] == pytest.approx(-0.5)
    cheap = day([1.0] * PM + [1.0] * 5 + [1.0, 0.98, 0.97] + [0.97] * 300)
    assert simulate(cheap, PM + 4)["exit"] == "time"           # 3-cent dip on a $1 stock: stop is 4 cents


def test_summary_weights_the_sampled_non_movers():
    ev = pd.DataFrame([
        {"marker": "vwap_reclaim", "key": "A|1", "is_mover": True, "early": True, "weight": 1.0, "net_pct": 5.0},
        {"marker": "vwap_reclaim", "key": "B|1", "is_mover": False, "early": True, "weight": 9.0, "net_pct": -1.0},
    ])
    line = [l for l in summarize(ev, n_days=1, movers_total=2) if "vwap_reclaim" in l][0]
    assert "fires ~ 10.0/day" in line                         # 1 mover + 1 sampled standing for 9 others
    assert "10.0% were real movers" in line and "caught 50.0% of movers early" in line
    assert "avg -0.40%" in line                               # (5*1 - 1*9) / 10


# ---------------- trader setups ----------------
def setup_day(reg_prices, open_first=None, pm=None, trades=None):
    """Regular-session bars only matter here; pre-market is flat at the first price unless given."""
    pm = pm if pm is not None else [reg_prices[0]] * PM
    df = day(pm + reg_prices)
    if open_first is not None:
        df.iloc[PM, df.columns.get_loc("open")] = open_first
    if trades is not None:
        df["trade_count"] = np.array(trades, float)
    return df


def test_red_to_green():
    reg = [1.95] * 10 + [2.02] * (REG - 10)                    # opened red at 1.95, crosses 2.00 at 9:40
    out = detect_markers(setup_day(reg, open_first=1.95), 2.0, 500_000)
    assert out["red_to_green"] == PM + 10
    assert "red_to_green" not in detect_markers(setup_day([2.05] * REG, open_first=2.05), 2.0, 500_000)  # opened green


def test_one_min_orb_needs_a_gap():
    reg = [2.20] + [2.19] * 9 + [2.30] * (REG - 10)
    df = setup_day(reg, open_first=2.15)                        # gapped +7.5%
    assert detect_markers(df, 2.0, 500_000)["one_min_orb"] == PM + 10
    reg2 = [2.02] + [2.01] * 9 + [2.10] * (REG - 10)
    assert "one_min_orb" not in detect_markers(setup_day(reg2, open_first=2.01), 2.0, 500_000)   # no gap


def test_bull_flag_and_micro_pullback():
    pole = [2.30, 2.36, 2.42, 2.48]                             # 3 green bars up from 2.30
    flag = [2.46, 2.44]                                         # 2 red bars, shallow
    reg = [2.30] * 10 + pole[1:] + flag + [2.52] + [2.52] * (REG - 16)
    df = setup_day(reg)
    df.iloc[PM + 10, df.columns.get_loc("open")] = 2.28         # make the first pole bar green
    out = detect_markers(df, 2.0, 500_000)
    assert out["bull_flag"] == PM + 15                          # breaks the flag high at the green bar
    assert out["micro_pullback"] == PM + 15


def test_bull_flag_rejects_deep_pullbacks():
    reg = [2.30] * 10 + [2.36, 2.42, 2.48] + [2.40, 2.32] + [2.52] + [2.52] * (REG - 16)   # gave back ~90%
    df = setup_day(reg)
    df.iloc[PM + 10, df.columns.get_loc("open")] = 2.28
    assert "bull_flag" not in detect_markers(df, 2.0, 500_000)


def test_flat_top_hod_and_whole_dollar():
    reg = [2.30] * 10 + [2.40, 2.35, 2.40, 2.36, 2.40, 2.37, 2.38, 2.39, 2.38, 2.39] + [2.45] + [2.45] * (REG - 21)
    vols = [10] * PM + [100] * REG
    vols[PM + 20] = 400
    df = day([2.30] * PM + reg, vols)
    out = detect_markers(df, 2.0, 500_000)
    assert out["flat_top_breakout"] == PM + 20
    hod = [2.30] * 5 + [2.40] + [2.35] * 20 + [2.45] + [2.45] * (REG - 27)      # new high 21 min after the last
    assert detect_markers(setup_day(hod), 2.0, 500_000)["hod_break"] == PM + 26
    wd = [2.40] * 10 + [2.45, 2.52] + [2.52] * (REG - 12)                       # crosses $2.50
    assert detect_markers(setup_day(wd), 2.0, 500_000)["whole_dollar_break"] == PM + 11


def test_print_surge_needs_trade_counts():
    reg = [2.04] * REG
    trades = [1] * PM + [20] * REG
    trades[PM + 30:PM + 35] = [3000] * 5                        # 7,800 trades a day = 100 per 5 min normally
    out = detect_markers(setup_day(reg, trades=trades), 2.0, 500_000, {"avg_daily_trades": 7_800})
    assert out["print_surge"] == PM + 30
    assert "print_surge" not in detect_markers(setup_day(reg), 2.0, 500_000, {"avg_daily_trades": 7_800})


def test_day2_and_first_green_day_use_yesterday():
    reg = [2.50] * 20 + [2.70] + [2.70] * (REG - 21)
    ctx = {"prev_gain": 0.45, "prev_high": 2.60}
    assert detect_markers(setup_day(reg), 2.0, 500_000, ctx)["day2_continuation"] == PM + 20
    assert "day2_continuation" not in detect_markers(setup_day(reg), 2.0, 500_000, {"prev_gain": 0.1, "prev_high": 2.6})
    fg = [1.98] * 15 + [2.07] + [2.07] * (REG - 16)
    ctx = {"red_days": 4, "drop": 0.25}
    assert detect_markers(setup_day(fg), 2.0, 500_000, ctx)["first_green_day"] == PM + 15
    assert "first_green_day" not in detect_markers(setup_day(fg), 2.0, 500_000, {"red_days": 1, "drop": 0.25})


def test_no_marker_ever_peeks_ahead():
    """All 14: cut the day right after a signal, and the same signal must still fire at the same minute."""
    rng = np.random.default_rng(11)
    ctx = {"avg_daily_trades": 40_000, "prev_gain": 0.5, "prev_high": 2.05, "red_days": 4, "drop": 0.3}
    fired = set()
    for seed in range(40):
        p = 2.0 * np.exp(np.cumsum(rng.normal(0.0006, 0.006, PM + REG)))
        df = day(p, rng.lognormal(5, 1.2, PM + REG))
        df["trade_count"] = rng.lognormal(3, 1.5, PM + REG)
        full = detect_markers(df, 2.0, 50_000, ctx)
        for mk, i in full.items():
            fired.add(mk)
            assert detect_markers(df.iloc[:i + 1], 2.0, 50_000, ctx).get(mk) == i, (seed, mk)
    assert len(fired) >= 10                                     # the check really exercised most markers


def test_edge_check_flags_luck_and_unstable_results():
    from bot.study import edge_check
    days = pd.date_range("2026-01-01", periods=100).date
    steady = pd.DataFrame({"day": np.repeat(days, 10), "weight": 1.0,
                           "net_pct": np.random.default_rng(1).normal(0.8, 2.0, 1000)})
    mean, t, h1, h2 = edge_check(steady, days[49])
    assert t > 5 and h1 > 0 and h2 > 0
    noise = steady.assign(net_pct=np.random.default_rng(2).normal(0.0, 2.0, 1000))
    assert abs(edge_check(noise, days[49])[1]) < 3
