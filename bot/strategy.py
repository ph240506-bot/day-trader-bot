"""Turns indicators into a scored long/short signal.

Plain-English version: we look for the price trend turning (fast average crossing the slow
one), confirmed by price being on the right side of the day's average price (VWAP),
momentum (MACD) agreeing, RSI not already stretched, and above-normal volume.
Each confirmation adds a point. Only setups with enough points are considered.
"""
from dataclasses import dataclass, asdict
import pandas as pd

from .indicators import add_all


@dataclass
class Signal:
    symbol: str
    side: str            # "buy" or "sell" (sell = short; crypto never shorts)
    score: int
    price: float
    atr: float
    reasons: list

    def to_dict(self):
        return asdict(self)


def evaluate(symbol: str, bars: pd.DataFrame, allow_short: bool = True) -> Signal | None:
    if bars is None or len(bars) < 30:
        return None
    df = add_all(bars)
    return signal_from_rows(symbol, df.iloc[-2], df.iloc[-1], allow_short)


def signal_from_rows(symbol: str, prev, last, allow_short: bool = True) -> Signal | None:
    """The scoring rules on two consecutive indicator rows (shared by the live bot and the backtest)."""
    crossed_up = prev["ema9"] <= prev["ema21"] and last["ema9"] > last["ema21"]
    crossed_dn = prev["ema9"] >= prev["ema21"] and last["ema9"] < last["ema21"]
    vol_spike = last["vol_avg"] > 0 and last["volume"] > 1.5 * last["vol_avg"]

    if crossed_up:
        side, reasons, score = "buy", ["EMA9 crossed above EMA21"], 1
        if last["close"] > last["vwap"]:
            score += 1; reasons.append("price above VWAP")
        if last["macd_hist"] > 0:
            score += 1; reasons.append("MACD momentum positive")
        if 45 <= last["rsi"] <= 70:
            score += 1; reasons.append(f"RSI {last['rsi']:.0f} healthy, not overbought")
        if vol_spike:
            score += 1; reasons.append("volume spike")
    elif crossed_dn and allow_short:
        side, reasons, score = "sell", ["EMA9 crossed below EMA21"], 1
        if last["close"] < last["vwap"]:
            score += 1; reasons.append("price below VWAP")
        if last["macd_hist"] < 0:
            score += 1; reasons.append("MACD momentum negative")
        if 30 <= last["rsi"] <= 55:
            score += 1; reasons.append(f"RSI {last['rsi']:.0f} weak, not oversold")
        if vol_spike:
            score += 1; reasons.append("volume spike")
    else:
        return None

    return Signal(symbol, side, score, float(last["close"]), float(last["atr"]), reasons)


def chart_summary(bars: pd.DataFrame) -> dict:
    """Compact numbers the LLM sees instead of a raw chart."""
    df = add_all(bars).tail(12)
    last = df.iloc[-1]
    return {
        "last_close": round(float(last["close"]), 4),
        "vwap": round(float(last["vwap"]), 4),
        "rsi": round(float(last["rsi"]), 1),
        "atr": round(float(last["atr"]), 4),
        "ema9_minus_ema21": round(float(last["ema9"] - last["ema21"]), 4),
        "macd_hist": round(float(last["macd_hist"]), 4),
        "pct_change_last_12_bars": round(float(df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100, 2),
        "recent_closes": [round(float(x), 4) for x in df["close"]],
    }
