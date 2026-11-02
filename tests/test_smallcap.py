import asyncio
import json
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import bot.smallcap.engine as eng
from bot.smallcap.brain import Brain, parse
from bot.smallcap.broker import AlpacaBroker, Quote, RateLimiter, round_price
from bot.smallcap.catalysts import triage, triage_form
from bot.smallcap.config import SCConfig
from bot.smallcap.engine import Engine, Watch, pullback_step, session
from bot.smallcap.feeds import Event, load_cik_map, parse_edgar_atom, parse_halts

NY = eng.NY


def run(c):
    return asyncio.run(c)


# ======================= fakes =======================
class FakeBroker:
    def __init__(self, quote, shares=10e6, vwap=None):
        self.q = quote
        self.per_symbol = {}
        self.shares = shares
        self.vwap_val = vwap
        self.orders, self.placed = {}, []
        self.fill_mode = "full"     # full | none | partial
        self.equity = (1000.0, 1000.0)

    def clock(self): return True, None, None, None
    def asset_ok(self, s): return True
    def quote(self, s): return self.per_symbol.get(s, self.q)
    def quotes(self, syms, finnhub_for=frozenset()): return {s: self.quote(s) for s in syms}
    def account(self): return self.equity
    def shares_outstanding(self, s): return self.shares
    def vwap(self, s): return self.vwap_val

    def limit(self, sym, qty, side, price, extended):
        oid = f"o{len(self.placed)}"
        self.placed.append((sym, qty, side, price, extended))
        if self.fill_mode == "full":
            self.orders[oid] = ("filled", qty, price)
        elif self.fill_mode == "partial":
            self.orders[oid] = ("partially_filled", qty / 2, price)
            self.fill_mode = "full"
        else:
            self.orders[oid] = ("new", 0, 0)
        return oid

    def market_sell(self, sym, qty): return self.limit(sym, qty, "sell", self.q.price, False)
    def order(self, oid): return self.orders[oid]

    def cancel(self, oid):
        st, f, p = self.orders[oid]
        self.orders[oid] = ("canceled", f, p)


class FakeBrain:
    def __init__(self, verdict):
        self.verdict, self.calls = verdict, 0

    async def judge(self, sym, text, ctx):
        self.calls += 1
        return self.verdict


class Sink:
    def __init__(self): self.sent = []
    def send(self, kind, title, msg="", *a): self.sent.append((kind, title))


def cfg(tmp_path, **kw):
    c = SCConfig()
    c.journal_path = str(tmp_path / "smallcap.csv")
    c.fill_timeout_sec = 1
    c.entry_mode = "immediate"
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def mover(price=4.0, prev=3.0, bid=None, ask=None, age=1.0):
    return Quote(price, prev, bid or price - 0.01, ask or price + 0.01, 3_000_000, 1_000_000, age_sec=age)


def ev(sym="ABCD", text="ABCD receives FDA approval for lead drug", source="news", form="", age=5):
    return Event(sym, text, source, datetime.now(timezone.utc) - timedelta(seconds=age), form)


STRONG = {"verdict": "strong", "confidence": 0.85, "reason": "FDA approval is material", "model": "fast"}


@pytest.fixture(autouse=True)
def regular_session(monkeypatch):
    monkeypatch.setattr(eng, "session", lambda now, is_open: "regular")


# ======================= instant triage =======================
def test_triage_rules():
    assert triage("XYZ announces $10M registered direct offering")[0] == "trap"
    assert triage("XYZ prices underwritten public offering of common stock")[0] == "trap"
    assert triage("XYZ announces 1-for-20 reverse stock split")[0] == "trap"
    assert triage("XYZ receives FDA approval for Drug A")[0] == "strong"
    assert triage("XYZ signs definitive merger agreement with BigCo")[0] == "strong"
    assert triage("XYZ to present at investor conference")[0] == "neutral"
    assert triage("XYZ wins $5M contract; announces at-the-market offering")[0] == "trap"
    assert triage_form("424B5") == "trap" and triage_form("8-K") == "neutral" and triage_form("10-Q") == "ignore"


# ======================= immediate mode =======================
def test_strong_news_on_mover_buys_with_risk_sizing(tmp_path):
    b, sink = FakeBroker(mover()), Sink()
    e = Engine(cfg(tmp_path), b, FakeBrain(STRONG), sink)
    assert run(e.handle(ev())) == "bought"
    sym, qty, side, price, extended = b.placed[0]
    limit = round_price(4.01 * 1.01)
    stop = round_price(4.0 * 0.95)
    # $200 would buy 49 shares, but 1% risk ($10) / (4.05 - 3.80) caps it at 40
    assert side == "buy" and price == limit and not extended and qty == 40
    p = e.positions["ABCD"]
    assert p.stop == stop and p.target == round_price(limit + 2 * (limit - stop))
    assert "BUY ABCD" in sink.sent[-1][1]
    assert "ABCD" in Engine(cfg(tmp_path), b, FakeBrain(STRONG), sink).positions   # survives restart
    assert run(e.handle(ev())) == "dupe"
    assert run(e.handle(ev(text="ABCD FDA approval: more details"))) == "skip-sym"


def test_offering_blocks_later_good_news(tmp_path):
    b, brain = FakeBroker(mover()), FakeBrain(STRONG)
    e = Engine(cfg(tmp_path), b, brain, Sink())
    assert run(e.handle(ev(text="SEC 424B5: ABCD CORP", source="edgar", form="424B5"))) == "trap"
    assert run(e.handle(ev())) == "skip-sym"
    assert brain.calls == 0 and not b.placed


@pytest.mark.parametrize("q,expected", [
    (mover(price=3.1, prev=3.0), "filtered"),     # not moving
    (mover(price=180, prev=150), "skip-price"),   # not a small cap
    (mover(bid=3.8, ask=4.2), "filtered"),        # spread too wide
    (mover(age=600), "filtered"),                 # stale price data
])
def test_filters_before_ai(tmp_path, q, expected):
    brain = FakeBrain(STRONG)
    e = Engine(cfg(tmp_path), FakeBroker(q), brain, Sink())
    assert run(e.handle(ev())) == expected and brain.calls == 0


def test_share_count_filter(tmp_path):
    brain = FakeBrain(STRONG)
    e = Engine(cfg(tmp_path), FakeBroker(mover(), shares=400e6), brain, Sink())
    assert run(e.handle(ev())) == "filtered" and brain.calls == 0
    assert "400M shares" in open(tmp_path / "smallcap.csv").read()
    # unknown share count: allowed by default, blocked if required
    assert run(Engine(cfg(tmp_path), FakeBroker(mover(), shares=None), FakeBrain(STRONG), Sink()).handle(ev("UNKN"))) == "bought"
    assert run(Engine(cfg(tmp_path, require_share_data=True), FakeBroker(mover(), shares=None),
                      FakeBrain(STRONG), Sink()).handle(ev("UNK2"))) == "filtered"


def test_ai_silence_or_weak_means_no_trade(tmp_path):
    b = FakeBroker(mover())
    assert run(Engine(cfg(tmp_path), b, FakeBrain(None), Sink()).handle(ev())) == "pass"
    weak = {"verdict": "weak", "confidence": 0.7, "reason": "vague PR", "model": "x"}
    assert run(Engine(cfg(tmp_path), b, FakeBrain(weak), Sink()).handle(ev(sym="EFGH"))) == "pass"
    assert not b.placed


def test_stale_news_and_naive_timestamps(tmp_path):
    e = Engine(cfg(tmp_path), FakeBroker(mover()), FakeBrain(STRONG), Sink())
    assert run(e.handle(ev(age=600))) == "stale"
    naive = Event("NAIV", "NAIV receives FDA approval", "news", datetime.now(timezone.utc).replace(tzinfo=None))
    assert run(e.handle(naive)) == "bought"


def test_no_fill_cancels(tmp_path):
    b = FakeBroker(mover())
    b.fill_mode = "none"
    e = Engine(cfg(tmp_path), b, FakeBrain(STRONG), Sink())
    assert run(e.handle(ev())) == "no-fill" and not e.positions and b.orders["o0"][0] == "canceled"


def test_limits_respected(tmp_path):
    e = Engine(cfg(tmp_path, max_positions=1), FakeBroker(mover()), FakeBrain(STRONG), Sink())
    assert run(e.handle(ev("AAAA", "AAAA receives FDA approval"))) == "bought"
    assert run(e.handle(ev("BBBB", "BBBB receives FDA approval"))) == "skip-limits"


def test_premarket_uses_extended_hours(tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "session", lambda now, is_open: "premarket")
    b = FakeBroker(mover())
    assert run(Engine(cfg(tmp_path), b, FakeBrain(STRONG), Sink()).handle(ev())) == "bought"
    assert b.placed[0][4] is True
    monkeypatch.setattr(eng, "session", lambda now, is_open: "closed")
    assert run(Engine(cfg(tmp_path), FakeBroker(mover()), FakeBrain(STRONG), Sink()).handle(ev("ZZZZ"))) == "skip-session"


def test_parallel_events_respect_position_limit(tmp_path):
    from bot.smallcap.main import worker

    class SlowBrain(FakeBrain):
        async def judge(self, *a):
            await asyncio.sleep(0.2)
            return STRONG

    e = Engine(cfg(tmp_path, max_positions=2), FakeBroker(mover()), SlowBrain(STRONG), Sink())

    async def go():
        q, stats = asyncio.Queue(), {}
        ws = [asyncio.create_task(worker(q, e, stats)) for _ in range(4)]
        for i in range(4):
            await q.put(ev(f"RUN{i}", f"RUN{i} receives FDA approval"))
        await q.join()
        for w in ws:
            w.cancel()
        return stats
    stats = run(go())
    assert len(e.positions) == 2 and stats["bought"] == 2 and stats["skip-limits"] == 2


# ======================= pullback mode =======================
PB = SimpleNamespace(pullback_min_pct=3.0, pullback_max_pct=15.0, bounce_pct=1.5)


def feed(w, prices):
    return [pullback_step(w, p, PB) for p in prices]


def test_pullback_state_machine():
    w = Watch("X", 0, high=4.00)
    # spike to 4.40, dip to 4.20 (-4.5% = pullback), low 4.15, then a bounce of +1.7% -> buy
    assert feed(w, [4.20, 4.40, 4.30, 4.20, 4.15, 4.18]) == ["wait"] * 6
    assert w.state == "pulling" and w.high == 4.40 and w.low == 4.15
    assert pullback_step(w, 4.22, PB) == "buy"
    w2 = Watch("Y", 0, high=4.00)                 # small wiggles never count as a pullback
    assert feed(w2, [4.05, 4.00, 3.95, 4.02]) == ["wait"] * 4 and w2.state == "spiking"
    w3 = Watch("Z", 0, high=5.00)                 # a collapse is abandoned, not bought
    assert feed(w3, [4.80, 4.50, 4.20]) == ["wait", "wait", "abandon"]


def _watching(tmp_path, vwap=None, **kw):
    b, sink = FakeBroker(mover(price=4.0), vwap=vwap), Sink()
    e = Engine(cfg(tmp_path, entry_mode="pullback", **kw), b, FakeBrain(STRONG), sink)
    assert run(e.handle(ev())) == "watching" and not b.placed
    return e, b, sink


def tick_prices(e, b, prices, age=1.0):
    for p in prices:
        b.q = mover(price=p, age=age)
        run(e.tick())


def test_pullback_buy_with_stop_under_the_low(tmp_path):
    e, b, sink = _watching(tmp_path, vwap=3.90)
    tick_prices(e, b, [4.40, 4.20, 4.10, 4.12])
    assert not b.placed and e.watching["ABCD"].low == 4.10
    tick_prices(e, b, [4.17])                     # +1.7% off the low, above VWAP -> buy
    p = e.positions["ABCD"]
    assert p.stop == round_price(4.10 * 0.99)     # just under the pullback low
    assert "ABCD" not in e.watching and "pullback bounce" in open(tmp_path / "smallcap.csv").read()
    assert any("BUY ABCD" in t for _, t in sink.sent)


def test_pullback_waits_while_below_vwap(tmp_path):
    e, b, sink = _watching(tmp_path, vwap=4.30)
    tick_prices(e, b, [4.40, 4.20, 4.10, 4.17])   # bounce, but still under VWAP 4.30
    assert not e.positions and "ABCD" in e.watching
    b.vwap_val = 4.00
    e.watching["ABCD"].vwap_at = 0                # force a VWAP refresh
    tick_prices(e, b, [4.18])
    assert "ABCD" in e.positions


def test_pullback_abandons_and_expires(tmp_path):
    e, b, sink = _watching(tmp_path)
    tick_prices(e, b, [4.40, 4.00, 3.70])          # -16% from the high: failed move
    assert not e.watching and not b.placed and "faded" in sink.sent[-1][1]
    (tmp_path / "x").mkdir()
    e2, b2, _ = _watching(tmp_path / "x", watch_min=0)
    tick_prices(e2, b2, [4.10])
    assert not e2.watching and "watch-expired" in open(tmp_path / "x" / "smallcap.csv").read()


def test_trap_news_drops_watched_stock(tmp_path):
    e, b, sink = _watching(tmp_path)
    assert run(e.handle(ev(text="ABCD announces $8M registered direct offering"))) == "trap"
    assert not e.watching


def test_ignores_stale_quotes_while_watching(tmp_path):
    e, b, sink = _watching(tmp_path)
    tick_prices(e, b, [4.40, 4.20, 4.10, 4.20], age=300)
    assert e.watching["ABCD"].state == "spiking" and not b.placed


def test_watchlist_cap(tmp_path):
    e, b, sink = _watching(tmp_path, max_watch=1)
    assert run(e.handle(ev("EFGH", "EFGH receives FDA approval"))) == "skip-watchlist-full"


# ======================= exits =======================
def _bought(tmp_path, **kw):
    b, sink = FakeBroker(mover()), Sink()
    e = Engine(cfg(tmp_path, **kw), b, FakeBrain(STRONG), sink)
    run(e.handle(ev()))
    return e, b, sink, e.positions["ABCD"]


def test_stop_loss_exit(tmp_path):
    e, b, sink, p = _bought(tmp_path)
    b.q = mover(price=p.stop - 0.05)
    run(e.tick())
    assert "ABCD" not in e.positions and b.placed[-1][2] == "sell"
    assert "stop-loss" in sink.sent[-1][1] and "SOLD ABCD -$" in sink.sent[-1][1]


def test_trailing_stop_locks_profit(tmp_path):
    e, b, sink, p = _bought(tmp_path, reward_risk=10)    # far target so the trail does the work
    entry = p.entry
    b.q = mover(price=entry * 1.10)
    run(e.tick())
    assert e.positions["ABCD"].stop == round_price(entry * 1.10 * 0.96)
    b.q = mover(price=entry * 1.05)
    run(e.tick())
    assert "ABCD" not in e.positions and "trailing stop" in sink.sent[-1][1] and "+$" in sink.sent[-1][1]


def test_target_and_time_exits(tmp_path):
    e, b, sink, p = _bought(tmp_path)
    b.q = mover(price=p.target + 0.1)
    run(e.tick())
    assert "target" in sink.sent[-1][1]
    (tmp_path / "t2").mkdir()
    e, b, sink, p = _bought(tmp_path / "t2", max_hold_min=0)
    run(e.tick())
    assert "time limit" in sink.sent[-1][1]


def test_partial_exit_then_rest(tmp_path):
    e, b, sink, p = _bought(tmp_path)
    qty = p.qty
    b.fill_mode = "partial"
    b.q = mover(price=p.stop - 0.05)
    run(e.tick())
    sells = [o for o in b.placed if o[2] == "sell"]
    assert sells[0][1] == qty and sells[1][1] == qty / 2 and "ABCD" not in e.positions


def test_daily_loss_limit_sells_and_stops(tmp_path):
    e, b, sink, p = _bought(tmp_path)
    b.equity = (940.0, 1000.0)
    run(e.check_daily_loss())
    assert not e.positions and any("daily loss" in t for _, t in sink.sent)
    assert run(e.handle(ev("NEWW", "NEWW receives FDA approval"))) == "skip-halted-day"


# ======================= feeds =======================
ATOM = b"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>8-K - ACME BIO INC (0001234567) (Filer)</title>
<summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-11-03 Item 8.01 Other Events</summary>
<updated>2026-11-03T08:01:02-05:00</updated><category scheme="https://www.sec.gov/" label="form type" term="8-K"/></entry>
<entry><title>424B5 - DILUTE CORP (0007654321) (Filer)</title><updated>2026-11-03T08:02:00-05:00</updated>
<category term="424B5"/></entry>
<entry><title>10-Q - IGNORED CO (0001111111) (Filer)</title><updated>2026-11-03T08:03:00-05:00</updated>
<category term="10-Q"/></entry>
<entry><title>8-K - UNKNOWN CO (0009999999) (Filer)</title><updated>2026-11-03T08:03:00-05:00</updated>
<category term="8-K"/></entry>
</feed>"""


def test_edgar_parse():
    cik = load_cik_map(json.dumps({"0": {"cik_str": 1234567, "ticker": "acme", "title": "x"},
                                   "1": {"cik_str": 7654321, "ticker": "DLTE", "title": "y"},
                                   "2": {"cik_str": 1111111, "ticker": "IGN", "title": "z"}}).encode())
    evs = parse_edgar_atom(ATOM, cik)
    assert [(e.symbol, e.form) for e in evs] == [("ACME", "8-K"), ("DLTE", "424B5")]
    assert "Item 8.01" in evs[0].text and evs[0].at.tzinfo is not None


HALTS = b"""<?xml version="1.0"?><rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/"><channel>
<item><ndaq:HaltDate>11/03/2026</ndaq:HaltDate><ndaq:IssueSymbol>RUNR</ndaq:IssueSymbol>
<ndaq:ReasonCode>LUDP</ndaq:ReasonCode><ndaq:ResumptionTradeTime></ndaq:ResumptionTradeTime></item>
<item><ndaq:IssueSymbol>BACK</ndaq:IssueSymbol><ndaq:ResumptionTradeTime>10:05:00</ndaq:ResumptionTradeTime></item>
</channel></rss>"""


def test_halts_parse_and_block(tmp_path):
    assert parse_halts(HALTS) == {"RUNR"}

    class H: halted = {"ABCD"}
    e = Engine(cfg(tmp_path), FakeBroker(mover()), FakeBrain(STRONG), Sink(), halts=H())
    assert run(e.handle(ev())) == "skip-halted"


def test_session_windows():
    d = datetime(2026, 11, 3, tzinfo=NY)
    assert session(d.replace(hour=5), False) == "premarket"
    assert session(d.replace(hour=10), True) == "regular"
    assert session(d.replace(hour=15, minute=55), True) == "closing"
    assert session(d.replace(hour=20), False) == "closed"
    assert session(datetime(2026, 11, 7, 6, tzinfo=NY), False) == "closed"


def test_rate_limiter():
    rl = RateLimiter(3, per=60)
    assert [rl.allow() for _ in range(5)] == [True, True, True, False, False]


# ======================= real alpaca-py code vs a fake Alpaca server =======================
def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class FakeAlpaca(BaseHTTPRequestHandler):
    """Speaks Alpaca's real JSON wire format, so AlpacaBroker runs its true parsing code."""
    posted = []

    def _send(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def log_message(self, *a): pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        now = datetime.now(timezone.utc)
        today_bar = {"t": iso(datetime.now(NY).replace(hour=4, minute=0, second=0, microsecond=0)),
                     "o": 3.2, "h": 4.5, "l": 3.1, "c": 4.1, "v": 900000, "n": 5000, "vw": 3.9}
        prev_bar = {"t": iso(now - timedelta(days=1)), "o": 3.0, "h": 3.1, "l": 2.9, "c": 3.0,
                    "v": 300000, "n": 900, "vw": 3.0}
        snap = {"latestTrade": {"t": iso(now - timedelta(seconds=3)), "x": "V", "p": 4.12, "s": 100, "c": ["@"],
                                "i": 1, "z": "C"},
                "latestQuote": {"t": iso(now), "ax": "V", "ap": 4.14, "as": 3, "bx": "V", "bp": 4.10, "bs": 2,
                                "c": ["R"], "z": "C"},
                "minuteBar": today_bar, "dailyBar": today_bar, "prevDailyBar": prev_bar}
        if u.path == "/v2/stocks/snapshots":
            assert q.get("feed") == "iex"
            syms = q["symbols"].split(",")
            return self._send({s: snap for s in syms if s != "NODATA"})
        if u.path == "/v2/stocks/bars":
            assert q["timeframe"] == "1Min" and q.get("feed") == "iex"
            bars = [{"t": iso(now - timedelta(minutes=2)), "o": 4, "h": 4, "l": 4, "c": 4, "v": 1000, "n": 1, "vw": 4.0},
                    {"t": iso(now - timedelta(minutes=1)), "o": 5, "h": 5, "l": 5, "c": 5, "v": 3000, "n": 1, "vw": 5.0}]
            return self._send({"bars": {q["symbols"]: bars}, "next_page_token": None})
        if u.path.startswith("/v2/assets/"):
            sym = u.path.rsplit("/", 1)[1]
            return self._send({"id": "9a1b2c3d-0000-0000-0000-000000000001", "class": "us_equity",
                               "exchange": "OTC" if sym == "OTCX" else "NASDAQ", "symbol": sym, "status": "active",
                               "tradable": True, "marginable": True, "shortable": True, "easy_to_borrow": True,
                               "fractionable": True})
        if u.path == "/v2/account":
            return self._send({"id": "9a1b2c3d-0000-0000-0000-000000000002", "account_number": "PA1",
                               "status": "ACTIVE", "equity": "650.25", "last_equity": "640.00"})
        if u.path == "/v2/clock":
            return self._send({"timestamp": iso(now), "is_open": True, "next_open": iso(now + timedelta(days=1)),
                               "next_close": iso(now + timedelta(hours=3))})
        if u.path.startswith("/v2/orders/"):
            return self._send(self._order("filled", "40", "4.15"))
        self._send({"message": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAlpaca.posted.append(body)
        self._send(self._order("accepted", "0", None))

    def do_DELETE(self):
        self.send_response(204); self.end_headers()

    @staticmethod
    def _order(status, filled, avg):
        now = iso(datetime.now(timezone.utc))
        return {"id": "61e69015-8549-4bfd-b9c3-01e75843f47d", "client_order_id": "c1", "created_at": now,
                "updated_at": now, "submitted_at": now, "filled_at": None,
                "asset_id": "9a1b2c3d-0000-0000-0000-000000000001", "symbol": "ABCD", "asset_class": "us_equity",
                "qty": "40", "filled_qty": filled, "filled_avg_price": avg, "order_class": "simple",
                "order_type": "limit", "type": "limit", "side": "buy", "time_in_force": "day",
                "limit_price": "4.15", "status": status, "extended_hours": True}


@pytest.fixture
def alpaca(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeAlpaca)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"

    def fake_get(u, headers=None, timeout=5):
        if "profile2" in u:
            return {"ticker": "ABCD", "shareOutstanding": 12.5}
        if "finnhub.io/api/v1/quote" in u:
            return {"c": 4.20, "pc": 3.0, "t": int(time.time())}
        if "company_tickers" in u:
            assert headers and "User-Agent" in headers
            return {"0": {"cik_str": 1234567, "ticker": "ABCD", "title": "x"}}
        if "companyconcept/CIK0001234567/" in u:
            return {"units": {"shares": [{"end": "2026-06-30", "val": 9000000, "filed": "2026-08-01"},
                                         {"end": "2026-09-30", "val": 11000000, "filed": "2026-11-01"}]}}
        raise AssertionError(u)

    c = cfg(tmp_path)
    c.alpaca_key, c.alpaca_secret, c.finnhub_key, c.edgar_user_agent = "k", "s", "", ""
    yield AlpacaBroker(c, trading_url=url, data_url=url, http_get=fake_get), c
    srv.shutdown()


def test_real_client_quotes_and_vwap(alpaca):
    broker, c = alpaca
    qs = broker.quotes(["ABCD", "NODATA"])
    q = qs["ABCD"]
    assert "NODATA" not in qs
    assert q.price == 4.12 and q.prev_close == 3.0 and round(q.gap_pct, 1) == 37.3
    assert q.bid == 4.10 and q.ask == 4.14 and q.rel_volume == 3.0 and 0 <= q.age_sec < 30
    assert broker.vwap("ABCD") == pytest.approx((4 * 1000 + 5 * 3000) / 4000)


def test_real_client_finnhub_and_shares(alpaca):
    broker, c = alpaca
    assert broker.quote("ABCD").price == 4.12                # no Finnhub key: Alpaca price
    c.finnhub_key = "fk"
    q = broker.quote("ABCD")
    assert q.price == 4.20 and q.source == "finnhub"
    assert broker.shares_outstanding("ABCD") == 12_500_000   # Finnhub reports millions
    c.finnhub_key, c.edgar_user_agent = "", "test bot me@example.com"
    broker._shares.clear()
    assert broker.shares_outstanding("ABCD") == 11_000_000   # SEC: latest filing wins
    c.edgar_user_agent = ""
    broker._shares.clear()
    assert broker.shares_outstanding("ABCD") is None         # no sources configured


def test_real_client_orders_account_clock_assets(alpaca):
    broker, c = alpaca
    FakeAlpaca.posted.clear()
    oid = broker.limit("ABCD", 40, "buy", 4.1512, True)
    sent = FakeAlpaca.posted[-1]
    assert sent["symbol"] == "ABCD" and sent["side"] == "buy" and sent["type"] == "limit"
    assert float(sent["qty"]) == 40 and float(sent["limit_price"]) == 4.15
    assert sent["time_in_force"] == "day" and sent["extended_hours"] is True
    assert broker.order(oid) == ("filled", 40.0, 4.15)
    broker.cancel(oid)
    broker.market_sell("ABCD", 40)
    assert FakeAlpaca.posted[-1]["type"] == "market" and FakeAlpaca.posted[-1]["side"] == "sell"
    assert broker.account() == (650.25, 640.0)
    is_open, nxt_open, nxt_close, ts = broker.clock()
    assert is_open is True and nxt_close > ts
    assert broker.asset_ok("ABCD") is True and broker.asset_ok("OTCX") is False


def test_full_engine_on_real_client(alpaca):
    """Whole fast path through real alpaca-py parsing: headline -> checks -> AI -> limit buy -> fill."""
    broker, c = alpaca
    FakeAlpaca.posted.clear()
    e = Engine(c, broker, FakeBrain(STRONG), Sink())
    assert run(e.handle(ev())) == "bought"
    assert e.positions["ABCD"].entry == 4.15 and FakeAlpaca.posted[0]["side"] == "buy"


# ======================= AI race =======================
class RaceLLM(BaseHTTPRequestHandler):
    delays = {"slow": 3.0, "fast": 0.1}
    replies = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        time.sleep(self.delays.get(body["model"], 0))
        content = self.replies.get(body["model"]) or json.dumps(
            {"verdict": "strong", "confidence": 0.9, "reason": f"from {body['model']}"})
        out = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                          "choices": [{"index": 0, "finish_reason": "stop",
                                       "message": {"role": "assistant", "content": content}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a): pass


def test_ai_race_fastest_wins_garbage_skipped_timeout(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), RaceLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = cfg(tmp_path, llm_base_url=f"http://127.0.0.1:{srv.server_port}/v1", llm_api_key="k",
            llm_models=["slow", "fast"], llm_timeout_sec=2.0)
    t = time.time()
    out = run(Brain(c).judge("ABCD", "FDA approval", {}))
    assert out["model"] == "fast" and time.time() - t < 1.5
    # the fastest model answers nonsense -> the next valid answer is used
    RaceLLM.delays, RaceLLM.replies = {"junk": 0.05, "ok": 0.3}, {"junk": "I think it's good!"}
    c.llm_models = ["junk", "ok"]
    assert run(Brain(c).judge("ABCD", "FDA approval", {}))["model"] == "ok"
    RaceLLM.delays, RaceLLM.replies = {"slow": 3.0}, {}
    c.llm_models, c.llm_timeout_sec = ["slow"], 0.5
    t = time.time()
    assert run(Brain(c).judge("ABCD", "FDA approval", {})) is None and time.time() - t < 1.5
    srv.shutdown()


def test_ai_parse_defensive():
    assert parse('ok {"verdict":"STRONG","confidence":0.8,"reason":"x"}')["verdict"] == "strong"
    assert parse('```json\n{"verdict": "strong", "confidence": 0.8, "reason": "FDA appr')["verdict"] == "strong"
    assert parse('<think>long reasoning</think>```json{"verdict":"trap","confidence":1}```')["verdict"] == "trap"
    assert parse('{"verdict":"weak","confidence":"high"}')["confidence"] == 0.0
    with pytest.raises(ValueError):
        parse('{"verdict":"maybe"}')


def test_live_money_latched(monkeypatch):
    monkeypatch.setenv("SMALLCAP_ALPACA_API_KEY", "k"); monkeypatch.setenv("SMALLCAP_ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("SMALLCAP_PAPER", "false")
    with pytest.raises(SystemExit):
        SCConfig().validate()


def test_premarket_quote_uses_yesterdays_close():
    """Before the open there's no daily bar for today yet: gap must be vs yesterday's close."""
    from bot.smallcap.broker import snapshot_to_quote
    now = datetime.now(timezone.utc)
    yesterday = SimpleNamespace(timestamp=now - timedelta(days=1), close=3.0, volume=500000)
    two_days = SimpleNamespace(timestamp=now - timedelta(days=2), close=2.8, volume=400000)
    snap = SimpleNamespace(daily_bar=yesterday, previous_daily_bar=two_days,
                           latest_trade=SimpleNamespace(price=4.5, timestamp=now - timedelta(seconds=5)),
                           latest_quote=SimpleNamespace(bid_price=4.45, ask_price=4.55))
    q = snapshot_to_quote(snap)
    assert q.prev_close == 3.0 and round(q.gap_pct) == 50
    assert q.day_volume is None and q.rel_volume is None     # unknown, so the volume filter is skipped
    assert snapshot_to_quote(None) is None
    no_quote = SimpleNamespace(daily_bar=yesterday, previous_daily_bar=None, latest_trade=None, latest_quote=None)
    q2 = snapshot_to_quote(no_quote)
    assert q2.price == 3.0 and q2.bid == 0 and q2.spread_pct is None and q2.age_sec is None


def test_null_snapshot_from_alpaca(alpaca, monkeypatch):
    broker, c = alpaca
    orig = FakeAlpaca.do_GET

    def with_null(self):
        if self.path.startswith("/v2/stocks/snapshots") and "NULLX" in self.path:
            return self._send({"NULLX": None})
        return orig(self)
    monkeypatch.setattr(FakeAlpaca, "do_GET", with_null)
    assert broker.quotes(["NULLX"]) == {}
