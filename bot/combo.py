"""Do COMBINATIONS of chart markers work, when no single one does?

Run on the Pi after botstudy (it reads the signals botstudy saved, ~1 min):   botcombo

The trap: 14 markers make 91 pairs and 364 triples. Test 455 combinations and some will look great by luck.
So this works the way quant researchers do it:
  1. TRAIN on the first half of the days only: every pair and triple whose markers all fired on the same stock
     within WINDOW_MIN minutes of each other. The trade starts after the LAST of them fires (that's when you'd
     know all of them had), so its result is that marker's trade from the study.
  2. Pick the best TOP_N combinations on the training half.
  3. TEST only those on the second half, which played no part in choosing them.
A combination is only worth adding to the bot if it is clearly profitable in the second half too.
"""
import itertools
import os

import numpy as np
import pandas as pd

WINDOW_MIN = 60
MIN_TRAIN = 30          # a combination needs this many training trades to be considered
TOP_N = 10


def _mins(hhmm: str) -> int:
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def wavg(x, w):
    return float((x * w).sum() / w.sum()) if w.sum() > 0 else float("nan")


def tstat(x, w):
    if len(x) < 2 or w.sum() <= 0:
        return float("nan")
    m = wavg(x, w)
    var = float((w * (x - m) ** 2).sum() / w.sum())
    n_eff = w.sum() ** 2 / (w ** 2).sum()
    return m / (np.sqrt(var / n_eff) + 1e-12)


def combo_trades(events: pd.DataFrame, max_size: int = 3, window: int = WINDOW_MIN) -> pd.DataFrame:
    """One row per (combination, stock-day) where all its markers fired within `window` minutes.
    The trade is the one taken after the last marker fired."""
    ev = events.dropna(subset=["net_pct"]).copy()
    ev["m"] = ev["time"].map(_mins)
    rows = []
    for key, g in ev.groupby("key"):
        fired = {r.marker: r for r in g.itertuples()}
        names = sorted(fired)
        for size in range(2, max_size + 1):
            for combo in itertools.combinations(names, size):
                times = [fired[mk].m for mk in combo]
                if max(times) - min(times) > window:
                    continue
                last = max(combo, key=lambda mk: fired[mk].m)
                r = fired[last]
                rows.append({"combo": " + ".join(combo), "size": size, "key": key, "day": r.day,
                             "weight": r.weight, "is_mover": r.is_mover, "net_pct": r.net_pct})
    return pd.DataFrame(rows)


def evaluate(trades: pd.DataFrame, top_n: int = TOP_N, min_train: int = MIN_TRAIN):
    """Choose on the first half of the days, judge on the second. Returns (n_tested, rows)."""
    if trades.empty:
        return 0, []
    days = sorted(trades["day"].unique())
    cut = days[len(days) // 2 - 1]
    train, test = trades[trades["day"] <= cut], trades[trades["day"] > cut]
    scored = []
    for combo, g in train.groupby("combo"):
        if len(g) < min_train:
            continue
        x, w = g["net_pct"].to_numpy(float), g["weight"].to_numpy(float)
        scored.append((tstat(x, w), wavg(x, w), len(g), combo))
    scored.sort(reverse=True)                     # rank by how SURE the training result is, not just its size
    out = []
    for t_tr, avg_tr, n_tr, combo in scored[:top_n]:
        g = test[test["combo"] == combo]
        x, w = g["net_pct"].to_numpy(float), g["weight"].to_numpy(float)
        avg_te = wavg(x, w) if len(g) else float("nan")
        t_te = tstat(x, w) if len(g) else float("nan")
        passed = len(g) >= 20 and avg_te > 0.1 and t_te > 2.5
        out.append({"combo": combo, "train_n": n_tr, "train_avg": avg_tr, "train_t": t_tr,
                    "test_n": len(g), "test_avg": avg_te, "test_t": t_te, "passed": passed,
                    "per_day": float(g["weight"].sum()) / max(len(days) - len(days) // 2, 1)})
    return len(scored), out


def report(events: pd.DataFrame) -> list[str]:
    trades = combo_trades(events)
    n_tested, rows = evaluate(trades)
    days = events["day"].nunique() if len(events) else 0
    lines = [f"Marker combinations: {days} trading days, pairs and triples firing within {WINDOW_MIN} min of each other.",
             f"Chose the best {TOP_N} of {n_tested} combinations on the FIRST half of the days, then judged them",
             "on the SECOND half only (data they were never chosen on). Same trade test and costs as botstudy.", ""]
    if not rows:
        return lines + ["  Not enough combined signals to test anything."]
    lines.append(f"  {'combination':<58} {'first half':>18}   {'second half (the real test)':>30}")
    for r in rows:
        mark = "  <<< PASSED" if r["passed"] else ""
        lines.append(f"  {r['combo'][:58]:<58} {r['train_avg']:+.2f}% ({r['train_n']:4d})   "
                     f"{r['test_avg']:+.2f}% over {r['test_n']:4d} trades, t={r['test_t']:+.1f}, "
                     f"~{r['per_day']:.1f}/day{mark}")
    passed = [r for r in rows if r["passed"]]
    lines.append("")
    if passed:
        lines.append(f"{len(passed)} combination(s) held up out of sample: worth paper-testing live before real money.")
    else:
        lines.append("None held up out of sample: the best-looking combinations were luck, like day-2 continuation.")
    return lines


def main():
    path = os.path.join(os.getenv("STUDY_DIR", "journal"), "study_events.csv")
    if not os.path.exists(path):
        print("No study data yet. Run botstudy first (DAYS=175 botstudy for the bigger sample).")
        return
    events = pd.read_csv(path)
    text = "\n".join(report(events))
    print(text)
    with open(os.path.join(os.path.dirname(path), "combo_report.txt"), "w") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
