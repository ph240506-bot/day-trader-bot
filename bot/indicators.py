"""Chart maths. Every function takes a DataFrame with open/high/low/close/volume columns."""
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP, reset each calendar day (by bar timestamp)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    day = pd.Series(df.index.date, index=df.index)
    pv = (typical * df["volume"]).groupby(day).cumsum()
    vol = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    return (pv / vol).fillna(typical)


def macd(close: pd.Series, fast=12, slow=26, signal=9):
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def add_all(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema9"] = ema(df["close"], 9)
    df["ema21"] = ema(df["close"], 21)
    df["rsi"] = rsi(df["close"])
    df["atr"] = atr(df)
    df["vwap"] = vwap(df)
    df["macd"], df["macd_sig"], df["macd_hist"] = macd(df["close"])
    df["vol_avg"] = df["volume"].rolling(20, min_periods=5).mean()
    return df
