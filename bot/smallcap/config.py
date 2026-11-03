"""Small-cap mode settings. Everything is an env var prefixed SC_ (plus its own Alpaca keys)."""
import os
from dataclasses import dataclass, field


def _e(name, default, cast=str):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if cast is list:
        return [s.strip() for s in raw.split(",") if s.strip()]
    return cast(raw)


@dataclass
class SCConfig:
    # Use a SEPARATE Alpaca paper account so results don't mix with the main bot.
    alpaca_key: str = field(default_factory=lambda: _e("SMALLCAP_ALPACA_API_KEY", ""))
    alpaca_secret: str = field(default_factory=lambda: _e("SMALLCAP_ALPACA_SECRET_KEY", ""))
    paper: bool = field(default_factory=lambda: _e("SMALLCAP_PAPER", True, bool))
    live_ack: str = field(default_factory=lambda: _e("SMALLCAP_LIVE_ACK", ""))
    finnhub_key: str = field(default_factory=lambda: _e("FINNHUB_API_KEY", ""))
    # SEC requires a real contact in the User-Agent; the filings feed stays off without one.
    edgar_user_agent: str = field(default_factory=lambda: _e("SEC_USER_AGENT", "") or (
        f"day-trader-bot {_e('SEC_CONTACT_EMAIL', '')}" if _e("SEC_CONTACT_EMAIL", "") else ""))

    # What counts as a small-cap mover
    min_price: float = field(default_factory=lambda: _e("SC_MIN_PRICE", 1.0, float))
    max_price: float = field(default_factory=lambda: _e("SC_MAX_PRICE", 20.0, float))
    min_gap_pct: float = field(default_factory=lambda: _e("SC_MIN_GAP_PCT", 8.0, float))
    # Today's volume vs yesterday's full-day volume (IEX feed undercounts, ratio still useful)
    min_rel_volume: float = field(default_factory=lambda: _e("SC_MIN_REL_VOLUME", 1.0, float))
    max_spread_pct: float = field(default_factory=lambda: _e("SC_MAX_SPREAD_PCT", 2.0, float))
    max_news_age_sec: int = field(default_factory=lambda: _e("SC_MAX_NEWS_AGE_SEC", 180, int))

    # Share count ("float" stand-in): fewer shares = moves faster on buying pressure
    max_shares_m: float = field(default_factory=lambda: _e("SC_MAX_SHARES_M", 50.0, float))
    require_share_data: bool = field(default_factory=lambda: _e("SC_REQUIRE_SHARE_DATA", False, bool))

    # Entry style. pullback = wait for the first dip after the spike, buy when it turns back up.
    entry_mode: str = field(default_factory=lambda: _e("SC_ENTRY_MODE", "pullback"))
    watch_min: int = field(default_factory=lambda: _e("SC_WATCH_MIN", 20, int))
    max_watch: int = field(default_factory=lambda: _e("SC_MAX_WATCH", 4, int))
    pullback_min_pct: float = field(default_factory=lambda: _e("SC_PULLBACK_MIN_PCT", 3.0, float))
    pullback_max_pct: float = field(default_factory=lambda: _e("SC_PULLBACK_MAX_PCT", 15.0, float))
    bounce_pct: float = field(default_factory=lambda: _e("SC_BOUNCE_PCT", 1.5, float))
    require_above_vwap: bool = field(default_factory=lambda: _e("SC_REQUIRE_ABOVE_VWAP", True, bool))
    stop_buffer_pct: float = field(default_factory=lambda: _e("SC_STOP_BUFFER_PCT", 1.0, float))
    max_stop_pct: float = field(default_factory=lambda: _e("SC_MAX_STOP_PCT", 10.0, float))
    reward_risk: float = field(default_factory=lambda: _e("SC_REWARD_RISK", 2.0, float))
    risk_per_trade_pct: float = field(default_factory=lambda: _e("SC_RISK_PER_TRADE_PCT", 1.0, float))
    max_quote_age_sec: float = field(default_factory=lambda: _e("SC_MAX_QUOTE_AGE_SEC", 60.0, float))

    # Sizing and exits
    position_usd: float = field(default_factory=lambda: _e("SC_POSITION_USD", 200.0, float))
    max_position_pct: float = field(default_factory=lambda: _e("SC_MAX_POSITION_PCT", 25.0, float))
    max_positions: int = field(default_factory=lambda: _e("SC_MAX_POSITIONS", 3, int))
    max_trades_per_day: int = field(default_factory=lambda: _e("SC_MAX_TRADES_PER_DAY", 6, int))
    entry_slippage_pct: float = field(default_factory=lambda: _e("SC_ENTRY_SLIPPAGE_PCT", 1.0, float))
    exit_slippage_pct: float = field(default_factory=lambda: _e("SC_EXIT_SLIPPAGE_PCT", 1.5, float))
    fill_timeout_sec: int = field(default_factory=lambda: _e("SC_FILL_TIMEOUT_SEC", 8, int))
    stop_pct: float = field(default_factory=lambda: _e("SC_STOP_PCT", 5.0, float))
    target_pct: float = field(default_factory=lambda: _e("SC_TARGET_PCT", 15.0, float))
    trail_arm_pct: float = field(default_factory=lambda: _e("SC_TRAIL_ARM_PCT", 6.0, float))
    trail_pct: float = field(default_factory=lambda: _e("SC_TRAIL_PCT", 4.0, float))
    max_hold_min: int = field(default_factory=lambda: _e("SC_MAX_HOLD_MIN", 30, int))
    daily_loss_pct: float = field(default_factory=lambda: _e("SC_DAILY_LOSS_PCT", 5.0, float))

    # Sessions (New York time). Pre-market is where most small-cap news hits.
    trade_premarket: bool = field(default_factory=lambda: _e("SC_TRADE_PREMARKET", True, bool))
    exit_poll_sec: float = field(default_factory=lambda: _e("SC_EXIT_POLL_SEC", 2.0, float))
    edgar_poll_sec: float = field(default_factory=lambda: _e("SC_EDGAR_POLL_SEC", 15.0, float))
    # Gainers scanner: catch small caps already jumping, then look up why (like a day trader's scanner)
    scan_movers: bool = field(default_factory=lambda: _e("SC_SCAN_MOVERS", True, bool))
    scan_sec: float = field(default_factory=lambda: _e("SC_SCAN_SEC", 60.0, float))
    scan_top: int = field(default_factory=lambda: _e("SC_SCAN_TOP", 50, int))

    # AI: race several free models, first sensible answer wins
    llm_base_url: str = field(default_factory=lambda: _e("LLM_BASE_URL", "http://localhost:3001/v1"))
    llm_api_key: str = field(default_factory=lambda: _e("LLM_API_KEY", ""))
    llm_models: list = field(default_factory=lambda: _e("SC_LLM_MODELS", ["auto:fast", "auto"], list))
    llm_timeout_sec: float = field(default_factory=lambda: _e("SC_LLM_TIMEOUT_SEC", 8.0, float))
    # Top-gainer scanner hits are already moving and not a race against other news readers: wait longer.
    scanner_llm_timeout_sec: float = field(default_factory=lambda: _e("SC_SCANNER_LLM_TIMEOUT_SEC", 20.0, float))
    llm_required: bool = field(default_factory=lambda: _e("SC_LLM_REQUIRED", True, bool))

    dry_run: bool = field(default_factory=lambda: _e("SC_DRY_RUN", False, bool))
    journal_path: str = field(default_factory=lambda: _e("SC_JOURNAL_PATH", "journal/smallcap.csv"))

    def validate(self):
        if not self.alpaca_key or not self.alpaca_secret:
            raise SystemExit("Small-cap mode needs SMALLCAP_ALPACA_API_KEY and SMALLCAP_ALPACA_SECRET_KEY "
                             "(use a second Alpaca paper account).")
        if not self.paper and self.live_ack != "I accept small caps can lose everything":
            raise SystemExit("Small-cap mode refuses live money unless SMALLCAP_PAPER=false and "
                             "SMALLCAP_LIVE_ACK='I accept small caps can lose everything'.")
