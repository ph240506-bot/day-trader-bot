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
    monkeypatch.setenv("JOURNAL_PATH", str(tmp_path / "trades.csv"))
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
    b = m.Bot(cfg)
    b.tick(market_open=True)
    b.tick(market_open=True)  # same bar again must not double-order
    assert len(orders) == 1 and orders[0][1] == "buy"
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
