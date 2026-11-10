"""Decides and acts.

event -> instant trap check -> price/volume/share-count checks -> AI race
      -> (pullback mode) watch the stock: wait for the first dip, buy when it turns back up above VWAP
      -> limit buy -> managed exit (stop under the dip low, 2R target, trailing stop, time limit)
"""
import asyncio
import csv
import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .broker import round_price
from .catalysts import is_common_stock_symbol, triage, triage_form

log = logging.getLogger("smallcap")
NY = ZoneInfo("America/New_York")

FIELDS = ["time", "symbol", "source", "latency_ms", "rule", "ai", "ai_model", "price", "gap_pct", "rel_vol",
          "spread_pct", "shares_m", "vwap", "qty", "entry", "stop", "target", "exit", "pnl", "action", "reason", "text"]


@dataclass
class Position:
    symbol: str
    qty: float
    entry: float
    stop: float
    target: float
    high: float
    opened: float          # epoch seconds
    text: str = ""


@dataclass
class Watch:
    """A stock with a confirmed catalyst that we're waiting to dip and bounce."""
    symbol: str
    started: float
    high: float                    # highest price since we started watching (the spike)
    low: float | None = None       # lowest price during the pullback
    state: str = "spiking"         # spiking -> pulling -> (buy | abandoned)
    vwap: float | None = None
    vwap_at: float = 0.0
    reason: str = ""
    text: str = ""
    base: dict = field(default_factory=dict)


def session(now_ny: datetime, is_open: bool, mins_to_close: float | None = None) -> str:
    """premarket | regular | closing | closed (New York time).
    mins_to_close comes from Alpaca's clock, so early-close days (e.g. after Thanksgiving) are handled."""
    t = now_ny.time()
    if is_open:
        if mins_to_close is not None:
            return "closing" if mins_to_close <= 10 else "regular"
        return "closing" if t >= datetime.strptime("15:50", "%H:%M").time() else "regular"
    if now_ny.weekday() < 5 and datetime.strptime("04:00", "%H:%M").time() <= t < datetime.strptime("09:30", "%H:%M").time():
        return "premarket"
    return "closed"


def pullback_step(w: Watch, price: float, cfg) -> str:
    """Advance the pullback state machine with a new price. Returns: wait | buy | abandon.

    Spike: track the high. Once price falls pullback_min% below the high, we're in the pullback.
    Pullback: track the low. Too deep (pullback_max%) = the move failed. A bounce of bounce_pct%
    off the low = buy signal (the classic 'first pullback' entry)."""
    if w.state == "spiking":
        if price > w.high:
            w.high = price
        elif price <= w.high * (1 - cfg.pullback_min_pct / 100):
            w.state, w.low = "pulling", price
        return "wait"
    # pulling
    if price < w.low:
        w.low = price
    if w.low <= w.high * (1 - cfg.pullback_max_pct / 100):
        return "abandon"
    if price >= w.low * (1 + cfg.bounce_pct / 100):
        return "buy"
    return "wait"


class Engine:
    def __init__(self, cfg, broker, brain, notify, halts=None):
        self.cfg, self.broker, self.brain, self.notify = cfg, broker, brain, notify
        self.halts = halts
        self.positions: dict[str, Position] = {}
        self.watching: dict[str, Watch] = {}
        self.seen: set[str] = set()
        self.traded_today: set[str] = set()
        self.trap_today: set[str] = set()
        self.trades_today = 0
        self.day = None
        self.halted_day = None
        self.busy: set[str] = set()          # symbols being checked right now (no duplicate work)
        self.reserved = 0                     # buys in progress: count toward position/trade limits
        self._clock_cache = (None, 0.0)
        self.parked: dict[str, dict] = {}     # symbol -> pre-market news waiting for a price at the open
        self._bg: set = set()                 # background re-check tasks (kept so they aren't garbage-collected)
        self._mins_to_close = None
        self.day_stats = {"trades": 0, "wins": 0, "pnl": 0.0, "watched": 0}
        self.summary_day = None
        self.exit_failed: set[str] = set()
        os.makedirs(os.path.dirname(cfg.journal_path) or ".", exist_ok=True)
        self.state_path = os.path.join(os.path.dirname(cfg.journal_path) or ".", "smallcap_positions.json")
        self._load()

    # ---------- persistence / journal ----------
    def _load(self):
        try:
            with open(self.state_path) as f:
                self.positions = {k: Position(**v) for k, v in json.load(f).items()}
        except (OSError, ValueError, TypeError):
            self.positions = {}

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: asdict(v) for k, v in self.positions.items()}, f)
        os.replace(tmp, self.state_path)        # atomic: a power cut can't leave a half-written file

    def journal(self, **row):
        new = not os.path.exists(self.cfg.journal_path)
        row.setdefault("time", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with open(self.cfg.journal_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in FIELDS})

    def _new_day(self):
        today = datetime.now(NY).date()
        if self.day != today:
            self.day = today
            self.traded_today.clear()
            self.trap_today.clear()
            self.watching.clear()
            self.parked.clear()
            self.trades_today = 0
            self.day_stats = {"trades": 0, "wins": 0, "pnl": 0.0, "watched": 0}

    async def _call(self, fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    def _slots_left(self) -> int:
        """Free slots. Only buys actually in progress count, not stocks still being checked:
        a burst of headlines must all get looked at, even if only a few can be bought."""
        return min(self.cfg.max_positions - len(self.positions) - self.reserved,
                   self.cfg.max_trades_per_day - self.trades_today - self.reserved)

    async def _session(self) -> str:
        clock, at = self._clock_cache
        if clock is None or time.time() - at > 15:         # market clock barely changes; save a request
            clock = await self._call(self.broker.clock)
            self._clock_cache = (clock, time.time())
        is_open, _, next_close, _ = clock
        mins_to_close = None
        if is_open and next_close is not None:
            nc = next_close if next_close.tzinfo else next_close.replace(tzinfo=timezone.utc)
            mins_to_close = (nc - datetime.now(timezone.utc)).total_seconds() / 60
        self._mins_to_close = mins_to_close
        return session(datetime.now(NY), is_open, mins_to_close)

    def _too_late(self, sess) -> bool:
        """Too close to the close to open a trade that needs time (max hold, pullback watch)."""
        return (sess == "regular" and self._mins_to_close is not None
                and self._mins_to_close < self.cfg.no_entry_before_close_min)

    # ---------- the fast path ----------
    async def handle(self, ev):
        """Process one news/filing event. Returns the action taken (for logs/tests)."""
        self._new_day()
        if ev.key in self.seen:
            return "dupe"
        self.seen.add(ev.key)
        if len(self.seen) > 20000:
            self.seen.clear()
        sym = ev.symbol
        if not is_common_stock_symbol(sym):
            return "skip-asset"            # warrants, units, rights: not ordinary shares

        at = ev.at if ev.at.tzinfo else ev.at.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - at).total_seconds() > self.cfg.max_news_age_sec:
            return "stale"

        # 1. instant triage (microseconds)
        rule, matched = triage(ev.text)
        if ev.source == "edgar" and triage_form(ev.form) == "trap":
            rule, matched = "trap", ev.form
        if rule == "trap":
            self.trap_today.add(sym)       # e.g. offering filed today: ignore later "good news" on it
            self.parked.pop(sym, None)
            if sym in self.watching:
                self.watching.pop(sym)
                self.notify.send("veto", f"Dropped {sym} from watchlist", f"Trap news: {matched}", 3, "warning")
            if sym in self.positions:
                self.notify.send("alert", f"Trap news on {sym} you hold", f"{matched}: {ev.text[:200]}", 4, "warning")
            return "trap"

        # 2. cheap gates before touching any API
        if (sym in self.trap_today or sym in self.traded_today or sym in self.positions
                or sym in self.busy or sym in self.watching):
            return "skip-sym"
        if self.halted_day == self.day:
            return "skip-halted-day"
        if self._slots_left() <= 0:
            return "skip-limits"
        if self.cfg.entry_mode == "pullback" and len(self.watching) >= self.cfg.max_watch:
            return "skip-watchlist-full"
        if self.halts and sym in self.halts.halted:
            return "skip-halted"

        self.busy.add(sym)
        try:
            return await self._evaluate(ev, rule, matched)
        finally:
            self.busy.discard(sym)

    # ---------- pre-market news, re-checked at the open ----------
    def _park(self, ev, rule, matched, base) -> bool:
        cfg = self.cfg
        if not cfg.park_premarket or ev.parked:
            return False
        old = self.parked.get(ev.symbol)
        if old and old["rule"] == "strong" and rule != "strong":
            return True                               # keep the stronger headline we already hold
        if not old and len(self.parked) >= cfg.park_max:
            weak = next((s for s, p in self.parked.items() if p["rule"] != "strong"), None)
            if weak is None and rule != "strong":
                return False
            self.parked.pop(weak or next(iter(self.parked)))
        self.parked[ev.symbol] = {"ev": ev, "rule": rule, "matched": matched, "next": 0.0}
        self.journal(**base, action="parked", reason="no pre-market price; will re-check at the open")
        return True

    def _mins_since_open(self) -> float:
        now = datetime.now(NY)
        return (now - now.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60

    async def recheck_parked(self, wait: bool = False):
        """In the first minutes of the regular session, give parked pre-market news a second look."""
        if not self.parked:
            return
        if await self._session() != "regular":
            return
        mins = self._mins_since_open()
        if mins < self.cfg.park_start_min:
            return
        last_chance = mins >= self.cfg.park_window_min
        tasks = []
        for sym, p in list(self.parked.items()):
            if sym in self.busy or (time.time() < p["next"] and not last_chance):
                continue
            p["next"] = time.time() + self.cfg.park_retry_sec
            if last_chance:
                self.parked.pop(sym, None)
            self.busy.add(sym)
            tasks.append(asyncio.create_task(self._recheck_one(sym, p, last_chance)))
            if len(tasks) >= 6:                       # a few at a time: never starve exits of API calls
                break
        if wait:
            await asyncio.gather(*tasks)
        else:
            for t in tasks:
                self._bg.add(t)
                t.add_done_callback(self._bg.discard)

    async def _recheck_one(self, sym, p, last_chance):
        try:
            if (sym in self.trap_today or sym in self.traded_today or sym in self.positions or sym in self.watching
                    or self.halted_day == self.day or (self.halts and sym in self.halts.halted)):
                self.parked.pop(sym, None)
                return "skip-sym"
            if self._slots_left() <= 0:
                return "skip-limits"                  # stays parked: a slot may free up
            ev = p["ev"]
            at = ev.at if ev.at.tzinfo else ev.at.replace(tzinfo=timezone.utc)
            age_min = max(0, (datetime.now(timezone.utc) - at).total_seconds() / 60)
            ago = f"{age_min / 60:.1f}h" if age_min >= 90 else f"{age_min:.0f} min"
            ev2 = replace(ev, text=f"[Pre-market news, published {ago} ago, re-checked at the open] {ev.text}"[:500],
                          received=time.time(), parked=True)
            # quiet until the last try: too early, the stock may simply not have traded enough yet
            result = await self._evaluate(ev2, p["rule"], p["matched"], quiet_filter=not last_chance)
            if result != "filtered":
                self.parked.pop(sym, None)            # decided (bought / watching / passed): done with it
            return result
        except Exception as e:
            log.warning("re-check of %s failed: %s", sym, e)
        finally:
            self.busy.discard(sym)

    async def _evaluate(self, ev, rule, matched, quiet_filter=False):
        sym, cfg = ev.symbol, self.cfg
        sess = await self._session()
        if sess not in ("regular", "premarket") or (sess == "premarket" and not cfg.trade_premarket):
            return "skip-session"
        if self._too_late(sess):
            return "skip-late"
        if not await self._call(self.broker.asset_ok, sym):
            return "skip-asset"

        q = await self._call(self.broker.quote, sym)
        if not q or q.price <= 0:
            return "skip-noquote"
        if not (cfg.min_price <= q.price <= cfg.max_price):
            return "skip-price"            # not a small cap by our definition: silent
        base = dict(symbol=sym, source=ev.source, rule=rule, price=q.price, gap_pct=round(q.gap_pct, 1),
                    rel_vol=round(q.rel_volume, 2) if q.rel_volume else "",
                    spread_pct=round(q.spread_pct, 2) if q.spread_pct is not None else "", text=ev.text[:200])

        why = None
        if q.age_sec is not None and q.age_sec > cfg.max_quote_age_sec:
            if sess == "premarket" and q.age_sec > 3600:
                if self._park(ev, rule, matched, base):
                    return "parked"
                why = "no pre-market trades yet"
            else:
                why = f"price data {q.age_sec:.0f}s old"
        elif q.gap_pct < cfg.min_gap_pct:
            why = f"only {q.gap_pct:+.1f}% (need {cfg.min_gap_pct:+.0f}%)"
        elif q.rel_volume is not None and q.rel_volume < cfg.min_rel_volume:
            why = f"volume only {q.rel_volume:.1f}x"
        elif q.spread_pct is not None and q.spread_pct > cfg.max_spread_pct:
            why = f"spread {q.spread_pct:.1f}% too wide"
        if not why:
            shares = await self._call(self.broker.shares_outstanding, sym)
            base["shares_m"] = round(shares / 1e6, 1) if shares else ""
            if shares is None and cfg.require_share_data:
                why = "no share-count data"
            elif shares is not None and shares > cfg.max_shares_m * 1e6:
                why = f"{shares / 1e6:.0f}M shares (max {cfg.max_shares_m:.0f}M)"
        if why:
            if not quiet_filter:
                self.journal(**base, action="filtered", latency_ms=int((time.time() - ev.received) * 1000),
                             reason=(why + " (parked news, final check)") if ev.parked else why)
            return "filtered"

        # 3. AI race (only for stocks that pass every cheap check)
        verdict = await self.brain.judge(sym, ev.text, {
            "price": q.price, "change_pct_today": round(q.gap_pct, 1),
            "volume_vs_yesterday": round(q.rel_volume, 2) if q.rel_volume else None,
            "shares_outstanding_millions": base.get("shares_m") or None,
            "session": sess, "rule_check": f"{rule} ({matched})" if matched else rule},
            timeout=cfg.scanner_llm_timeout_sec if (ev.source == "scanner" or ev.parked) else None)
        ai = verdict["verdict"] if verdict else "none"
        base.update(ai=ai, ai_model=verdict.get("model", "") if verdict else "")
        if verdict and ai == "trap":
            self.trap_today.add(sym)
        go = (ai == "strong" and verdict["confidence"] >= 0.6) or (
            verdict is None and not cfg.llm_required and rule == "strong")
        # Momentum: a stock already moving hard (scanner hit, or pre-market news re-checked at the open) that the
        # AI called "weak" - no news, or a headline that doesn't explain the move - can still be traded on price
        # action alone: huge volume, few shares, never a trap, smaller size. The AI must have ANSWERED "weak":
        # no answer means nobody checked for an offering.
        mover = ev.source == "scanner" or ev.parked
        momentum = (not go and cfg.trade_no_news and mover and ai == "weak" and sym not in self.trap_today
                    and q.rel_volume is not None and q.rel_volume >= cfg.no_news_min_rel_volume
                    and base.get("shares_m") not in ("", None) and base["shares_m"] <= cfg.no_news_max_shares_m)
        if momentum:
            go = True
            base.update(size_mult=cfg.no_news_size, rule="momentum")
        momentum_miss = ""
        if not go and mover and ai == "weak":
            if not cfg.trade_no_news:
                momentum_miss = "momentum mode is off"
            elif q.rel_volume is None:
                momentum_miss = "no volume data yet"
            elif q.rel_volume < cfg.no_news_min_rel_volume:
                momentum_miss = f"volume {q.rel_volume:.1f}x, needs {cfg.no_news_min_rel_volume:.0f}x"
            elif base.get("shares_m") in ("", None):
                momentum_miss = "share count unknown"
            elif base["shares_m"] > cfg.no_news_max_shares_m:
                momentum_miss = f"{base['shares_m']:.0f}M shares, max {cfg.no_news_max_shares_m:.0f}M"
        latency = int((time.time() - ev.received) * 1000)
        base["latency_ms"] = latency
        fail = getattr(self.brain, "last_fail", "timeout")
        reason = (f"momentum: volume {q.rel_volume:.0f}x normal, {base['shares_m']}M shares, "
                  f"{cfg.no_news_size:.0%} size. AI: {verdict['reason']}") if momentum else \
            verdict["reason"] if verdict else ("rules only" if go else
                                                    "AI replies unreadable" if fail == "unreadable" else
                                                    "AI didn't answer in time")
        if not go:
            if momentum_miss:
                reason = f"{verdict['reason']} | not a momentum trade: {momentum_miss}"
            self.journal(**base, action="pass", reason=reason)
            self.notify.send("veto", f"Pass {sym} {q.gap_pct:+.0f}% (AI: {ai})",
                             f"${q.price:.2f}. {reason}\n{ev.text[:160]}", 2, "no_entry")
            return "pass"

        if cfg.entry_mode == "pullback":
            # Start from today's real high, so a stock that spiked earlier and is already dipping counts.
            vwap, day_high = await self._call(self.broker.intraday, sym)
            w = Watch(sym, time.time(), max(q.price, day_high or 0), reason=reason, text=ev.text[:200], base=base,
                      vwap=vwap, vwap_at=time.time())
            if q.price <= w.high * (1 - cfg.pullback_max_pct / 100):
                self.journal(**base, action="faded", reason=f"already {(1 - q.price / w.high) * 100:.0f}% off "
                                                            f"today's high ${w.high:.2f}")
                return "faded"
            if q.price <= w.high * (1 - cfg.pullback_min_pct / 100):
                w.state, w.low = "pulling", q.price
            self.watching[sym] = w
            self.day_stats["watched"] += 1
            self.journal(**base, action="watching", reason=reason)
            self.notify.send("veto", f"Watching {sym} {q.gap_pct:+.0f}% for a pullback",
                             f"${q.price:.2f}. AI: {reason}\nWill buy if it dips {cfg.pullback_min_pct:.0f}-"
                             f"{cfg.pullback_max_pct:.0f}% and bounces {cfg.bounce_pct:.1f}%.", 3, "eyes")
            return "watching"
        stop = round_price(q.price * (1 - cfg.stop_pct / 100))
        return await self._enter(sym, q, stop, sess, base, reason, ev.text[:200])

    # ---------- entering ----------
    async def _enter(self, sym, q, stop, sess, base, reason, text):
        if self._too_late(sess):
            self.journal(**base, action="skipped", reason="too close to the close")
            return "skip-late"
        # Check-and-reserve with no await in between, so parallel buys can't overshoot the limits.
        if self._slots_left() <= 0:
            self.journal(**base, action="skipped", reason="position/trade limit reached")
            return "skip-limits"
        self.reserved += 1
        try:
            return await self._enter_reserved(sym, q, stop, sess, base, reason, text)
        finally:
            self.reserved -= 1

    async def _enter_reserved(self, sym, q, stop, sess, base, reason, text):
        cfg = self.cfg
        equity, _ = await self._call(self.broker.account)
        ask = q.ask if q.ask > 0 else q.price
        limit = round_price(ask * (1 + cfg.entry_slippage_pct / 100))
        risk_per_share = limit - stop
        if risk_per_share <= 0:
            self.journal(**base, action="skipped", reason="stop above entry")
            return "skip-stop"
        size = float(base.get("size_mult") or 1.0)          # momentum (no-news) trades are smaller
        dollars = min(cfg.position_usd, equity * cfg.max_position_pct / 100) * size
        qty = min(math.floor(dollars / limit),
                  math.floor(equity * cfg.risk_per_trade_pct / 100 * size / risk_per_share))
        if qty < 1:
            self.journal(**base, action="skipped", reason=f"position too small (${dollars:.0f}, risk/share ${risk_per_share:.2f})")
            return "skip-size"
        target = round_price(limit + cfg.reward_risk * risk_per_share)
        base.update(qty=qty, stop=stop, target=target)
        if cfg.dry_run:
            self.journal(**base, entry=limit, action="dry-run", reason=reason)
            self.notify.send("trade", f"(dry run) would BUY {sym} x{qty} @ ${limit:.2f}",
                             f"Stop ${stop:.2f} | Target ${target:.2f}", 3, "test_tube")
            return "dry-run"

        oid = await self._call(self.broker.limit, sym, qty, "buy", limit, sess != "regular")
        filled, avg = await self._await_fill(oid, cfg.fill_timeout_sec)
        if filled <= 0:
            self.journal(**base, entry=limit, action="no-fill", reason=f"limit ${limit} not filled in {cfg.fill_timeout_sec}s")
            self.notify.send("alert", f"No fill: {sym}", f"Limit ${limit} missed, price ran away.", 2, "hourglass")
            return "no-fill"

        # keep the planned stop; recompute the target from the actual fill price
        risk = avg - stop
        pos = Position(sym, filled, avg, stop, round_price(avg + cfg.reward_risk * max(risk, 0.01)), avg, time.time(), text)
        self.positions[sym] = pos
        self.traded_today.add(sym)
        self.trades_today += 1
        self._save()
        self.journal(**{**base, "qty": filled, "target": pos.target}, entry=avg, action="bought", reason=reason)
        self.notify.send("trade", f"BUY {sym} x{filled:g} @ ${avg:.2f} ({q.gap_pct:+.0f}% today)",
                         f"Stop ${pos.stop:.2f} ({(pos.stop / avg - 1) * 100:.1f}%) | Target ${pos.target:.2f}\n"
                         f"AI: {reason}\n{text[:160]}", 4, "rocket")
        return "bought"

    async def _await_fill(self, oid, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, filled, avg = await self._call(self.broker.order, oid)
            if status == "filled" or status in ("canceled", "rejected", "expired"):
                return filled, avg
            await asyncio.sleep(0.5)
        await self._call(self.broker.cancel, oid)
        await asyncio.sleep(0.3)
        _, filled, avg = await self._call(self.broker.order, oid)   # catch fills that landed during cancel
        return filled, avg

    # ---------- the per-second loop ----------
    async def tick(self):
        """Called every couple of seconds: one batched price request for everything we care about."""
        self._new_day()
        await self.recheck_parked()
        symbols = list(dict.fromkeys(list(self.positions) + list(self.watching)))
        if not symbols:
            return
        sess = await self._session()
        quotes = await self._call(self.broker.quotes, symbols, finnhub_for=set(self.positions))
        await self.manage_exits(quotes, sess)
        await self.manage_watch(quotes, sess)

    async def manage_watch(self, quotes, sess):
        cfg = self.cfg
        for sym, w in list(self.watching.items()):
            if self._too_late(sess):
                self.watching.pop(sym)
                self.journal(**w.base, action="watch-expired", reason="too close to the close")
                continue
            if sess not in ("regular", "premarket") or time.time() - w.started > cfg.watch_min * 60:
                self.watching.pop(sym)
                self.journal(**w.base, action="watch-expired", reason=f"no clean pullback in {cfg.watch_min}m")
                continue
            if self.halts and sym in self.halts.halted:
                continue
            q = quotes.get(sym)
            if not q or q.price <= 0 or (q.age_sec is not None and q.age_sec > cfg.max_quote_age_sec):
                continue
            step = pullback_step(w, q.price, cfg)
            if step == "abandon":
                self.watching.pop(sym)
                self.journal(**w.base, action="watch-failed",
                             reason=f"dropped {(1 - w.low / w.high) * 100:.0f}% from ${w.high:.2f}: move failed")
                self.notify.send("veto", f"{sym} faded, not buying", f"Fell to ${w.low:.2f} from ${w.high:.2f}", 2,
                                 "chart_with_downwards_trend")
                continue
            if step != "buy":
                continue
            if cfg.require_above_vwap:
                if time.time() - w.vwap_at > 60:
                    w.vwap, w.vwap_at = (await self._call(self.broker.intraday, sym))[0], time.time()
                if w.vwap is not None and q.price < w.vwap:
                    continue                 # bounced, but still below the day's average price: wait
            stop = round_price(w.low * (1 - cfg.stop_buffer_pct / 100))
            if (q.price - stop) / q.price * 100 > cfg.max_stop_pct:
                self.watching.pop(sym)
                self.journal(**w.base, action="watch-failed", reason="stop would be too far below")
                continue
            if self._slots_left() <= 0:
                continue
            self.watching.pop(sym)
            base = {**w.base, "price": q.price, "vwap": round(w.vwap, 4) if w.vwap else ""}
            self.busy.add(sym)
            try:
                await self._enter(sym, q, stop, sess, base,
                                  f"pullback bounce off ${w.low:.2f} (high ${w.high:.2f}). {w.reason}", w.text)
            finally:
                self.busy.discard(sym)

    # ---------- exits ----------
    async def manage_exits(self, quotes=None, sess=None):
        if not self.positions:
            return
        if sess is None:
            is_open, *_ = await self._call(self.broker.clock)
            sess = session(datetime.now(NY), is_open)
        for sym, p in list(self.positions.items()):
            if self.halts and sym in self.halts.halted:
                continue                    # can't trade during a halt; check again next tick
            q = (quotes or {}).get(sym) or await self._call(self.broker.quote, sym)
            if not q or q.price <= 0:
                continue
            price = q.price
            p.high = max(p.high, price)
            if p.high >= p.entry * (1 + self.cfg.trail_arm_pct / 100):
                p.stop = max(p.stop, round_price(p.high * (1 - self.cfg.trail_pct / 100)))
            reason = None
            if price <= p.stop:
                reason = "trailing stop" if p.stop > p.entry else "stop-loss"
            elif price >= p.target:
                reason = "target"
            elif time.time() - p.opened > self.cfg.max_hold_min * 60:
                reason = f"time limit {self.cfg.max_hold_min}m"
            elif sess in ("closing", "closed"):
                reason = "end of session"
            if reason:
                await self._exit(p, q, reason, sess)
            else:
                self._save()

    async def _exit(self, p, q, reason, sess):
        extended = sess not in ("regular", "closing")
        ref = q.bid if q.bid > 0 else q.price
        remaining, sold, value = p.qty, 0.0, 0.0
        for attempt in range(3):                       # marketable limit, a bit lower each try
            if remaining <= 0:
                break
            limit = round_price(ref * (1 - self.cfg.exit_slippage_pct * (attempt + 1) / 100))
            oid = await self._call(self.broker.limit, p.symbol, remaining, "sell", limit, extended)
            f, a = await self._await_fill(oid, 5)
            if f > 0:
                sold, value, remaining = sold + f, value + f * a, remaining - f
        if remaining > 0 and not extended:             # regular hours: last resort market order
            oid = await self._call(self.broker.market_sell, p.symbol, remaining)
            f, a = await self._await_fill(oid, 10)
            if f > 0:
                sold, value, remaining = sold + f, value + f * a, remaining - f
        if sold <= 0:
            if p.symbol not in self.exit_failed:
                self.exit_failed.add(p.symbol)
                self.notify.send("alert", f"EXIT FAILED {p.symbol}", f"Couldn't sell ({reason}). Retrying.", 5,
                                 "rotating_light")
            return
        self.exit_failed.discard(p.symbol)
        avg = value / sold
        pnl = (avg - p.entry) * sold
        self.day_stats["pnl"] += pnl
        if remaining <= 1e-9:
            self.day_stats["trades"] += 1
            self.day_stats["wins"] += 1 if (avg - p.entry) > 0 else 0
        self.journal(symbol=p.symbol, source="exit", qty=sold, entry=p.entry, stop=p.stop, exit=round(avg, 4),
                     pnl=round(pnl, 2), action="sold" if remaining <= 0 else "partial-sell", reason=reason, text=p.text)
        self.notify.send("trade", f"SOLD {p.symbol} {'+' if pnl >= 0 else '-'}${abs(pnl):.2f} ({reason})",
                         f"Entry ${p.entry:.2f} -> exit ${avg:.2f} ({(avg / p.entry - 1) * 100:+.1f}%)"
                         + ("" if remaining <= 0 else f"\n{remaining:g} shares still held, retrying"), 4,
                         "white_check_mark" if pnl >= 0 else "x")
        if remaining <= 1e-9:
            self.positions.pop(p.symbol, None)
        else:
            p.qty = remaining
        self._save()

    async def check_daily_loss(self):
        self._new_day()
        if self.halted_day == self.day:
            return
        equity, last_equity = await self._call(self.broker.account)
        if last_equity > 0 and (equity - last_equity) / last_equity * 100 <= -self.cfg.daily_loss_pct:
            self.halted_day = self.day
            self.watching.clear()
            self.notify.send("alert", "Small-cap daily loss limit hit",
                             f"Down {(equity - last_equity) / last_equity * 100:.1f}%. Selling everything, done for today.",
                             5, "rotating_light")
            is_open, *_ = await self._call(self.broker.clock)
            sess = session(datetime.now(NY), is_open)
            for p in list(self.positions.values()):
                q = await self._call(self.broker.quote, p.symbol)
                if q:
                    await self._exit(p, q, "daily loss limit", sess)

    async def maybe_daily_summary(self):
        """Once per trading day, after the close: one phone alert with how the day went."""
        now = datetime.now(NY)
        if now.weekday() >= 5 or now.hour < 16 or self.summary_day == now.date():
            return
        if await self._session() not in ("closed",):
            return
        self.summary_day = now.date()
        st = self.day_stats
        equity, last_equity = await self._call(self.broker.account)
        wr = f"{st['wins']}/{st['trades']} wins" if st["trades"] else "no trades"
        self.notify.send("alert", f"Small-cap day: {wr}, {'+' if st['pnl'] >= 0 else '-'}${abs(st['pnl']):.2f}",
                         f"Watched {st['watched']} catalyst stocks. Account ${equity:,.2f} "
                         f"({equity - last_equity:+,.2f} today).", 3, "bar_chart")
