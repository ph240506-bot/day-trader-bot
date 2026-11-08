"""Main loop: every minute, pull fresh bars, look for setups, ask the LLM, size, trade, journal."""
import csv
import json
import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import Config
from .llm import Reviewer
from .market import Market, is_crypto
from .risk import daily_loss_hit, plan_order
from .notify import Notifier
from .scanner import MoverScanner
from .indicators import add_all
from .strategy import chart_summary, evaluate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")
NY = ZoneInfo("America/New_York")

JOURNAL_FIELDS = ["time", "symbol", "side", "score", "price", "qty", "stop", "target",
                  "llm_decision", "llm_reason", "action", "reasons"]


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.mkt = Market(cfg)
        self.reviewer = Reviewer(cfg)
        self.notify = Notifier(cfg)
        self.crypto_exits: dict[str, tuple[str, float, float]] = {}  # symbol -> (side, stop, target)
        self.seen: set[tuple[str, str]] = set()  # (symbol, bar time) already handled
        self.halted = False
        self.halted_on = None  # New York date the loss limit fired; resets next trading day
        self._last_status = 0.0
        self.mins_to_close = 9999.0     # set by run() from Alpaca's clock (handles early-close days)
        self.seen_day = None
        self.prev_positions: set[str] = set()
        self.last_exit: dict[str, float] = {}    # symbol -> when its position closed
        self.scanner = MoverScanner(cfg, self.mkt.trading) if cfg.scan_movers else None
        os.makedirs(os.path.dirname(cfg.journal_path) or ".", exist_ok=True)
        # Crypto stops/targets live here so a restart doesn't leave a position unmanaged.
        self.state_path = os.path.join(os.path.dirname(cfg.journal_path) or ".", "crypto_exits.json")
        self.crypto_exits = self._load_exits()

    def _load_exits(self):
        try:
            with open(self.state_path) as f:
                return {k: tuple(v) for k, v in json.load(f).items()}
        except (OSError, ValueError):
            return {}

    def _save_exits(self):
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.crypto_exits, f)
            os.replace(tmp, self.state_path)       # atomic: a power cut can't corrupt it
        except OSError as e:
            log.warning("could not save crypto exits: %s", e)

    # ---------- journal ----------
    def journal(self, row: dict):
        new = not os.path.exists(self.cfg.journal_path)
        with open(self.cfg.journal_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            if new:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in JOURNAL_FIELDS})

    # ---------- one pass ----------
    def tick(self, market_open: bool):
        acct = self.mkt.account()
        equity, last_equity = float(acct.equity), float(acct.last_equity)

        today = datetime.now(NY).date()
        if self.seen_day != today:          # long-running on the Pi: don't let this grow forever
            self.seen_day, self.seen = today, set()
        if self.halted and self.halted_on != today:
            log.info("New trading day: daily loss limit reset.")
            self.halted = False
        if not self.halted and daily_loss_hit(equity, last_equity, self.cfg):
            log.error("Daily loss limit hit (equity %.2f vs %.2f). Flattening and stopping new trades.",
                      equity, last_equity)
            if not self.cfg.dry_run:
                self.mkt.flatten_all()
            self.notify.send("alert", "Daily loss limit hit",
                             f"Account down {(equity - last_equity) / last_equity * 100:.1f}% today "
                             f"(${equity:,.0f}). Closed everything, no new trades today.", 5, "rotating_light")
            self.halted = True
            self.halted_on = today
        if self.halted:
            return

        stocks = list(self.cfg.stocks)
        movers = []
        if market_open and self.scanner:
            movers = [m for m in self.scanner.refresh() if m not in stocks]
            stocks += movers
        symbols = (stocks if market_open else []) + (self.cfg.cryptos if self.cfg.trade_crypto else [])
        if not symbols:
            return
        bars = self.mkt.bars(symbols)
        positions = self.mkt.positions()
        for k in self.prev_positions - set(positions):     # closed since last tick (stop, target or exit)
            self.last_exit[k] = time.time()
        self.prev_positions = set(positions)
        self.manage_crypto_exits(bars, positions)
        self.status(symbols, bars, equity, last_equity, positions, market_open)
        stock_entries_ok = market_open and self._in_entry_window()
        regime = self._market_regime(bars) if (market_open and self.cfg.market_filter) else None

        for sym, df in bars.items():
            if len(df) < 2:
                continue
            # Only act on a finished bar: drop the still-forming last one.
            df = df.iloc[:-1]
            bar_key = (sym, str(df.index[-1]))
            if bar_key in self.seen:
                continue
            self.seen.add(bar_key)

            if sym in movers and not enough_volume(df, self.cfg.min_iex_volume):
                continue  # thin trading = wide spreads and fake breakouts
            can_short = not is_crypto(sym) and (sym not in movers or sym in self.scanner.shortable)
            sig = evaluate(sym, df, allow_short=can_short)
            if sig and sig.score < self.cfg.min_score:
                log.info("Near miss: %s %s scored %d/5 (need %d): %s", sig.side, sym, sig.score,
                         self.cfg.min_score, "; ".join(sig.reasons))
                self.notify.send("near_miss", f"Near miss: {sig.side} {sym} {sig.score}/5",
                                 f"${sig.price:,.2f}. Had: " + "; ".join(sig.reasons), 2, "eyes")
            if not sig or sig.score < self.cfg.min_score:
                continue
            if not is_crypto(sym):
                if not stock_entries_ok:
                    log.info("Skip %s %s: outside entry window (first %dm / last %dm of the session)",
                             sig.side, sym, self.cfg.open_delay_min, self.cfg.no_entry_before_close_min)
                    continue
                if regime and ((sig.side == "buy" and regime == "down") or (sig.side == "sell" and regime == "up")):
                    log.info("Skip %s %s: against the market (SPY %s its VWAP)", sig.side, sym,
                             "below" if regime == "down" else "above")
                    continue
            pos_key = sym.replace("/", "")
            if pos_key in positions or len(positions) >= self.cfg.max_open_positions:
                continue
            since_exit = time.time() - self.last_exit.get(pos_key, 0)
            if since_exit < self.cfg.reentry_cooldown_min * 60:
                log.info("Skip %s %s: closed a trade in it %.0f min ago (cooldown %d min)",
                         sig.side, sym, since_exit / 60, self.cfg.reentry_cooldown_min)
                continue
            if not self.cfg.dry_run and self.mkt.has_open_order(sym):
                continue

            verdict = self.reviewer.review(sig, chart_summary(df), self.mkt.headlines(sym))
            # Re-price from the latest trade: the signal candle can be minutes old, and stops/targets
            # must be on the correct side of the price the order actually fills at.
            px = self.mkt.latest_price(sym) or sig.price
            if abs(px - sig.price) > self.cfg.max_chase_atr * sig.atr:
                log.info("Skip %s %s: price moved %.2f -> %.2f since the signal", sig.side, sym, sig.price, px)
                continue
            plan = plan_order(sig.side, px, sig.atr, equity, self.cfg, fractional=is_crypto(sym))
            if plan and is_crypto(sym) and abs(plan.target - px) / px * 100 < self.cfg.min_crypto_target_pct:
                log.info("Skip %s %s: target only %.2f%% away, too small to beat ~0.5%% crypto fees",
                         sig.side, sym, abs(plan.target - px) / px * 100)
                continue
            row = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbol": sym,
                   "side": sig.side, "score": sig.score, "price": sig.price,
                   "llm_decision": verdict["decision"], "llm_reason": verdict["reason"],
                   "reasons": "; ".join(sig.reasons)}
            if plan:
                row.update(qty=plan.qty, stop=plan.stop, target=plan.target)

            if verdict["decision"] != "approve" or not plan:
                row["action"] = "skipped"
                if verdict["decision"] != "approve":
                    self.notify.send("veto", f"AI veto: {sig.side} {sym} ({sig.score}/5)",
                                     f"${sig.price:,.2f}. {verdict['reason']}", 3, "no_entry")
            elif self.cfg.dry_run:
                row["action"] = "dry-run"
            else:
                try:
                    self.mkt.enter(sym, sig.side, plan)
                    row["action"] = "ordered"
                    positions[pos_key] = True  # count it toward the cap this pass
                    if is_crypto(sym):
                        self.crypto_exits[pos_key] = (sig.side, plan.stop, plan.target)
                        self._save_exits()
                    log.info("ORDER %s %s x%s stop %.2f target %.2f", sig.side, sym, plan.qty, plan.stop, plan.target)
                    self.notify.send(
                        "trade", f"{'BUY' if sig.side == 'buy' else 'SHORT'} {sym} x{plan.qty}",
                        f"Entry ~${sig.price:,.2f} (${plan.qty * sig.price:,.0f})\n"
                        f"Stop ${plan.stop:,.2f} | Target ${plan.target:,.2f}\n"
                        f"Score {sig.score}/5: " + "; ".join(sig.reasons) + f"\nAI: {verdict['reason']}",
                        4, "chart_with_upwards_trend" if sig.side == "buy" else "chart_with_downwards_trend")
                except Exception as e:
                    row["action"] = f"error: {e}"[:200]
                    log.error("order failed for %s: %s", sym, e)
                    self.notify.send("alert", f"Order failed: {sym}", str(e)[:300], 4, "warning")
            self.journal(row)

    def _in_entry_window(self) -> bool:
        now = datetime.now(NY)
        mins_since_open = (now - now.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60
        return mins_since_open >= self.cfg.open_delay_min and self.mins_to_close >= self.cfg.no_entry_before_close_min

    def _market_regime(self, bars):
        """'up' if SPY is above today's VWAP, 'down' if below, None if unknown."""
        spy = bars.get("SPY")
        if spy is None:
            spy = self.mkt.bars(["SPY"]).get("SPY")
        if spy is None or len(spy) < 2:
            return None
        last = add_all(spy).iloc[-1]
        return "up" if last["close"] >= last["vwap"] else "down"

    def status(self, symbols, bars, equity, last_equity, positions, market_open):
        """Heartbeat so a quiet log still shows the bot is alive and looking."""
        if time.time() - self._last_status < self.cfg.status_every_minutes * 60:
            return
        self._last_status = time.time()
        day_pl = equity - last_equity
        prices = ", ".join(f"{s} {float(bars[s]['close'].iloc[-1]):,.2f}" for s in list(bars)[:6])
        log.info("Status: %s | watching %d symbols (%s%s) | equity $%s (today %+.2f) | %d open position(s)%s",
                 "stocks+crypto" if market_open else "crypto only (stock market closed)",
                 len(symbols), prices, ", ..." if len(bars) > 6 else "",
                 f"{equity:,.2f}", day_pl, len(positions),
                 ": " + ", ".join(positions) if positions else "")

    def manage_crypto_exits(self, bars, positions):
        for pos_key, (side, stop, target) in list(self.crypto_exits.items()):
            if pos_key not in positions:
                self.crypto_exits.pop(pos_key)
                self._save_exits()
                continue
            sym = next((s for s in bars if s.replace("/", "") == pos_key), None)
            if not sym:
                continue
            price = float(bars[sym]["close"].iloc[-1])
            hit = (price <= stop or price >= target) if side == "buy" else (price >= stop or price <= target)
            if hit:
                log.info("Crypto exit %s at %.2f (stop %.2f / target %.2f)", sym, price, stop, target)
                won = (price >= target) if side == "buy" else (price <= target)
                self.notify.send("trade", f"Closed {sym}: {'target hit' if won else 'stopped out'}",
                                 f"Exit ~${price:,.2f}", 4, "white_check_mark" if won else "x")
                if not self.cfg.dry_run:
                    self.mkt.close(sym)
                self.crypto_exits.pop(pos_key)
                self._save_exits()

    # ---------- session ----------
    def run(self):
        started = time.time()
        deadline = started + self.cfg.max_runtime_minutes * 60
        log.info("Bot starting. paper=%s dry_run=%s llm=%s", self.cfg.paper, self.cfg.dry_run,
                 "on" if self.reviewer.client else "off")

        clock = self.mkt.clock()
        if not clock.is_open:
            wait = (clock.next_open - clock.timestamp).total_seconds()
            if wait > self.cfg.wait_for_open_minutes * 60 and not self.cfg.trade_crypto:
                log.info("Market opens in %.0f min; nothing to do. Exiting.", wait / 60)
                return

        flattened_for = None  # date we already flattened on
        while time.time() < deadline:
            clock = self.mkt.clock()
            mins_to_close = (clock.next_close - clock.timestamp).total_seconds() / 60
            self.mins_to_close = mins_to_close
            stocks_live = clock.is_open
            if clock.is_open and mins_to_close <= self.cfg.flatten_minutes_before_close:
                stocks_live = False
                if flattened_for != clock.timestamp.date():
                    log.info("%.0f min to close: flattening for the day.", mins_to_close)
                    left = [] if self.cfg.dry_run else (self.mkt.flatten_all() or [])
                    acct = self.mkt.account()
                    eq, prev = float(acct.equity), float(acct.last_equity)
                    self.notify.send("alert", "Market closing: positions closed",
                                     f"Account ${eq:,.2f}, today {eq - prev:+,.2f} ({(eq - prev) / prev * 100:+.2f}%)",
                                     3, "bell")
                    if left:
                        self.notify.send("alert", f"COULDN'T CLOSE: {', '.join(left)}",
                                         "Close it yourself in the Alpaca app (Positions -> Close).", 5, "warning")
                    self.crypto_exits.clear()
                    self._save_exits()
                    flattened_for = clock.timestamp.date()
            if not clock.is_open and not self.cfg.trade_crypto:
                wait = (clock.next_open - clock.timestamp).total_seconds()
                if wait > self.cfg.wait_for_open_minutes * 60:
                    log.info("Market closed. Exiting.")
                    break
            try:
                self.tick(market_open=stocks_live)
            except Exception as e:
                log.exception("tick failed: %s", e)
            time.sleep(self.cfg.loop_seconds)

        # Crypto is only watched while the bot runs, so never leave it unattended.
        if self.crypto_exits and not self.cfg.dry_run:
            for pos_key in list(self.crypto_exits):
                try:
                    self.mkt.close(pos_key)
                except Exception as e:
                    log.error("could not close %s: %s", pos_key, e)
            self.crypto_exits.clear()
            self._save_exits()
        log.info("Bot stopped after %.0f min.", (time.time() - started) / 60)


def enough_volume(df, min_shares) -> bool:
    today = df.index[-1].date()
    return df.loc[df.index.date == today, "volume"].sum() >= min_shares


def main():
    cfg = Config()
    cfg.validate()
    Bot(cfg).run()


if __name__ == "__main__":
    main()
