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
    # If the LLM can't be reached: True = skip the trade (safer), False = trade on indicators alone.
    llm_required: bool = field(default_factory=lambda: _env("LLM_REQUIRED", True, bool))

    # Dry run: analyse and log, never send orders.
    dry_run: bool = field(default_factory=lambda: _env("DRY_RUN", False, bool))
    journal_path: str = field(default_factory=lambda: _env("JOURNAL_PATH", "journal/trades.csv"))

    def validate(self):
        if not self.alpaca_key or not self.alpaca_secret:
            raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY.")
        if not self.paper and self.live_ack != "I accept I can lose real money":
            raise SystemExit(
                "Refusing to trade a LIVE account. Keep ALPACA_PAPER=true, or set "
                "LIVE_TRADING_ACK='I accept I can lose real money' if you really mean it.")
