"""
Alpaca options client — contract lookup and order placement.
Uses the trading REST API for orders and the data API for quotes.
"""

import logging
from datetime import date

import httpx

from executor.config import Config

log = logging.getLogger("executor.client")

_DATA_BASE = "https://data.alpaca.markets"


class AlpacaOptionsClient:
    def __init__(self, cfg: Config) -> None:
        self._trading_base = cfg.alpaca_base_url.rstrip("/")
        self._headers = {
            "APCA-API-KEY-ID": cfg.alpaca_api_key,
            "APCA-API-SECRET-KEY": cfg.alpaca_secret_key,
        }

    def find_contract(
        self, ticker: str, expiry: date, strike: float, option_type: str
    ) -> dict | None:
        """Return the closest matching contract dict from Alpaca, or None."""
        margin = round(strike * 0.05, 2)
        params = {
            "underlying_symbols": ticker,
            "expiration_date_gte": expiry.isoformat(),
            "expiration_date_lte": expiry.isoformat(),
            "strike_price_gte": str(round(strike - margin, 2)),
            "strike_price_lte": str(round(strike + margin, 2)),
            "type": option_type,
            "limit": 10,
        }
        with httpx.Client(headers=self._headers, timeout=30) as client:
            r = client.get(f"{self._trading_base}/v2/options/contracts", params=params)
            r.raise_for_status()
        contracts = r.json().get("option_contracts", [])
        if not contracts:
            return None
        # Pick the strike closest to what the models proposed
        return min(contracts, key=lambda c: abs(float(c["strike_price"]) - strike))

    def get_ask_price(self, ticker: str, occ_symbol: str) -> float | None:
        """Return the latest ask price for a contract, or None if unavailable."""
        with httpx.Client(headers=self._headers, timeout=30) as client:
            r = client.get(
                f"{_DATA_BASE}/v1beta1/options/snapshots/{ticker}",
                params={"symbols": occ_symbol, "feed": "indicative"},
            )
        if r.status_code != 200:
            log.warning("Quote fetch failed for %s: HTTP %s", occ_symbol, r.status_code)
            return None
        snap = r.json().get("snapshots", {}).get(occ_symbol)
        if snap is None:
            return None
        ask = float(snap.get("latestQuote", {}).get("ap", 0))
        return ask if ask > 0 else None

    def place_order(
        self,
        occ_symbol: str,
        qty: int,
        order_type: str,
        limit_price: float | None,
    ) -> dict:
        """Place a day buy order for an options contract. Returns the order dict."""
        body: dict = {
            "symbol": occ_symbol,
            "qty": str(qty),
            "side": "buy",
            "type": order_type,
            "time_in_force": "day",
        }
        if order_type == "limit" and limit_price is not None:
            body["limit_price"] = str(round(limit_price, 2))
        with httpx.Client(headers=self._headers, timeout=30) as client:
            r = client.post(f"{self._trading_base}/v2/orders", json=body)
            r.raise_for_status()
        return r.json()

    def get_open_positions_for_ticker(self, ticker: str) -> list[dict]:
        """Return all open option positions for a given underlying ticker."""
        with httpx.Client(headers=self._headers, timeout=30) as client:
            r = client.get(f"{self._trading_base}/v2/positions")
            r.raise_for_status()
        return [
            p for p in r.json()
            if p.get("asset_class") == "us_option"
            and p.get("underlying_symbol", "").upper() == ticker.upper()
        ]

    def close_position(self, occ_symbol: str) -> dict:
        """Sell-to-close an open position via DELETE /v2/positions/{symbol}."""
        with httpx.Client(headers=self._headers, timeout=30) as client:
            r = client.delete(f"{self._trading_base}/v2/positions/{occ_symbol}")
            r.raise_for_status()
        return r.json()
