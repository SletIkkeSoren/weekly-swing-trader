"""
Alpaca REST client wrapper for OHLCV bar fetching and position queries.
"""

import re
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass

from fetcher.config import Config
from fetcher.models import OHLCVBar, OpenPosition


class AlpacaClient:
    def __init__(self, cfg: Config) -> None:
        paper = "paper" in cfg.alpaca_base_url.lower()
        self._data_client = StockHistoricalDataClient(
            api_key=cfg.alpaca_api_key,
            secret_key=cfg.alpaca_secret_key,
        )
        self._trading_client = TradingClient(
            api_key=cfg.alpaca_api_key,
            secret_key=cfg.alpaca_secret_key,
            paper=paper,
        )
        self._lookback_days = cfg.lookback_days
        self._feed = cfg.data_feed

    def fetch_bars(self, ticker: str) -> list[OHLCVBar]:
        """Return daily OHLCV bars for the past lookback_days calendar days."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=int(self._lookback_days * 1.4))

        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment="split",
            feed=self._feed,
        )
        bars = self._data_client.get_stock_bars(request)
        df: pd.DataFrame = bars.df

        if df.empty:
            raise ValueError(f"No bar data returned for {ticker}")

        # alpaca-py returns a MultiIndex (symbol, timestamp) DataFrame
        if isinstance(df.index, pd.MultiIndex):
            df = df.loc[ticker]

        # Strip timezone so downstream date handling stays consistent
        idx = pd.to_datetime(df.index)
        df.index = idx.tz_convert(None) if idx.tz is not None else idx

        df = df.tail(self._lookback_days).copy()

        result: list[OHLCVBar] = []
        for ts, row in df.iterrows():
            result.append(
                OHLCVBar(
                    date=ts.date(),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    vwap=float(row["vwap"]) if "vwap" in row and pd.notna(row["vwap"]) else None,
                )
            )
        return result

    def is_trading_day_today(self) -> bool:
        """Return True if today is a US stock market trading day (not a holiday)."""
        from alpaca.trading.requests import GetCalendarRequest
        today = date.today()
        calendar = self._trading_client.get_calendar(
            GetCalendarRequest(start=str(today), end=str(today))
        )
        return len(calendar) > 0

    def fetch_open_positions(self) -> list[OpenPosition]:
        """Return all open US option positions on the account."""
        raw = self._trading_client.get_all_positions()
        positions: list[OpenPosition] = []
        today = datetime.now(timezone.utc).date()

        for pos in raw:
            if pos.asset_class != AssetClass.US_OPTION:
                continue

            occ_symbol: str = pos.symbol
            m = re.match(r"([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])", occ_symbol)
            if not m:
                continue
            ticker, yy, mm, dd, opt_char = m.groups()
            expiry = date(2000 + int(yy), int(mm), int(dd))
            option_type = "call" if opt_char == "C" else "put"
            dte = (expiry - today).days

            avg_price = float(pos.avg_entry_price)
            curr_price = float(pos.current_price)
            pnl_pct = (curr_price - avg_price) / avg_price if avg_price else 0.0

            positions.append(
                OpenPosition(
                    ticker=ticker,
                    occ_symbol=occ_symbol,
                    option_type=option_type,
                    qty=abs(int(float(pos.qty))),
                    avg_entry_price=avg_price,
                    current_price=curr_price,
                    pnl_pct=pnl_pct,
                    days_to_expiry=max(0, dte),
                )
            )
        return positions
