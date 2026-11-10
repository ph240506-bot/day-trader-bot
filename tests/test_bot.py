import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from bot.config import Config
from bot.indicators import add_all, rsi
from bot.llm import Reviewer, parse
from bot.risk import daily_loss_hit, plan_order
from bot.strategy import Signal, chart_summary, evaluate


def make_bars(prices, vols=None):
    idx = pd.date_range("2026-10-30 09:30", periods=len(prices), freq="5min", tz="America/New_York")
    p = np.asarray(prices, float)
    return pd.DataFrame({"open": p, "high": p * 1.002, "low": p * 0.998, "close": p,
                         "volume": vols if vols is not None else np.full(len(p), 1000.0)}, index=idx)


def downtrend_then_breakout():
    """Slide down, then rip up; returns bars ending exactly on the crossover bar."""
    prices = list(np.linspace(110, 100, 40)) + list(np.linspace(100.8, 108, 10))
    vols = [1000.0] * 40 + [3000.0] * 10
    full = add_all(make_bars(prices, vols))
    gap = full["ema9"] - full["ema21"]
    cross = next(i for i in range(41, len(full)) if gap.iloc[i - 1] <= 0 < gap.iloc[i])
    return make_bars(prices[:cross + 1], vols[:cross + 1])


def test_indicators_have_no_gaps():
    df = add_all(downtrend_then_breakout())
    assert not df[["ema9", "ema21", "rsi", "atr", "vwap", "macd_hist"]].iloc[25:].isna().any().any()
    assert 0 <= rsi(df["close"]).min() and rsi(df["close"]).max() <= 100


def test_bullish_crossover_detected():
    bars = downtrend_then_breakout()
    sig = None
    for i in range(30, len(bars) + 1):  # replay bar by bar like the live loop
        s = evaluate("TEST", bars.iloc[:i])
        sig = s or sig
    assert sig and sig.side == "buy"
    assert sig.score >= 3 and "EMA9 crossed above EMA21" in sig.reasons


def test_no_signal_on_flat_market():
    assert evaluate("FLAT", make_bars([100.0] * 60)) is None


def test_crypto_never_shorts():
    prices = list(np.linspace(100, 110, 40)) + [108, 105, 102]
    for i in range(30, len(prices) + 1):
        s = evaluate("BTC/USD", make_bars(prices[:i]), allow_short=False)
        assert s is None or s.side == "buy"


def test_position_sizing_respects_risk_and_cap():
    cfg = Config()
    plan = plan_order("buy", price=100, atr=2, equity=100_000, cfg=cfg, fractional=False)
    # risk 0.5% of 100k = $500, stop distance 1.5*2 = $3 -> 166 shares, cap 10% = 100 shares
    assert plan.qty == 100
    assert plan.stop == 97 and plan.target == 106
    short = plan_order("sell", 100, 2, 100_000, cfg, False)
    assert short.stop == 103 and short.target == 94


def test_daily_loss_limit():
    cfg = Config()
    assert daily_loss_hit(97_900, 100_000, cfg)
    assert not daily_loss_hit(98_500, 100_000, cfg)


def test_live_trading_is_latched(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k"); monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(SystemExit):
        Config().validate()


def test_llm_parse_is_defensive():
    assert parse('Sure! {"decision":"APPROVE","confidence":0.7,"reason":"ok"}')["decision"] == "approve"
    assert parse('{"decision":"maybe"}')["decision"] == "veto"
    with pytest.raises(ValueError):
        parse("no json here")


class FakeLLM(BaseHTTPRequestHandler):
    reply = '{"decision":"approve","confidence":0.8,"reason":"trend and volume agree"}'

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["messages"][0]["role"] == "system"
        out = {"id": "x", "object": "chat.completion", "created": 0, "model": "auto",
               "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": self.reply}}]}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_reviewer_against_openai_compatible_server(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{srv.server_port}/v1")
    monkeypatch.setenv("LLM_API_KEY", "freellmapi-test")
    cfg = Config()
    sig = Signal("AAPL", "buy", 4, 100.0, 1.0, ["x"])
    out = Reviewer(cfg).review(sig, chart_summary(downtrend_then_breakout()), ["10:00 Apple beats"])
    srv.shutdown()
    assert out["decision"] == "approve"


def test_reviewer_fails_safe_when_llm_down(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("LLM_API_KEY", "x")
    out = Reviewer(Config()).review(Signal("AAPL", "buy", 4, 100, 1, []), {}, [])
    assert out["decision"] == "veto"


def test_full_tick_with_fake_broker(monkeypatch, tmp_path):
    """One loop pass end-to-end: bars -> signal -> LLM -> sizing -> order -> journal."""
    import bot.main as m

    monkeypatch.setenv("ALPACA_API_KEY", "k"); monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "trades.csv")); monkeypatch.setenv("STRATEGY", "ema")
    cfg = Config()
    orders = []
    bars = downtrend_then_breakout()
    # add one still-forming bar the bot must ignore
    bars = pd.concat([bars, make_bars([105.2]).set_index(bars.index[-1:] + pd.Timedelta("5min"))])

    class FakeMarket:
        trading = None
        def __init__(self, cfg): pass
        def account(self): return SimpleNamespace(equity="100000", last_equity="100000")
        def bars(self, syms): return {"AAPL": bars}
        def positions(self): return {}
        def has_open_order(self, s): return False
        def headlines(self, s): return ["09:45 Apple unveils new chip"]
        def enter(self, sym, side, plan): orders.append((sym, side, plan))
        def latest_price(self, s): return None

    class FakeReviewer:
        def __init__(self, cfg): self.client = True
        def review(self, sig, chart, news): return {"decision": "approve", "confidence": 1, "reason": "ok"}

    monkeypatch.setattr(m, "Market", FakeMarket)
    monkeypatch.setattr(m, "Reviewer", FakeReviewer)

    class FakeScanner:
        shortable = set()
        def __init__(self, cfg, trading): pass
        def refresh(self): return ["MOVR"]  # a mover with no bars must be harmless
    monkeypatch.setattr(m, "MoverScanner", FakeScanner)
    sent = []
    monkeypatch.setattr(m.Bot, "_in_entry_window", lambda self: True)
    b = m.Bot(cfg)
    b.notify.send = lambda kind, title, msg, *a: sent.append((kind, title))
    b.tick(market_open=True)
    b.tick(market_open=True)  # same bar again must not double-order
    assert len(orders) == 1 and orders[0][1] == "buy"
    assert sent and sent[0][0] == "trade" and sent[0][1].startswith("BUY AAPL")
    journal = (tmp_path / "trades.csv").read_text()
    assert "ordered" in journal and "AAPL" in journal


def test_mover_filter_drops_junk():
    from bot.scanner import pick_movers
    M = lambda s, pct, price: SimpleNamespace(symbol=s, percent_change=pct, price=price)
    raw = [M("PENNY", 80, 0.9), M("ABCDW", 60, 12), M("BIGUP", 25, 40), M("DUMP", -18, 22),
           M("SMALL", 1.5, 50), M("HALTD", 30, 15), M("BIGUP", 25, 40)]
    assets = {"BIGUP": SimpleNamespace(tradable=True, status="active"),
              "DUMP": SimpleNamespace(tradable=True, status="active"),
              "SMALL": SimpleNamespace(tradable=True, status="active"),
              "HALTD": SimpleNamespace(tradable=False, status="active")}
    picked = [m.symbol for m in pick_movers(raw, Config(), assets.get)]
    assert picked == ["BIGUP", "DUMP"]  # biggest moves first, losers included


def test_volume_gate():
    from bot.main import enough_volume
    df = make_bars([10.0] * 10, np.full(10, 1000.0))
    assert enough_volume(df, 5000) and not enough_volume(df, 50000)


def test_notifier_posts_to_ntfy(monkeypatch):
    from bot.notify import Notifier
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.path, self.headers["Title"], self.headers["Priority"],
                        self.rfile.read(int(self.headers["Content-Length"])).decode()))
            self.send_response(200); self.end_headers()
        def log_message(self, *a): pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NTFY_TOPIC", "pat-bot-abc123")
    monkeypatch.setenv("NTFY_SERVER", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("NOTIFY_ON", "TRADE,VETO")
    n = Notifier(Config())
    n.send("trade", "BUY NVDA x12", "Entry ~$185.00", 4, "chart_with_upwards_trend")
    n.send("near_miss", "should be filtered", "x")
    srv.shutdown()
    assert got == [("/pat-bot-abc123", "BUY NVDA x12", "4", "Entry ~$185.00")]


def test_notifier_never_raises(monkeypatch):
    from bot.notify import Notifier
    monkeypatch.setenv("NTFY_TOPIC", "t"); monkeypatch.setenv("NTFY_SERVER", "http://127.0.0.1:9")
    Notifier(Config()).send("trade", "x", "y")  # server down: must not crash the bot


def test_crypto_exits_survive_restart(monkeypatch, tmp_path):
    import bot.main as m
    monkeypatch.setenv("ALPACA_API_KEY", "k"); monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "trades.csv"))

    class FakeMarket:
        trading = None
        def __init__(self, cfg): pass
    monkeypatch.setattr(m, "Market", FakeMarket)
    monkeypatch.setattr(m, "MoverScanner", lambda cfg, t: None)
    b = m.Bot(Config())
    b.crypto_exits["BTCUSD"] = ("buy", 70000.0, 74000.0)
    b._save_exits()
    assert m.Bot(Config()).crypto_exits == {"BTCUSD": ("buy", 70000.0, 74000.0)}


def test_loss_limit_resets_next_day(monkeypatch, tmp_path):
    import datetime as dt
    import bot.main as m
    monkeypatch.setenv("ALPACA_API_KEY", "k"); monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "trades.csv"))
    monkeypatch.setenv("TRADE_CRYPTO", "false")

    class FakeMarket:
        trading = None
        equity = "97000"
        def __init__(self, cfg): pass
        def account(self): return SimpleNamespace(equity=FakeMarket.equity, last_equity="100000")
        def positions(self): return {}
        def flatten_all(self): pass
    monkeypatch.setattr(m, "Market", FakeMarket)
    monkeypatch.setattr(m, "MoverScanner", lambda cfg, t: None)
    b = m.Bot(Config())
    b.notify.send = lambda *a: None
    b.tick(market_open=False)
    assert b.halted
    b.halted_on = dt.date(2000, 1, 1)   # pretend it fired on an earlier day
    FakeMarket.equity = "100000"
    b.tick(market_open=False)
    assert not b.halted


def test_llm_parse_handles_fences_cutoff_and_thinking():
    # the exact reply from the phone alert: code fence + cut off mid-JSON
    out = parse('```json\n{"decision": "approve", "confidence": 0.7, "reason": "trend and vol')
    assert out["decision"] == "approve" and out["confidence"] == 0.7 and "cut off" in out["reason"]
    assert parse('```json\n{"decision": "approve')["decision"] == "approve"
    full = '```json\n{"decision": "veto", "confidence": 0.9, "reason": "earnings today"}\n```'
    assert parse(full) == {"decision": "veto", "confidence": 0.9, "reason": "earnings today"}
    assert parse('<think>maybe {"decision":"approve"}? no.</think>{"decision":"veto","reason":"x"}')["decision"] == "veto"



def _main_bot(monkeypatch, tmp_path, latest=None, spy_trend=None, window=True):
    """A main bot on fake data with one fresh buy signal on AAPL."""
    import bot.main as m
    monkeypatch.setenv("ALPACA_API_KEY", "k"); monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "trades.csv"))
    bars = downtrend_then_breakout()
    bars = pd.concat([bars, make_bars([105.2]).set_index(bars.index[-1:] + pd.Timedelta("5min"))])
    orders = []
    spy = None
    if spy_trend:
        spy = make_bars(np.linspace(500, 510, 30) if spy_trend == "up" else np.linspace(510, 500, 30))

    class FakeMarket:
        trading = None
        def __init__(self, cfg): pass
        def account(self): return SimpleNamespace(equity="100000", last_equity="100000")
        def bars(self, syms):
            out = {"AAPL": bars} if "AAPL" in syms or len(syms) > 1 else {}
            if spy is not None and "SPY" in syms:
                out["SPY"] = spy
            return out
        def positions(self): return {}
        def has_open_order(self, s): return False
        def headlines(self, s): return []
        def enter(self, sym, side, plan): orders.append((sym, side, plan))
        def latest_price(self, s): return latest

    class FakeReviewer:
        def __init__(self, cfg): self.client = True
        def review(self, *a): return {"decision": "approve", "confidence": 1, "reason": "ok"}

    monkeypatch.setattr(m, "Market", FakeMarket)
    monkeypatch.setattr(m, "Reviewer", FakeReviewer)
    monkeypatch.setattr(m, "MoverScanner", lambda cfg, t: None)
    monkeypatch.setattr(m.Bot, "_in_entry_window", lambda self: window)
    b = m.Bot(Config())
    b.cfg.stocks = ["AAPL"]
    b.notify.send = lambda *a: None
    return b, orders, bars


def test_orders_are_repriced_from_latest_trade(monkeypatch, tmp_path):
    probe, _, bars = _main_bot(monkeypatch, tmp_path)
    sig_price = float(bars["close"].iloc[-2])
    now_price = sig_price + 0.3
    (tmp_path / "b").mkdir()
    b, orders, _ = _main_bot(monkeypatch, tmp_path / "b", latest=now_price)
    b.tick(market_open=True)
    plan = orders[0][2]
    assert plan.stop < now_price < plan.target              # built around the CURRENT price
    assert abs((plan.target - now_price) - 2 * (now_price - plan.stop)) <= 0.02   # 2:1, within rounding


def test_skips_when_price_ran_away(monkeypatch, tmp_path):
    b, orders, _ = _main_bot(monkeypatch, tmp_path, latest=1000.0)
    b.tick(market_open=True)
    assert orders == []


def test_entry_window_blocks_stock_entries(monkeypatch, tmp_path):
    b, orders, _ = _main_bot(monkeypatch, tmp_path, window=False)
    b.tick(market_open=True)
    assert orders == []


def test_market_filter(monkeypatch, tmp_path):
    b, orders, _ = _main_bot(monkeypatch, tmp_path, spy_trend="down")    # market falling: no new longs
    b.tick(market_open=True)
    assert orders == []
    (tmp_path / "up").mkdir()
    b, orders, _ = _main_bot(monkeypatch, tmp_path / "up", spy_trend="up")
    b.tick(market_open=True)
    assert len(orders) == 1 and orders[0][1] == "buy"


def test_entry_window_times(monkeypatch):
    import bot.main as m
    from datetime import datetime as real_dt
    b = m.Bot.__new__(m.Bot)
    b.cfg = Config()
    def at(h, mi, to_close):
        class FakeDT(real_dt):
            @classmethod
            def now(cls, tz=None): return real_dt(2026, 11, 3, h, mi, tzinfo=tz)
        monkeypatch.setattr(m, "datetime", FakeDT)
        b.mins_to_close = to_close
        return b._in_entry_window()
    assert at(9, 35, 385) is False       # first 10 minutes
    assert at(9, 45, 375) is True
    assert at(15, 40, 20) is False       # last 30 minutes
    assert at(12, 40, 20) is False       # early-close day: 20 min left at 12:40


def test_llm_tries_next_model_when_reply_unreadable(monkeypatch):
    """Night 1: a free model 'thought out loud' and never gave an answer -> bot vetoed good setups."""
    from bot.llm import Reviewer
    from bot.strategy import Signal
    replies = {"auto:fast": "The user wants me to act as a cautious intraday trading risk reviewer and I need to",
               "auto": '{"decision": "approve", "confidence": 0.8, "reason": "trend and volume agree"}'}
    asked = []

    class FakeCompletions:
        def create(self, model, **kw):
            asked.append(model)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=replies[model]))])

    monkeypatch.setenv("LLM_API_KEY", "x")
    r = Reviewer(Config())
    r.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    out = r.review(Signal("NVDA", "buy", 4, 180.0, 1.0, []), {}, [])
    assert asked == ["auto:fast", "auto"] and out["decision"] == "approve"
    replies["auto"] = "still thinking..."
    out = r.review(Signal("NVDA", "buy", 4, 180.0, 1.0, []), {}, [])
    assert out["decision"] == "veto" and "LLM skipped" in out["reason"]


def test_cooldown_after_position_closes(monkeypatch, tmp_path):
    """Night 3: TSLA stopped out, then traded again 8 min later. Now it waits REENTRY_COOLDOWN_MIN."""
    import time
    import bot.main as m
    monkeypatch.setenv("ALPACA_API_KEY", "k"); monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "trades.csv")); monkeypatch.setenv("STRATEGY", "ema")
    monkeypatch.setenv("SCAN_MOVERS", "false"); monkeypatch.setenv("TRADE_CRYPTO", "false")
    orders, held = [], {"AAPL": object()}
    bars = downtrend_then_breakout()
    bars = pd.concat([bars, make_bars([105.2]).set_index(bars.index[-1:] + pd.Timedelta("5min"))])

    class FakeMarket:
        trading = None
        def __init__(self, cfg): pass
        def account(self): return SimpleNamespace(equity="100000", last_equity="100000")
        def bars(self, syms): return {"AAPL": bars}
        def positions(self): return dict(held)
        def has_open_order(self, s): return False
        def headlines(self, s): return []
        def enter(self, sym, side, plan): orders.append(sym)
        def latest_price(self, s): return None

    class FakeReviewer:
        def __init__(self, cfg): self.client = True
        def review(self, sig, chart, news): return {"decision": "approve", "confidence": 1, "reason": "ok"}

    monkeypatch.setattr(m, "Market", FakeMarket)
    monkeypatch.setattr(m, "Reviewer", FakeReviewer)
    monkeypatch.setattr(m.Bot, "_in_entry_window", lambda self: True)
    b = m.Bot(Config())
    b.notify.send = lambda *a: None
    b.tick(market_open=True)                 # holding AAPL: nothing to do
    held.clear(); b.seen.clear()
    b.tick(market_open=True)                 # AAPL just closed: cooldown, no new order
    assert orders == [] and "AAPL" in b.last_exit
    b.last_exit["AAPL"] = time.time() - 31 * 60; b.seen.clear()
    b.tick(market_open=True)                 # 31 min later: allowed again
    assert orders == ["AAPL"]


# ---------------- backtest ----------------
def _day_bars(day, prices, start="09:30"):
    idx = pd.date_range(f"{day} {start}", periods=len(prices), freq="5min", tz="America/New_York")
    p = np.array(prices, dtype=float)
    return pd.DataFrame({"open": p, "high": p + 0.2, "low": p - 0.2, "close": p, "volume": 1000.0}, index=idx)


class SlowAlpaca:
    """Behaves like Alpaca on 9 Nov: stop orders hold the shares, closing orders need a few polls to fill,
    and cancel_orders() cancels EVERYTHING open, closing orders included."""

    def __init__(self, symbols, fill_after=2, stuck=()):
        self.pos = set(symbols)
        self.orders = [{"symbol": s, "kind": "stop", "age": 0} for s in symbols]
        self.fill_after, self.stuck = fill_after, set(stuck)
        self.cancel_calls, self.closes_sent, self.closes_cancelled = 0, [], 0

    def _step(self):                                   # time passes on every query
        for o in list(self.orders):
            if o["kind"] == "close":
                o["age"] += 1
                if o["age"] >= self.fill_after and o["symbol"] not in self.stuck:
                    self.orders.remove(o)
                    self.pos.discard(o["symbol"])

    def cancel_orders(self):
        self.cancel_calls += 1
        self.closes_cancelled += sum(o["kind"] == "close" for o in self.orders)
        self.orders = []

    def get_orders(self, req):
        self._step()
        return [SimpleNamespace(symbol=o["symbol"]) for o in self.orders]

    def get_all_positions(self):
        return [SimpleNamespace(symbol=s) for s in sorted(self.pos)]

    def close_position(self, sym):
        if any(o["symbol"] == sym and o["kind"] == "stop" for o in self.orders):
            raise RuntimeError("insufficient qty available for order (held_for_orders)")
        self.closes_sent.append(sym)
        self.orders.append({"symbol": sym, "kind": "close", "age": 0})


def test_flatten_cancels_once_and_never_cancels_its_own_closes(monkeypatch):
    """9 Nov: seven positions survived the close because every retry cancelled the previous retry's sells."""
    import time
    from bot.market import Market
    monkeypatch.setattr(time, "sleep", lambda s: None)
    m = Market.__new__(Market)
    m.trading = SlowAlpaca(["AVGO", "HIMS", "KORU", "NVDX", "SOXL", "SOXX", "VOO"], fill_after=3)
    assert m.flatten_all() == []
    assert m.trading.cancel_calls == 1 and m.trading.closes_cancelled == 0
    assert sorted(m.trading.closes_sent) == ["AVGO", "HIMS", "KORU", "NVDX", "SOXL", "SOXX", "VOO"]   # one each


def test_flatten_reports_what_is_really_stuck(monkeypatch):
    import time
    from bot.market import Market
    clock = [0.0]
    monkeypatch.setattr(time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(time, "time", lambda: clock[0])
    m = Market.__new__(Market)
    m.trading = SlowAlpaca(["AAPL", "HIMS"], stuck={"HIMS"})
    assert m.flatten_all(timeout=20) == ["HIMS"]
    assert m.trading.closes_sent.count("HIMS") == 1           # didn't spam duplicate closing orders


# ---------------- v9: opening range breakout on stocks in play ----------------
def _orb_history(days=20, open_vol=1000.0, today_open_vol=None, up=True, seed=0):
    """5-min regular-session bars for `days` days; today's first candle is bullish (or bearish)."""
    rng = np.random.default_rng(seed)
    frames = []
    dates = pd.bdate_range(end="2026-11-06", periods=days)
    for k, d in enumerate(dates):
        idx = pd.date_range(f"{d.date()} 09:30", periods=78, freq="5min", tz="America/New_York")
        p = 50 + np.cumsum(rng.normal(0, 0.05, 78))
        df = pd.DataFrame({"open": p, "high": p + 0.1, "low": p - 0.1, "close": p + 0.02, "volume": 500.0}, index=idx)
        df.iloc[0, df.columns.get_loc("volume")] = (today_open_vol if (k == days - 1 and today_open_vol) else open_vol)
        if k == days - 1:    # today's opening candle: 50.00 -> 50.40 (bull) or 50.40 -> 50.00 (bear), high 50.5 low 49.9
            df.iloc[0, [df.columns.get_loc(c) for c in ("open", "high", "low", "close")]] = (
                [50.0, 50.5, 49.9, 50.4] if up else [50.4, 50.5, 49.9, 50.0])
        frames.append(df)
    return pd.concat(frames)


def test_opening_ranges_use_only_past_data():
    from bot.orb import opening_ranges
    df = _orb_history(today_open_vol=5000.0)
    r = opening_ranges(df, 5, 5)
    today = df.index[-1].date()
    o = r[today]
    assert (o["high"], o["low"], o["open"], o["close"]) == (50.5, 49.9, 50.0, 50.4)
    assert o["rel_volume"] == pytest.approx(5.0)              # 5000 vs a typical 1000
    assert 0 < o["atr_d"] < 2                                  # daily ATR from past days, in dollars
    assert df.index[0].date() not in r                         # no history yet on day 1 -> no range


def test_pick_in_play_filters_and_ranks():
    from bot.orb import pick_in_play
    cfg = Config()
    cands = {"HOT": {"close": 20, "atr_d": 1.0, "rel_volume": 6.0, "high": 20.5, "low": 19.5},
             "WARM": {"close": 20, "atr_d": 1.0, "rel_volume": 2.0, "high": 20.5, "low": 19.5},
             "QUIET": {"close": 20, "atr_d": 1.0, "rel_volume": 0.5, "high": 20.5, "low": 19.5},   # not in play
             "PENNY": {"close": 2, "atr_d": 1.0, "rel_volume": 9.0, "high": 2.1, "low": 1.9},      # < min price
             "FLAT": {"close": 20, "atr_d": 0.1, "rel_volume": 9.0, "high": 20.5, "low": 19.5}}    # ATR too small
    assert [s for s, _ in pick_in_play(cands, cfg)] == ["HOT", "WARM"]
    cfg.orb_top_n = 1
    assert [s for s, _ in pick_in_play(cands, cfg)] == ["HOT"]


def test_stop_distance_floors_and_breakout_side():
    from bot.orb import breakout_side, stop_distance
    cfg = Config()
    o = {"high": 50.5, "low": 49.9, "open": 50.0, "close": 50.4, "atr_d": 2.0}
    assert stop_distance(50.5, o, cfg) == pytest.approx(0.2)                 # 10% of a $2 ATR
    o_small = {**o, "atr_d": 0.6}
    assert stop_distance(50.5, o_small, cfg) == pytest.approx(50.5 * 0.003)  # floor: 0.3% of price, not $0.06
    cfg.orb_stop_mode = "range"
    assert stop_distance(50.5, o, cfg) == pytest.approx(0.6)                 # whole range when wider
    assert breakout_side(o, 50.51) == "buy" and breakout_side(o, 50.49) is None and breakout_side(o, 49.0) is None
    bear = {**o, "open": 50.4, "close": 50.0}
    assert breakout_side(bear, 49.89) == "sell" and breakout_side(bear, 49.89, allow_short=False) is None


def test_backtest_orb_day_win_loss_and_costs():
    from bot.backtest import simulate_orb_day
    cfg = Config()
    o = {"high": 50.5, "low": 49.9, "open": 50.0, "close": 50.4, "atr_d": 2.0}
    idx = pd.date_range("2026-11-06 09:30", periods=78, freq="5min", tz="America/New_York")
    base = pd.DataFrame({"open": 50.4, "high": 50.45, "low": 50.35, "close": 50.4, "volume": 100.0}, index=idx)
    # bar 2 (09:40) breaks 50.5, then price climbs to 53 and stays: hold-to-close winner
    win = base.copy()
    win.iloc[2, [1, 3]] = [50.6, 50.55]
    win.iloc[3:, 0:4] = [53.0, 53.1, 52.9, 53.0]
    t = simulate_orb_day("X", win, o, cfg, cost=0.0, stop_frac=0.10, stop_mode="atr", target_r=0, window_min=60, minutes=5)
    assert t.side == "buy" and t.entry == 50.5 and t.dist == pytest.approx(0.2)
    assert t.r == pytest.approx((53.0 - 50.5) / 0.2)                         # exits at the 15:55 bar open
    # same, with a 3R target: capped at +3R
    t3 = simulate_orb_day("X", win, o, cfg, 0.0, 0.10, "atr", 3, 60, 5)
    assert t3.r == pytest.approx(3.0)
    # breakout then an immediate reversal through the stop on the next bar: -1R, worse with costs
    loss = base.copy()
    loss.iloc[2, [1, 3]] = [50.6, 50.55]
    loss.iloc[3, 0:4] = [50.5, 50.5, 49.0, 49.2]
    t = simulate_orb_day("X", loss, o, cfg, 0.0, 0.10, "atr", 0, 60, 5)
    assert t.r == pytest.approx(-1.0)
    tc = simulate_orb_day("X", loss, o, cfg, 0.05, 0.10, "atr", 0, 60, 5)
    assert tc.r < -1.1                                                       # 0.1% round trip on a 0.4% stop hurts
    # no breakout inside the entry window -> no trade
    assert simulate_orb_day("X", base, o, cfg, 0.0, 0.10, "atr", 0, 60, 5) is None
    late = base.copy()
    late.iloc[20, [1, 3]] = [50.6, 50.55]                                    # 11:10, after the 60-min window
    assert simulate_orb_day("X", late, o, cfg, 0.0, 0.10, "atr", 0, 60, 5) is None


def test_backtest_report_runs_end_to_end():
    from bot.backtest import report
    frames = {"HOT": _orb_history(today_open_vol=5000.0, seed=1), "SPY": _orb_history(seed=2)}
    cfg = Config()
    text = "\n".join(report(frames, cfg, 30, 0.05))
    assert "ORB on stocks in play" in text and "Original EMA-cross" in text and "Robust" in text


def test_live_orb_tick_enters_once_with_limit_cap(monkeypatch, tmp_path):
    import bot.main as m
    monkeypatch.setenv("ALPACA_API_KEY", "k"); monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "trades.csv"))
    monkeypatch.setenv("STRATEGY", "orb"); monkeypatch.setenv("TRADE_CRYPTO", "false")
    monkeypatch.setenv("ORB_LLM", "false")
    orders, price = [], {"HOT": 50.4}

    class FakeMarket:
        trading = None
        def __init__(self, cfg): pass
        def account(self): return SimpleNamespace(equity="88000", last_equity="88000")
        def positions(self): return {}
        def has_open_order(self, s): return False
        def headlines(self, s): return []
        def latest_prices(self, syms): return {s: price[s] for s in syms}
        def enter(self, sym, side, plan, limit_price=None): orders.append((sym, side, plan, limit_price))

    class FakeInPlay:
        shortable = {"HOT"}
        def __init__(self, cfg, mkt): pass
        def pick(self, today):
            return [("HOT", {"high": 50.5, "low": 49.9, "open": 50.0, "close": 50.4, "volume": 5000,
                             "atr_d": 2.0, "rel_volume": 5.0})]

    monkeypatch.setattr(m, "Market", FakeMarket)
    monkeypatch.setattr(m, "InPlayScanner", FakeInPlay)
    monkeypatch.setattr(m.Bot, "_orb_window", lambda self: (True, True))
    b = m.Bot(Config())
    sent = []
    b.notify.send = lambda kind, title, *a: sent.append((kind, title))
    b.tick(market_open=True)                     # 50.40: inside the range, nothing
    assert orders == [] and len(b.orb_list) == 1
    price["HOT"] = 50.52
    b.tick(market_open=True)                     # broke 50.50: buy, stop 0.20 below (10% of $2 ATR)
    assert len(orders) == 1
    sym, side, plan, cap = orders[0]
    assert side == "buy" and plan.stop == pytest.approx(50.32) and plan.target is None
    assert cap == pytest.approx(50.52 * 1.002)
    assert plan.qty == min(int(88000 * 0.005 / 0.2), int(88000 * 0.10 / 50.52))
    b.tick(market_open=True)                     # still above: no second trade in the same stock today
    assert len(orders) == 1
    assert any(t.startswith("BUY HOT") for _, t in sent)
    assert "ordered" in (tmp_path / "trades.csv").read_text()


def test_live_orb_skips_a_break_that_already_ran(monkeypatch, tmp_path):
    import bot.main as m
    monkeypatch.setenv("ALPACA_API_KEY", "k"); monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "t.csv")); monkeypatch.setenv("STRATEGY", "orb")
    monkeypatch.setenv("TRADE_CRYPTO", "false"); monkeypatch.setenv("ORB_LLM", "false")
    orders = []

    class FakeMarket:
        trading = None
        def __init__(self, cfg): pass
        def account(self): return SimpleNamespace(equity="88000", last_equity="88000")
        def positions(self): return {}
        def has_open_order(self, s): return False
        def headlines(self, s): return []
        def latest_prices(self, syms): return {s: 51.5 for s in syms}   # $1 past a 50.50 break, $0.20 stop
        def enter(self, *a, **k): orders.append(a)

    class FakeInPlay:
        shortable = set()
        def __init__(self, cfg, mkt): pass
        def pick(self, today):
            return [("HOT", {"high": 50.5, "low": 49.9, "open": 50.0, "close": 50.4, "volume": 1, "atr_d": 2.0, "rel_volume": 3.0})]

    monkeypatch.setattr(m, "Market", FakeMarket); monkeypatch.setattr(m, "InPlayScanner", FakeInPlay)
    monkeypatch.setattr(m.Bot, "_orb_window", lambda self: (True, True))
    b = m.Bot(Config()); b.notify.send = lambda *a: None
    b.tick(market_open=True)
    assert orders == [] and "HOT" in b.orb_done


def test_leftovers_closed_at_the_open_but_never_mid_day(monkeypatch, tmp_path):
    import datetime as real
    import bot.main as m
    monkeypatch.setenv("ALPACA_API_KEY", "k"); monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "t.csv")); monkeypatch.setenv("STRATEGY", "orb")
    flattened = []

    class FakeMarket:
        trading = None
        def __init__(self, cfg): pass
        def positions(self): return {"HIMS": 1, "VOO": 1}
        def flatten_all(self): flattened.append(1); return []

    class FakeInPlay:
        shortable = set()
        def __init__(self, cfg, mkt): pass

    now = {"t": real.datetime(2026, 11, 10, 11, 0, tzinfo=m.NY)}

    class FakeDT(real.datetime):
        @classmethod
        def now(cls, tz=None): return now["t"]

    monkeypatch.setattr(m, "Market", FakeMarket); monkeypatch.setattr(m, "InPlayScanner", FakeInPlay)
    monkeypatch.setattr(m, "datetime", FakeDT)
    b = m.Bot(Config()); sent = []
    b.notify.send = lambda kind, title, *a: sent.append(title)
    b._close_leftovers()                                   # 11:00, e.g. after a botrestart: today's trades are safe
    assert flattened == []
    now["t"] = real.datetime(2026, 11, 11, 9, 31, tzinfo=m.NY)
    b._close_leftovers()                                   # next morning, 9:31: leftovers go
    assert flattened == [1] and "HIMS" in sent[0]
    b._close_leftovers()                                   # only once per day
    assert flattened == [1]
