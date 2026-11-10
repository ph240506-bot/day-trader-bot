"""All settings come from environment variables so nothing secret lives in the repo."""
import os
from dataclasses import dataclass, field


def _env(name, default=None, cast=str):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)


def _list(name, default):
    raw = os.getenv(name)
    if not raw:
        return default
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


@dataclass
class Config:
    # Alpaca
    alpaca_key: str = field(default_factory=lambda: _env("ALPACA_API_KEY", ""))
    alpaca_secret: str = field(default_factory=lambda: _env("ALPACA_SECRET_KEY", ""))
    paper: bool = field(default_factory=lambda: _env("ALPACA_PAPER", True, bool))
    # Safety latch: live money needs paper=false AND this exact phrase.
    live_ack: str = field(default_factory=lambda: _env("LIVE_TRADING_ACK", ""))

    # What to trade
    stocks: list = field(default_factory=lambda: _list(
        "STOCK_SYMBOLS", ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "AMD", "MSFT", "META"]))
    cryptos: list = field(default_factory=lambda: _list("CRYPTO_SYMBOLS", ["BTC/USD", "ETH/USD"]))
    trade_crypto: bool = field(default_factory=lambda: _env("TRADE_CRYPTO", True, bool))

    # Top-mover scanner: adds today's biggest % movers on top of STOCK_SYMBOLS.
    scan_movers: bool = field(default_factory=lambda: _env("SCAN_MOVERS", True, bool))
    top_movers: int = field(default_factory=lambda: _env("TOP_MOVERS", 10, int))
    scan_every_minutes: int = field(default_factory=lambda: _env("SCAN_EVERY_MINUTES", 15, int))
    min_price: float = field(default_factory=lambda: _env("MIN_PRICE", 5.0, float))
    max_price: float = field(default_factory=lambda: _env("MAX_PRICE", 1000.0, float))
    min_move_pct: float = field(default_factory=lambda: _env("MIN_MOVE_PCT", 3.0, float))
    # Skip a mover if fewer than this many shares traded on the IEX feed today.
    # (IEX sees only a slice of total volume, so this number looks small on purpose.)
    min_iex_volume: int = field(default_factory=lambda: _env("MIN_IEX_VOLUME", 20000, int))

    # Loop timing
    bar_minutes: int = field(default_factory=lambda: _env("BAR_MINUTES", 5, int))
    loop_seconds: int = field(default_factory=lambda: _env("LOOP_SECONDS", 60, int))
    max_runtime_minutes: int = field(default_factory=lambda: _env("MAX_RUNTIME_MINUTES", 350, int))
    wait_for_open_minutes: int = field(default_factory=lambda: _env("WAIT_FOR_OPEN_MINUTES", 45, int))
    flatten_minutes_before_close: int = field(default_factory=lambda: _env("FLATTEN_BEFORE_CLOSE_MIN", 5, int))
    # Entry windows (stocks only): skip the chaotic open and trades with no time left to work.
    open_delay_min: int = field(default_factory=lambda: _env("OPEN_DELAY_MIN", 10, int))
    no_entry_before_close_min: int = field(default_factory=lambda: _env("NO_ENTRY_BEFORE_CLOSE_MIN", 30, int))
    # Night 3: TSLA long stopped out, then shorted 8 min later and stopped again. Let the chop settle.
    reentry_cooldown_min: int = field(default_factory=lambda: _env("REENTRY_COOLDOWN_MIN", 30, int))
    # Trade with the market: longs only when SPY is above its VWAP, shorts only when below.
    market_filter: bool = field(default_factory=lambda: _env("MARKET_FILTER", True, bool))
    # Skip if price moved more than this many ATRs between the signal candle and the order.
    max_chase_atr: float = field(default_factory=lambda: _env("MAX_CHASE_ATR", 1.0, float))
    # Alpaca charges ~0.25% per crypto trade each way (0.5% round trip). Only take crypto trades whose
    # target is at least this far away, so a win actually beats the fees.
    min_crypto_target_pct: float = field(default_factory=lambda: _env("MIN_CRYPTO_TARGET_PCT", 1.5, float))

    # Strategy: "orb" = opening range breakout on today's stocks in play (published edge), "ema" = original rules.
    strategy: str = field(default_factory=lambda: _env("STRATEGY", "orb").lower())
    orb_minutes: int = field(default_factory=lambda: _env("ORB_MINUTES", 5, int))
    orb_top_n: int = field(default_factory=lambda: _env("ORB_TOP_N", 20, int))
    orb_min_rel_volume: float = field(default_factory=lambda: _env("ORB_MIN_REL_VOLUME", 1.0, float))
    orb_min_atr_dollars: float = field(default_factory=lambda: _env("ORB_MIN_ATR_DOLLARS", 0.5, float))
    orb_stop_atr_frac: float = field(default_factory=lambda: _env("ORB_STOP_ATR_FRAC", 0.10, float))
    orb_stop_mode: str = field(default_factory=lambda: _env("ORB_STOP_MODE", "atr").lower())   # atr | range
    orb_min_stop_pct: float = field(default_factory=lambda: _env("ORB_MIN_STOP_PCT", 0.3, float))
    orb_max_stop_pct: float = field(default_factory=lambda: _env("ORB_MAX_STOP_PCT", 3.0, float))
    orb_target_r: float = field(default_factory=lambda: _env("ORB_TARGET_R", 0.0, float))        # 0 = hold to close
    orb_entry_window_min: int = field(default_factory=lambda: _env("ORB_ENTRY_WINDOW_MIN", 60, int))
    orb_candidates: int = field(default_factory=lambda: _env("ORB_CANDIDATES", 120, int))
    orb_llm: bool = field(default_factory=lambda: _env("ORB_LLM", True, bool))
    # ETFs and leveraged funds are skipped by default: the research is on stocks, and SOXL + SOXX + NVDX
    # in one morning is one semiconductor bet placed three times.
    orb_allow_funds: bool = field(default_factory=lambda: _env("ORB_ALLOW_FUNDS", False, bool))
    orb_poll_seconds: int = field(default_factory=lambda: _env("ORB_POLL_SECONDS", 15, int))

    # Risk
    risk_per_trade_pct: float = field(default_factory=lambda: _env("RISK_PER_TRADE_PCT", 0.5, float))
    max_position_pct: float = field(default_factory=lambda: _env("MAX_POSITION_PCT", 10.0, float))
    max_open_positions: int = field(default_factory=lambda: _env("MAX_OPEN_POSITIONS", 4, int))
    daily_loss_limit_pct: float = field(default_factory=lambda: _env("DAILY_LOSS_LIMIT_PCT", 2.0, float))
    stop_atr_mult: float = field(default_factory=lambda: _env("STOP_ATR_MULT", 1.5, float))
    reward_risk: float = field(default_factory=lambda: _env("REWARD_RISK", 2.0, float))
    min_score: int = field(default_factory=lambda: _env("MIN_SIGNAL_SCORE", 3, int))

    # LLM via freellmapi (OpenAI-compatible)
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "http://localhost:3001/v1"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "auto"))
    # Tried in order until one gives a readable answer (auto:fast avoids slow "thinking" models).
    llm_models: list = field(default_factory=lambda: [m.strip() for m in _env("LLM_MODELS", "auto:fast,auto").split(",") if m.strip()])
    # If the LLM can't be reached: True = skip the trade (safer), False = trade on indicators alone.
    llm_required: bool = field(default_factory=lambda: _env("LLM_REQUIRED", True, bool))

    # Dry run: analyse and log, never send orders.
    # Phone alerts (ntfy.sh). Leave NTFY_TOPIC empty to turn off.
    ntfy_topic: str = field(default_factory=lambda: _env("NTFY_TOPIC", ""))
    ntfy_server: str = field(default_factory=lambda: _env("NTFY_SERVER", "https://ntfy.sh"))
    notify_on: list = field(default_factory=lambda: [s.lower() for s in _list(
        "NOTIFY_ON", ["TRADE", "VETO", "NEAR_MISS", "ALERT"])])

    status_every_minutes: int = field(default_factory=lambda: _env("STATUS_EVERY_MINUTES", 5, int))

    dry_run: bool = field(default_factory=lambda: _env("DRY_RUN", False, bool))
    journal_path: str = field(default_factory=lambda: _env("JOURNAL_PATH", "journal/trades.csv"))

    def validate(self):
        if not self.alpaca_key or not self.alpaca_secret:
            raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY.")
        if not self.paper and self.live_ack != "I accept I can lose real money":
            raise SystemExit(
                "Refusing to trade a LIVE account. Keep ALPACA_PAPER=true, or set "
                "LIVE_TRADING_ACK='I accept I can lose real money' if you really mean it.")
