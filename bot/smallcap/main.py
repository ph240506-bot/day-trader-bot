"""Run small-cap mode:  python -m bot.smallcap.main"""
import asyncio
import logging
import os
import signal
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import Config
from ..notify import Notifier
from .brain import Brain
from .broker import AlpacaBroker
from .config import SCConfig
from .engine import Engine, session
from .feeds import HaltWatch, edgar_poller, movers_scanner, news_stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("smallcap")
for noisy in ("httpx", "httpx2", "openai", "websockets", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


class _OneLine(logging.Filter):
    """Alpaca's websocket prints a full traceback on every reconnect; keep just the message."""
    def filter(self, record):
        record.exc_info, record.exc_text = None, None
        return True


logging.getLogger("alpaca.data.live.websocket").addFilter(_OneLine())
NY = ZoneInfo("America/New_York")


async def worker(queue, engine, stats):
    while True:
        ev = await queue.get()
        try:
            action = await engine.handle(ev)
            stats[action] = stats.get(action, 0) + 1
            if action not in ("dupe", "stale", "skip-price", "skip-asset", "skip-session", "skip-sym"):
                log.info("%s %s [%s] -> %s (%.0f ms) %s", ev.source, ev.symbol, ev.form or "news", action,
                         (time.time() - ev.received) * 1000, ev.text[:90])
        except Exception as e:
            if any(w in type(e).__name__ + str(e) for w in ("Connection", "Timeout", "RemoteDisconnected")):
                log.warning("event %s skipped: network hiccup (%s)", ev.symbol, type(e).__name__)
            else:
                log.exception("event %s failed: %s", ev.symbol, e)
        finally:
            queue.task_done()


async def exit_loop(engine, cfg, stop):
    while not stop.is_set():
        try:
            await engine.tick()               # exits + pullback watchlist, one batched price request
            await engine.check_daily_loss()
            await engine.maybe_daily_summary()
        except Exception as e:
            log.exception("exit loop: %s", e)
        await asyncio.sleep(cfg.exit_poll_sec)


async def heartbeat(engine, broker, stats, stop):
    while not stop.is_set():
        await asyncio.sleep(300)
        try:
            is_open, *_ = await asyncio.to_thread(broker.clock)
            equity, last = await asyncio.to_thread(broker.account)
            seen = sum(stats.values())
            ignored = sum(v for k, v in stats.items() if k.startswith("skip") or k in ("dupe", "stale"))
            log.info("Status: %s | events %d (ignored %d: big caps/OTC/repeats, filtered %d, passed %d, watching %d, "
                     "bought %d, traps %d) | parked for the open: %d | equity $%s (today %+.2f) | watchlist: %s | positions: %s",
                     session(datetime.now(NY), is_open), seen, ignored, stats.get("filtered", 0) + stats.get("parked", 0), stats.get("pass", 0),
                     stats.get("watching", 0), stats.get("bought", 0), stats.get("trap", 0), len(engine.parked), f"{equity:,.2f}",
                     equity - last,
                     ", ".join(f"{w.symbol} ({w.state}, high {w.high:.2f})" for w in engine.watching.values()) or "none",
                     ", ".join(f"{p.symbol} x{p.qty:g} @ {p.entry:.2f} stop {p.stop:.2f}"
                               for p in engine.positions.values()) or "none")
        except Exception as e:
            log.warning("heartbeat: %s", e)


async def run():
    cfg = SCConfig()
    cfg.validate()
    notify = Notifier(Config())
    broker = AlpacaBroker(cfg, trading_url=os.getenv("ALPACA_TRADING_URL_OVERRIDE") or None,
                          data_url=os.getenv("ALPACA_DATA_URL_OVERRIDE") or None)
    brain = Brain(cfg)
    halts = HaltWatch()
    engine = Engine(cfg, broker, brain, notify, halts)
    queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    # Checks are network-bound: more threads so a burst of headlines doesn't queue up.
    from concurrent.futures import ThreadPoolExecutor
    loop.set_default_executor(ThreadPoolExecutor(max_workers=32))
    stats: dict[str, int] = {}
    log.info("Small-cap mode starting. paper=%s dry_run=%s entry=%s AI=%s models=%s finnhub=%s sec=%s scanner=%s",
             cfg.paper, cfg.dry_run, cfg.entry_mode, "on" if brain.client else "off", ",".join(cfg.llm_models),
             "on" if cfg.finnhub_key else "off", "on" if cfg.edgar_user_agent else "off",
             f"every {cfg.scan_sec:.0f}s" if cfg.scan_movers else "off")
    tasks = [asyncio.create_task(c) for c in (
        news_stream(queue, cfg, stop), edgar_poller(queue, cfg, stop), halts.run(cfg, stop),
        exit_loop(engine, cfg, stop), heartbeat(engine, broker, stats, stop),
        *([movers_scanner(queue, cfg, stop)] if cfg.scan_movers else []),
        *[worker(queue, engine, stats) for _ in range(12)])]
    await stop.wait()
    log.info("Stopping; open positions stay managed on restart (saved to disk).")
    for t in tasks:
        t.cancel()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
