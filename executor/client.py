"""
Alpaca options client — contract lookup, market clock, quotes, and order placement.
Uses the trading REST API for orders and the data API for quotes.
"""

import logging
import re
import time
from datetime import date

import httpx

from executor.config import Config

log = logging.getLogger("executor.client")

_DATA_BASE = "https://data.alpaca.markets"


class AlpacaError(RuntimeError):
    """An Alpaca API error that carries the response body.

    httpx's raise_for_status() reports the status code only, which turned every
    rejected order into a bare "422 Unprocessable Entity" with no way to tell a
    closed market from an untradeable contract. Always raise this instead.
    """

    def __init__(self, resp: httpx.Response) -> None:
        body = " ".join((resp.text or "").split())[:500]
        super().__init__(
            f"HTTP {resp.status_code} from {resp.request.url.path}: {body or '<empty body>'}"
        )
        self.status_code = resp.status_code
        self.body = body


def _check(resp: httpx.Response) -> httpx.Response:
    if resp.is_error:
        raise AlpacaError(resp)
    return resp


class AlpacaOptionsClient:
    def __init__(self, cfg: Config) -> None:
        self._trading_base = cfg.alpaca_base_url.rstrip("/")
        self._options_feed = cfg.options_feed
        self._headers = {
            "APCA-API-KEY-ID": cfg.alpaca_api_key,
            "APCA-API-SECRET-KEY": cfg.alpaca_secret_key,
        }

    def get_clock(self) -> dict:
        """Return Alpaca's market clock: {timestamp, is_open, next_open, next_close}."""
        with httpx.Client(headers=self._headers, timeout=15) as client:
            r = client.get(f"{self._trading_base}/v2/clock")
        return _check(r).json()

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
            _check(r)
        contracts = r.json().get("option_contracts", [])
        if not contracts:
            return None
        # Pick the strike closest to what the models proposed
        return min(contracts, key=lambda c: abs(float(c["strike_price"]) - strike))

    def get_ask_price(self, occ_symbol: str) -> float | None:
        """Return the latest ask price for one contract, or None if unavailable.

        Uses the multi-contract snapshot route (/options/snapshots?symbols=...).
        The per-underlying route (/options/snapshots/{underlying}) returns the whole
        option chain and rejects a `symbols` filter with a 400 — that mismatch is
        what silently downgraded every order to a market order.
        """
        with httpx.Client(headers=self._headers, timeout=30) as client:
            r = client.get(
                f"{_DATA_BASE}/v1beta1/options/snapshots",
                params={"symbols": occ_symbol, "feed": self._options_feed},
            )
        if r.is_error:
            log.warning("Quote fetch failed for %s: %s", occ_symbol, AlpacaError(r))
            return None
        snap = r.json().get("snapshots", {}).get(occ_symbol)
        if snap is None:
            log.warning("No snapshot returned for %s (feed=%s)", occ_symbol, self._options_feed)
            return None
        ask = float(snap.get("latestQuote", {}).get("ap", 0))
        if ask <= 0:
            log.warning("Snapshot for %s has no usable ask (ap=%s)", occ_symbol, ask)
            return None
        return ask

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
            _check(r)
        return r.json()

    def wait_for_fill(
        self, order_id: str, timeout_secs: float = 10.0, poll_interval: float = 1.0
    ) -> dict:
        """Poll GET /v2/orders/{id} until it reaches a terminal state or timeout.

        Returns the latest order dict. A still-open order (no filled_avg_price)
        on timeout is not an error — callers should treat the fill as unknown.
        """
        deadline = time.monotonic() + timeout_secs
        order: dict = {}
        with httpx.Client(headers=self._headers, timeout=30) as client:
            while True:
                r = client.get(f"{self._trading_base}/v2/orders/{order_id}")
                _check(r)
                order = r.json()
                if order.get("status") in ("filled", "canceled", "expired", "rejected"):
                    return order
                if time.monotonic() >= deadline:
                    return order
                time.sleep(poll_interval)

    def get_open_positions_for_ticker(self, ticker: str) -> list[dict]:
        """Return all open option positions for a given underlying ticker.

        Alpaca's /v2/positions response does not reliably include underlying_symbol
        for options. Parse the ticker from the OCC symbol instead (e.g. TSLA → TSLA260117C00250000).
        """
        with httpx.Client(headers=self._headers, timeout=30) as client:
            r = client.get(f"{self._trading_base}/v2/positions")
            _check(r)
        results = []
        for p in r.json():
            if p.get("asset_class") != "us_option":
                continue
            m = re.match(r"([A-Z]+)\d", p.get("symbol", ""))
            if m and m.group(1).upper() == ticker.upper():
                results.append(p)
        return results

    def close_position(self, occ_symbol: str) -> dict:
        """Sell-to-close an open position via DELETE /v2/positions/{symbol}."""
        with httpx.Client(headers=self._headers, timeout=30) as client:
            r = client.delete(f"{self._trading_base}/v2/positions/{occ_symbol}")
            _check(r)
        return r.json()
