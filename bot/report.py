"""Performance report for both bots. Paste the output to Claude for a review.

Run on the Pi:   botreport            (last 1 day)
                 DAYS=30 botreport    (last 30 days)
"""
import csv
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone


# ---------------- trades from Alpaca fills ----------------
def round_trips(fills: list[dict]) -> list[dict]:
    """Pair fills into completed trades (flat -> position -> flat), FIFO, longs and shorts.
    A plain 'sell' with nothing bought in the window closes a position opened BEFORE the window
    (e.g. old holdings sold at the open): it is ignored, never mistaken for a new short.
    Only 'sell_short' opens a short."""
    trades = []
    book = defaultdict(lambda: {"lots": [], "pnl": 0.0, "opened": None, "side": None, "cost": 0.0, "active": False,
                                "opened_qty": 0.0, "fees": 0.0})
    for f in sorted(fills, key=lambda x: x["transaction_time"]):
        sym = f["symbol"].replace("/", "")
        qty, price, side = float(f["qty"]), float(f["price"]), f["side"]
        sign = 1 if side == "buy" else -1
        b = book[sym]
        remaining = qty
        while remaining > 1e-9 and b["lots"] and (b["lots"][0][1] > 0) != (sign > 0):   # close opposite lots
            lot_qty, lot_sign, lot_price = b["lots"][0]
            take = min(lot_qty, remaining)
            b["pnl"] += (price - lot_price) * take * lot_sign
            remaining -= take
            if take >= lot_qty - 1e-9:
                b["lots"].pop(0)
            else:
                b["lots"][0][0] = lot_qty - take
        # Crypto: Alpaca takes its fee out of the coins bought, so the sell is ~0.25% smaller than the buy.
        # A leftover of <=1% of what was opened is that fee: treat the trade as closed and charge it.
        if b["active"] and b["lots"] and remaining <= 1e-9 and sum(l[0] for l in b["lots"]) <= 0.01 * b["opened_qty"]:
            b["pnl"] -= sum(l[0] * l[2] for l in b["lots"])
            b["fees"] += sum(l[0] * l[2] for l in b["lots"])
            b["lots"] = []
        if b["active"] and not b["lots"]:                                               # back to flat
            trades.append({"symbol": sym, "side": b["side"], "pnl": round(b["pnl"], 2), "opened": b["opened"],
                           "closed": f["transaction_time"], "cost": round(b["cost"], 2),
                           "fees": round(b["fees"], 2)})
            b["active"] = False
        if remaining > 1e-9:
            if side == "sell" and not b["active"]:
                continue                                     # plain sell, nothing open: pre-window holding
            if not b["active"]:
                b.update(active=True, pnl=0.0, cost=0.0, opened=f["transaction_time"], opened_qty=0.0, fees=0.0,
                         side="long" if sign > 0 else "short")
            b["opened_qty"] += remaining
            b["lots"].append([remaining, sign, price])
            b["cost"] += remaining * price
    return trades


def money(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


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
    fees = sum(t.get("fees", 0) for t in trades)
    if fees:
        lines.append(f"  Crypto fees (included above): ${fees:,.2f}")
    best, worst = max(trades, key=lambda t: t["pnl"]), min(trades, key=lambda t: t["pnl"])
    lines.append(f"  Best: {best['symbol']} {best['side']} {money(best['pnl'])}   "
                 f"Worst: {worst['symbol']} {worst['side']} {money(worst['pnl'])}")
    by_sym = defaultdict(float)
    for t in trades:
        by_sym[t["symbol"]] += t["pnl"]
    top = sorted(by_sym.items(), key=lambda kv: kv[1])
    lines.append("  By symbol: " + ", ".join(f"{s} {v:+.2f}" for s, v in (top[:3] + top[-3:] if len(top) > 6 else top)))
    return lines


def order_lines(orders, n_fills) -> list[str]:
    """What actually happened to each order: filled, cancelled, rejected..."""
    if not orders:
        return [f"  Orders: none   Fills: {n_fills}"]
    st = Counter(str(getattr(o.status, "value", o.status)) for o in orders)
    lines = [f"  Orders: {len(orders)} (" + ", ".join(f"{k} {v}" for k, v in st.most_common()) + f")   Fills: {n_fills}"]
    for o in orders[:12]:
        status = str(getattr(o.status, "value", o.status))
        side = str(getattr(o.side, "value", o.side))
        typ = str(getattr(o.type, "value", o.type) if getattr(o, "type", None) else getattr(o, "order_type", ""))
        when = o.submitted_at.strftime("%H:%M") if getattr(o, "submitted_at", None) else "?"
        px = o.filled_avg_price or o.limit_price or o.stop_price or ""
        lines.append(f"    {when} UTC {side} {o.symbol} x{o.qty or o.notional} {typ} -> {status}"
                     + (f" @ {float(px):.2f}" if px else ""))
    return lines


# ---------------- journals ----------------
def read_csv(path, since):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("time", "") >= since]


def filter_kind(reason: str) -> str:
    """Group filter reasons by type, so '237820s old' and '237829s old' count together."""
    r = re.sub(r"[+-]?\d[\d.,]*", "N", reason)
    return re.sub(r"\(need N%\)|\(max NM\)", "", r).strip()[:40]


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
        vetoes = [r.get("llm_reason", "") for r in rows if r.get("llm_decision") == "veto"]
        failed = [v for v in vetoes if v.startswith("LLM skipped")]
        real = [v for v in vetoes if not v.startswith("LLM skipped")]
        if vetoes:
            lines.append(f"  AI vetoes: {len(real)} real, {len(failed)} where the AI gave no usable answer")
        for v in real[-4:] + failed[-2:]:
            lines.append(f"    veto: {v[:110]}")
    else:
        reasons = Counter(filter_kind(r.get("reason") or "") for r in rows if r.get("action") == "filtered")
        if reasons:
            lines.append("  Top filter reasons: " + "; ".join(f"{k} ({v})" for k, v in reasons.most_common(6)))
        lat = [int(r["latency_ms"]) for r in rows if (r.get("latency_ms") or "").isdigit()]
        if lat:
            lat.sort()
            lines.append(f"  Decision speed: median {lat[len(lat) // 2] / 1000:.1f}s, slowest {lat[-1] / 1000:.1f}s")
        for r in [r for r in rows if r.get("action") in ("pass", "watching", "bought", "sold", "watch-failed", "faded")][-8:]:
            gap = f"{r['gap_pct']}% " if r.get("gap_pct") else ""
            lines.append(f"    {r.get('action')}: {r.get('symbol')} {gap}"
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
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    try:
        orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500, nested=False,
                                                    after=datetime.now(timezone.utc) - timedelta(days=days)))
    except Exception:
        orders = []
    return rows, acct, positions, orders


def account_block(title, key, secret, paper, days, journal, kind) -> list[str]:
    lines = [f"=== {title} ==="]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    try:
        fills, acct, positions, orders = fetch_fills(key, secret, paper, days)
        eq, last = float(acct.equity), float(acct.last_equity)
        lines.append(f"  Account: ${eq:,.2f} (today {eq - last:+,.2f})   Open positions: "
                     + (", ".join(f"{p.symbol} {float(p.unrealized_pl):+.2f}" for p in positions) or "none"))
        lines += summarize(round_trips(fills))
        lines += order_lines(orders, len(fills))
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
