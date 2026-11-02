"""Performance report for both bots. Paste the output to Claude for a review.

Run on the Pi:   botreport            (last 1 day)
                 DAYS=30 botreport    (last 30 days)
"""
import csv
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone


# ---------------- trades from Alpaca fills ----------------
def round_trips(fills: list[dict]) -> list[dict]:
    """Pair fills into completed trades (flat -> position -> flat), FIFO, longs and shorts.
    fills: dicts with symbol, side ('buy'/'sell'/'sell_short'), qty, price, transaction_time."""
    trades = []
    book = defaultdict(lambda: {"pos": 0.0, "lots": [], "pnl": 0.0, "opened": None, "side": None, "cost": 0.0})
    for f in sorted(fills, key=lambda x: x["transaction_time"]):
        sym = f["symbol"].replace("/", "")
        qty, price = float(f["qty"]), float(f["price"])
        sign = 1 if f["side"] == "buy" else -1
        b = book[sym]
        if b["pos"] == 0:
            b["opened"], b["side"], b["pnl"], b["cost"] = f["transaction_time"], "long" if sign > 0 else "short", 0.0, 0.0
        remaining = qty
        # close against opposite lots first
        while remaining > 1e-9 and b["lots"] and (b["lots"][0][1] > 0) != (sign > 0):
            lot_qty, lot_sign, lot_price = b["lots"][0]
            take = min(lot_qty, remaining)
            b["pnl"] += (price - lot_price) * take * lot_sign
            b["pos"] += take * sign
            remaining -= take
            if take >= lot_qty - 1e-9:
                b["lots"].pop(0)
            else:
                b["lots"][0][0] = lot_qty - take
        if remaining > 1e-9:                      # opens or adds to a position
            b["lots"].append([remaining, sign, price])
            b["pos"] += remaining * sign
            b["cost"] += remaining * price
        if abs(b["pos"]) < 1e-9 and b["opened"] is not None and not b["lots"]:
            trades.append({"symbol": sym, "side": b["side"], "pnl": round(b["pnl"], 2), "opened": b["opened"],
                           "closed": f["transaction_time"], "cost": round(b["cost"], 2)})
            b["pos"], b["opened"] = 0.0, None
    return trades


def summarize(trades: list[dict]) -> list[str]:
    if not trades:
        return ["  No completed trades."]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    gross_win, gross_loss = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    lines = [
        f"  Trades: {len(trades)}   Wins: {len(wins)}   Losses: {len(losses)}   "
        f"Win rate: {len(wins) / len(trades) * 100:.0f}%",
        f"  Total P&L: {'+' if total >= 0 else '-'}${abs(total):,.2f}   "
        f"Avg win: ${gross_win / len(wins) if wins else 0:,.2f}   Avg loss: -${gross_loss / len(losses) if losses else 0:,.2f}",
        f"  Profit factor: {gross_win / gross_loss:.2f}" if gross_loss > 0 else "  Profit factor: n/a (no losses)",
    ]
    best, worst = max(trades, key=lambda t: t["pnl"]), min(trades, key=lambda t: t["pnl"])
    lines.append(f"  Best: {best['symbol']} {best['side']} +${best['pnl']:,.2f}   "
                 f"Worst: {worst['symbol']} {worst['side']} ${worst['pnl']:,.2f}")
    by_sym = defaultdict(float)
    for t in trades:
        by_sym[t["symbol"]] += t["pnl"]
    top = sorted(by_sym.items(), key=lambda kv: kv[1])
    lines.append("  By symbol: " + ", ".join(f"{s} {v:+.2f}" for s, v in (top[:3] + top[-3:] if len(top) > 6 else top)))
    return lines


# ---------------- journals ----------------
def read_csv(path, since):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("time", "") >= since]


def journal_lines(path, since, kind) -> list[str]:
    rows = read_csv(path, since)
    if not rows:
        return ["  (no journal entries)"]
    acts = Counter(r.get("action", "") for r in rows)
    lines = ["  Decisions: " + ", ".join(f"{k} {v}" for k, v in acts.most_common())]
    if kind == "main":
        scores = Counter(r.get("score", "") for r in rows if r.get("action") == "ordered")
        if scores:
            lines.append("  Orders by score: " + ", ".join(f"{k}/5: {v}" for k, v in sorted(scores.items())))
        vetoes = [r["llm_reason"] for r in rows if r.get("llm_decision") == "veto"][:5]
        for v in vetoes:
            lines.append(f"    veto: {v[:110]}")
    else:
        reasons = Counter((r.get("reason") or "")[:40] for r in rows if r.get("action") == "filtered")
        if reasons:
            lines.append("  Top filter reasons: " + "; ".join(f"{k} ({v})" for k, v in reasons.most_common(4)))
        lat = [int(r["latency_ms"]) for r in rows if (r.get("latency_ms") or "").isdigit()]
        if lat:
            lat.sort()
            lines.append(f"  Decision speed: median {lat[len(lat) // 2] / 1000:.1f}s, slowest {lat[-1] / 1000:.1f}s")
        for r in [r for r in rows if r.get("action") in ("pass", "watching", "bought", "sold", "watch-failed", "faded")][-8:]:
            lines.append(f"    {r.get('action')}: {r.get('symbol')} {r.get('gap_pct') or ''}% "
                         f"AI={r.get('ai') or '-'} | {(r.get('reason') or '')[:80]}")
    return lines


# ---------------- Alpaca ----------------
def fetch_fills(key, secret, paper, days):
    from alpaca.trading.client import TradingClient
    client = TradingClient(key, secret, paper=paper)
    after = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows, token = [], None
    while True:
        params = {"after": after, "direction": "asc", "page_size": 100}
        if token:
            params["page_token"] = token
        page = client.get("/account/activities/FILL", params) or []
        rows.extend(page)
        if len(page) < 100:
            break
        token = page[-1]["id"]
    acct = client.get_account()
    positions = client.get_all_positions()
    return rows, acct, positions


def account_block(title, key, secret, paper, days, journal, kind) -> list[str]:
    lines = [f"=== {title} ==="]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    try:
        fills, acct, positions = fetch_fills(key, secret, paper, days)
        eq, last = float(acct.equity), float(acct.last_equity)
        lines.append(f"  Account: ${eq:,.2f} (today {eq - last:+,.2f})   Open positions: "
                     + (", ".join(f"{p.symbol} {float(p.unrealized_pl):+.2f}" for p in positions) or "none"))
        lines += summarize(round_trips(fills))
    except Exception as e:
        lines.append(f"  Couldn't reach Alpaca: {e}")
    lines += journal_lines(journal, since, kind)
    return lines


def main():
    from .config import Config
    from .smallcap.config import SCConfig
    days = int(os.getenv("DAYS", "1"))
    cfg, sc = Config(), SCConfig()
    out = [f"Report for the last {days} day(s), generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", ""]
    out += account_block("Main bot", cfg.alpaca_key, cfg.alpaca_secret, cfg.paper, days, cfg.journal_path, "main")
    if sc.alpaca_key:
        out += [""] + account_block("Small-cap bot", sc.alpaca_key, sc.alpaca_secret, sc.paper, days,
                                    sc.journal_path, "smallcap")
    print("\n".join(out))


if __name__ == "__main__":
    main()
