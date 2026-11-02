"""Everything that talks to Alpaca: bars, news, account, orders. Free tier = IEX real-time feed."""
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import CryptoBarsRequest, NewsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest

log = logging.getLogger("market")


def is_crypto(symbol: str) -> bool:
    return "/" in symbol


class Market:
    def __init__(self, cfg):
        self.cfg = cfg
        self.trading = TradingClient(cfg.alpaca_key, cfg.alpaca_secret, paper=cfg.paper)
        self.stocks = StockHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)
        self.crypto = CryptoHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)
        self.news = NewsClient(cfg.alpaca_key, cfg.alpaca_secret)

    # ---------- data ----------
    def bars(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        tf = TimeFrame(self.cfg.bar_minutes, TimeFrameUnit.Minute)
        start = datetime.now(timezone.utc) - timedelta(days=4)  # covers weekends for warm-up
        out = {}
        stock_syms = [s for s in symbols if not is_crypto(s)]
        crypto_syms = [s for s in symbols if is_crypto(s)]
        try:
            if stock_syms:
                df = self.stocks.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=stock_syms, timeframe=tf, start=start, feed=DataFeed.IEX)).df
                out.update(_split(df))
            if crypto_syms:
                df = self.crypto.get_crypto_bars(CryptoBarsRequest(
                    symbol_or_symbols=crypto_syms, timeframe=tf, start=start)).df
                out.update(_split(df))
        except Exception as e:
            log.warning("bar fetch failed: %s", e)
        return out

    def latest_price(self, symbol: str) -> float | None:
        """Price right now (the signal used the last finished candle, which can be minutes old)."""
        try:
            if is_crypto(symbol):
                from alpaca.data.requests import CryptoLatestTradeRequest
                t = self.crypto.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=symbol))
            else:
                from alpaca.data.requests import StockLatestTradeRequest
                t = self.stocks.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))
            return float(t[symbol].price)
        except Exception as e:
            log.debug("latest price %s failed: %s", symbol, e)
            return None

    def headlines(self, symbol: str) -> list[str]:
        try:
            q = symbol.split("/")[0]
            res = self.news.get_news(NewsRequest(
                symbols=q, start=datetime.now(timezone.utc) - timedelta(hours=24), limit=8))
            items = res.data.get("news", []) if hasattr(res, "data") else []
            return [f"{n.created_at:%H:%M} {n.headline}" for n in items]
        except Exception as e:
            log.warning("news fetch failed for %s: %s", symbol, e)
            return []

    # ---------- account ----------
    def clock(self):
        return self.trading.get_clock()

    def account(self):
        return self.trading.get_account()

    def positions(self) -> dict:
        return {p.symbol: p for p in self.trading.get_all_positions()}

    def has_open_order(self, symbol: str) -> bool:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        orders = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
        return len(orders) > 0

    # ---------- orders ----------
    def enter(self, symbol: str, side: str, plan):
        side_enum = OrderSide.BUY if side == "buy" else OrderSide.SELL
        if is_crypto(symbol):
            # Crypto has no bracket orders on Alpaca: enter at market, the bot watches stop/target.
            req = MarketOrderRequest(symbol=symbol, qty=plan.qty, side=side_enum, time_in_force=TimeInForce.GTC)
        else:
            req = MarketOrderRequest(
                symbol=symbol, qty=plan.qty, side=side_enum, time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=plan.target),
                stop_loss=StopLossRequest(stop_price=plan.stop))
        return self.trading.submit_order(req)

    def close(self, symbol: str):
        return self.trading.close_position(symbol.replace("/", ""))

    def flatten_all(self):
        log.info("Flattening all positions and cancelling orders")
        return self.trading.close_all_positions(cancel_orders=True)


def _split(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if df is None or df.empty:
        return {}
    out = {}
    for sym in df.index.get_level_values(0).unique():
        d = df.xs(sym, level=0)[["open", "high", "low", "close", "volume"]].astype(float)
        d.index = pd.to_datetime(d.index).tz_convert("America/New_York")
        out[sym] = d
    return out
