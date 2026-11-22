"""Marker combinations: the trade is taken after the LAST marker fires, and only out-of-sample results count."""
import numpy as np
import pandas as pd

from bot.combo import combo_trades, evaluate, report


def ev(key, marker, time, net, day="2026-01-05", w=1.0, mover=False):
    return {"key": key, "marker": marker, "time": time, "net_pct": net, "day": day, "weight": w,
            "is_mover": mover, "symbol": key.split("|")[0]}


def test_combo_trades_use_the_last_signal_and_respect_the_window():
    rows = [ev("A|d1", "bull_flag", "10:05", -1.0), ev("A|d1", "hod_break", "10:20", +3.0),
            ev("A|d1", "red_to_green", "13:30", +9.0)]                         # 3h later: outside the window
    t = combo_trades(pd.DataFrame(rows))
    assert set(t["combo"]) == {"bull_flag + hod_break"}
    assert t.iloc[0]["net_pct"] == 3.0                                       # entered after hod_break, the later one


def _market(n_days=120, seed=0, real_edge=True):
    """Noise everywhere; optionally one pair (bull_flag + print_surge) that genuinely pays, all period long."""
    rng = np.random.default_rng(seed)
    markers = ["bull_flag", "print_surge", "hod_break", "vwap_reclaim", "red_to_green", "one_min_orb"]
    days = pd.date_range("2026-01-01", periods=n_days).strftime("%Y-%m-%d")
    rows = []
    for d in days:
        for s in range(25):
            key = f"S{s}|{d}"
            fired = [m for m in markers if rng.random() < 0.35]
            good = real_edge and "bull_flag" in fired and "print_surge" in fired
            for k, m in enumerate(fired):
                net = rng.normal(-0.6, 3.0)
                if good and m in ("bull_flag", "print_surge"):
                    net = rng.normal(+1.5, 3.0)
                rows.append(ev(key, m, f"10:{10 + k:02d}", net, day=d))
    return pd.DataFrame(rows)


def test_a_real_combination_survives_the_second_half():
    n, rows = evaluate(combo_trades(_market(real_edge=True)))
    found = [r for r in rows if r["combo"].startswith("bull_flag + print_surge")]
    assert found and any(r["passed"] for r in found)


def test_pure_noise_never_passes_even_after_picking_the_best_of_many():
    for seed in range(5):
        n, rows = evaluate(combo_trades(_market(seed=seed, real_edge=False)))
        assert n > 20                                                        # lots of combinations were tried
        assert not any(r["passed"] for r in rows), seed                     # and none fooled the second half


def test_report_reads_like_a_verdict():
    text = "\n".join(report(_market(real_edge=False)))
    assert "FIRST half" in text and "None held up out of sample" in text
