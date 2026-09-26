"""
Alpaca REST calls the 0DTE runner needs: clock, account, SPY minute bars, option
quotes, and order management. Errors go through executor.client._check so every
rejection carries Alpaca's response body.
"""

import time
from datetime import datetime, timezone

import httpx

from executor.client import AlpacaError, _check
from zerodte.config import Config
from zerodte.strategy import ET, Bar, Quote

_DATA_BASE = "https://data.alpaca.markets"
_TERMINAL = ("filled", "canceled", "expired", "rejected")


class ZeroDteClient:
    def __init__(self, cfg: Config) -> None:
        self._trading = cfg.alpaca_base_url.rstrip("/")
        self._stock_feed = cfg.stock_feed
        self._options_feed = cfg.options_feed
        self._http = httpx.Client(timeout=30, headers={
            "APCA-API-KEY-ID": cfg.alpaca_api_key,
            "APCA-API-SECRET-KEY": cfg.alpaca_secret_key,
        })

    def clock(self) -> dict:
        return _check(self._http.get(f"{self._trading}/v2/clock")).json()

    def account(self) -> dict:
        return _check(self._http.get(f"{self._trading}/v2/account")).json()

    def spy_minute_bars(self, start: datetime, end: datetime) -> list[Bar]:
        r = _check(self._http.get(f"{_DATA_BASE}/v2/stocks/SPY/bars", params={
            "timeframe": "1Min", "start": start.astimezone(timezone.utc).isoformat(),
            "end": end.astimezone(timezone.utc).isoformat(), "feed": self._stock_feed, "limit": 1000,
        }))
        return [Bar(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET),
                    float(b["h"]), float(b["l"]), float(b["c"]))
                for b in r.json().get("bars") or []]

    def option_quotes(self, symbols: list[str]) -> list[Quote]:
        r = _check(self._http.get(f"{_DATA_BASE}/v1beta1/options/snapshots", params={
            "symbols": ",".join(symbols), "feed": self._options_feed,
        }))
        quotes = []
        for sym, snap in (r.json().get("snapshots") or {}).items():
            q = snap.get("latestQuote") or {}
            quotes.append(Quote(sym, float(q.get("bp") or 0), float(q.get("ap") or 0)))
        return quotes

    def submit(self, symbol: str, qty: int, side: str, order_type: str,
               limit_price: float | None = None) -> dict:
        body = {"symbol": symbol, "qty": str(qty), "side": side, "type": order_type,
                "time_in_force": "day"}
        if limit_price is not None:
            body["limit_price"] = f"{limit_price:.2f}"
        return _check(self._http.post(f"{self._trading}/v2/orders", json=body)).json()

    def order(self, order_id: str) -> dict:
        return _check(self._http.get(f"{self._trading}/v2/orders/{order_id}")).json()

    def cancel(self, order_id: str) -> None:
        r = self._http.delete(f"{self._trading}/v2/orders/{order_id}")
        if r.status_code != 422:  # 422 = already filled/canceled — nothing to cancel
            _check(r)

    def wait_terminal(self, order_id: str, timeout_secs: float, poll: float = 1.0) -> dict:
        """Poll until the order is filled/canceled/expired/rejected or the timeout passes."""
        deadline = time.monotonic() + timeout_secs
        while True:
            o = self.order(order_id)
            if o.get("status") in _TERMINAL or time.monotonic() >= deadline:
                return o
            time.sleep(poll)

    def position_qty(self, symbol: str) -> int:
        r = self._http.get(f"{self._trading}/v2/positions/{symbol}")
        if r.status_code == 404:
            return 0
        return abs(int(float(_check(r).json()["qty"])))


__all__ = ["ZeroDteClient", "AlpacaError"]
