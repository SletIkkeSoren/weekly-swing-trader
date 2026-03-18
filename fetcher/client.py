"""
Alpaca REST client wrapper for OHLCV bar fetching and position queries.
"""

import re
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import TimeFrame

from fetcher.config import Config
from fetcher.models import OHLCVBar, OpenPosition


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

    def fetch_open_positions(self) -> list[OpenPosition]:
        """Return all open US option positions on the account."""
        raw = self._api.list_positions()
        positions: list[OpenPosition] = []
        today = datetime.now(timezone.utc).date()

        for pos in raw:
            if getattr(pos, "asset_class", None) != "us_option":
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
