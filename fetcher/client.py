"""
Alpaca REST client wrapper for OHLCV bar fetching.
"""

from datetime import date, timedelta

import pandas as pd
import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import TimeFrame

from fetcher.config import Config
from fetcher.models import OHLCVBar


class AlpacaClient:
    def __init__(self, cfg: Config) -> None:
        self._api = tradeapi.REST(
            key_id=cfg.alpaca_api_key,
            secret_key=cfg.alpaca_secret_key,
            base_url=cfg.alpaca_base_url,
            api_version="v2",
        )
        self._lookback_days = cfg.lookback_days
        self._feed = cfg.data_feed

    def fetch_bars(self, ticker: str) -> list[OHLCVBar]:
        """Return daily OHLCV bars for the past lookback_days calendar days."""
        end = date.today()
        # Add a 40% buffer to account for weekends/holidays
        start = end - timedelta(days=int(self._lookback_days * 1.4))

        df: pd.DataFrame = self._api.get_bars(
            ticker,
            TimeFrame.Day,
            start.isoformat(),
            end.isoformat(),
            adjustment="split",  # split-adjusted; corporate actions handled by Alpaca
            feed=self._feed,
        ).df

        if df.empty:
            raise ValueError(f"No bar data returned for {ticker}")

        # Keep only the most recent lookback_days trading days
        df = df.tail(self._lookback_days).copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)

        bars: list[OHLCVBar] = []
        for ts, row in df.iterrows():
            bars.append(
                OHLCVBar(
                    date=ts.date(),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    vwap=float(row["vwap"]) if "vwap" in row else None,
                )
            )
        return bars
