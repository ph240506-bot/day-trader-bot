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
