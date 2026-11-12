"""Check moomoo prices against what the bot has today.   Run on the Pi:  moomootest   (or: moomootest SOFI TTEC)"""
import sys
import time
from datetime import datetime

from .broker import AlpacaBroker
from .config import SCConfig
from .moomoo_feed import NY, MoomooFeed


def main():
    syms = [s.upper() for s in sys.argv[1:]] or ["SOFI", "AAPL", "KULR", "SNOA", "TTEC"]
    cfg = SCConfig()
    feed = MoomooFeed(cfg.moomoo_host, cfg.moomoo_port)
    ticks = {}
    for _ in range(6):                                    # the gateway connection takes a few seconds
        ticks = feed.snapshot(syms)
        if ticks:
            break
        feed._down_until = 0
        time.sleep(3)
    print(f"New York time now: {datetime.now(NY):%Y-%m-%d %H:%M:%S}")
    if not ticks:
        print("moomoo: NO DATA. Check 'moomoolog': is OpenD logged in? Does it want a verification code?")
        return
    cfg.moomoo = False
    iex = AlpacaBroker(cfg).quotes(syms)
    print(f"{'stock':<6} {'moomoo price':>12} {'age':>8} {'bid':>8} {'ask':>8} {'shares(M)':>10} | {'IEX price':>10} {'IEX age':>9}")
    for s in syms:
        t, q = ticks.get(s), iex.get(s)
        a = f"{t['price']:>12.4f} {t['age_sec']:>7.0f}s {t['bid']:>8.3f} {t['ask']:>8.3f} {t['shares'] / 1e6:>10.1f}" if t else f"{'-':>12} {'-':>8} {'-':>8} {'-':>8} {'-':>10}"
        b = f"{q.price:>10.4f} {q.age_sec or 0:>8.0f}s" if q else f"{'-':>10} {'-':>9}"
        print(f"{s:<6} {a} | {b}")
    print("\nGood sign: moomoo ages are small (seconds to a few minutes in market hours) and prices look right.")
    print("If it looks right, switch it on with:  moomooon")


if __name__ == "__main__":
    main()
