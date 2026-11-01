"""Position sizing and the guardrails that stop a bad day becoming a disaster."""
import math
from dataclasses import dataclass


@dataclass
class OrderPlan:
    qty: float
    stop: float
    target: float


def plan_order(side: str, price: float, atr: float, equity: float, cfg, fractional: bool) -> OrderPlan | None:
    if atr <= 0 or price <= 0 or equity <= 0:
        return None
    stop_dist = cfg.stop_atr_mult * atr
    risk_dollars = equity * cfg.risk_per_trade_pct / 100
    qty = risk_dollars / stop_dist
    # Cap the position's total size as a share of the account.
    qty = min(qty, equity * cfg.max_position_pct / 100 / price)
    qty = round(qty, 6) if fractional else math.floor(qty)
    if qty <= 0:
        return None
    if side == "buy":
        stop, target = price - stop_dist, price + cfg.reward_risk * stop_dist
    else:
        stop, target = price + stop_dist, price - cfg.reward_risk * stop_dist
    return OrderPlan(qty, round(stop, 2), round(target, 2))


def daily_loss_hit(equity: float, last_equity: float, cfg) -> bool:
    """last_equity = account value at the previous close (Alpaca reports it)."""
    if last_equity <= 0:
        return False
    return (equity - last_equity) / last_equity * 100 <= -cfg.daily_loss_limit_pct
