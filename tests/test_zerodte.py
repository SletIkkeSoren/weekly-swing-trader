"""
Tests for the 0DTE runner: pure strategy logic, plus the enter/exit flow against
a fake Alpaca client. No network calls.
"""

from datetime import date, datetime, time, timedelta

import pytest

import zerodte.main as runner
from zerodte import strategy
from zerodte.config import Config
from zerodte.models import ZeroDteTrade
from zerodte.strategy import ET, Bar, Quote

DAY = date(2026, 9, 28)


def _bars(or_high: float, or_low: float, last: float) -> list[Bar]:
    bars = []
    t = datetime.combine(DAY, time(9, 30), ET)
    while t.time() < time(10, 0):
        in_range = t.time() < time(9, 45)
        close = (or_high + or_low) / 2 if in_range else last
        bars.append(Bar(t, or_high if in_range else close, or_low if in_range else close, close))
        t += timedelta(minutes=1)
    return bars


# ── strategy ───────────────────────────────────────────────────────────────


def test_breakout_above_range_is_call():
    side, price, _ = strategy.breakout_signal(_bars(501.0, 499.0, 501.5))
    assert (side, price) == ("C", 501.5)


def test_breakout_below_range_is_put():
    assert strategy.breakout_signal(_bars(501.0, 499.0, 498.2))[0] == "P"


def test_inside_range_is_no_trade():
    assert strategy.breakout_signal(_bars(501.0, 499.0, 500.4))[0] is None


def test_stale_bars_are_no_trade():
    bars = [b for b in _bars(501.0, 499.0, 501.5) if b.start.time() < time(9, 50)]
    side, _, reason = strategy.breakout_signal(bars)
    assert side is None and "incomplete" in reason


def test_candidates_are_out_of_the_money():
    calls = strategy.candidate_symbols(DAY, "C", 501.5)
    puts = strategy.candidate_symbols(DAY, "P", 498.2)
    assert calls[0] == "SPY260928C00502000" and len(calls) == strategy.STRIKE_STEPS
    assert puts[0] == "SPY260928P00498000"   # 498 < 498.2 → OTM put
    assert puts[1] == "SPY260928P00497000"


def test_pick_contract_uses_band_centre_and_rejects_wide_spreads():
    quotes = [Quote("a", 0.60, 0.62), Quote("b", 0.93, 0.95), Quote("c", 1.00, 1.03),
              Quote("d", 0.90, 1.00)]   # d: mid 0.95 but 10c wide
    assert strategy.pick_contract(quotes, 0.80, 1.10, 0.05).symbol == "b"


def test_pick_contract_none_in_band():
    assert strategy.pick_contract([Quote("a", 0.30, 0.31)], 0.80, 1.10, 0.05) is None


@pytest.mark.parametrize("equity,bp,ask,expected", [
    (1000, 1000, 0.95, 2),     # 20% of 1000 = 200 → 2 × $95
    (1000, 1000, 1.05, 1),     # 200 // 105 = 1
    (400, 400, 1.05, 1),       # 20% = 80 < 105 → minimum 1 contract
    (80, 80, 1.05, 0),         # can't afford even one
    (7000, 7000, 1.00, 14),    # grows with the account
])
def test_contracts_scale_with_equity(equity, bp, ask, expected):
    assert strategy.contracts_to_buy(equity, bp, ask, 0.20) == expected


# ── enter / exit flow ──────────────────────────────────────────────────────


class FakeClient:
    def __init__(self, *, fill=True, tp_fills=False, daytrades=0, bars=None, now_open=True):
        self.fill, self.tp_fills, self.daytrades = fill, tp_fills, daytrades
        self.bars = bars or _bars(501.0, 499.0, 501.5)
        self.now_open = now_open
        self.orders: dict[str, dict] = {}
        self.submitted: list[dict] = []
        self.positions: dict[str, int] = {}

    def clock(self):
        return {"is_open": self.now_open, "next_close": f"{DAY}T16:00:00-04:00"}

    def account(self):
        return {"equity": "1000", "cash": "1000", "options_buying_power": "1000",
                "daytrade_count": self.daytrades}

    def spy_minute_bars(self, start, end):
        return self.bars

    def option_quotes(self, symbols):
        return [Quote(s, 0.93, 0.95) if i == 3 else Quote(s, 0.10, 0.11) for i, s in enumerate(symbols)]

    def submit(self, symbol, qty, side, order_type, limit_price=None):
        oid = f"o{len(self.submitted)}"
        o = {"id": oid, "symbol": symbol, "side": side, "type": order_type, "qty": qty,
             "status": "new", "filled_qty": "0", "filled_avg_price": None, "limit": limit_price}
        if side == "buy" and self.fill:
            o.update(status="filled", filled_qty=str(qty), filled_avg_price=str(limit_price))
            self.positions[symbol] = qty
        if side == "sell" and order_type == "limit" and self.tp_fills:
            o.update(status="filled", filled_qty=str(qty), filled_avg_price=str(limit_price))
            self.positions[symbol] = 0
        if side == "sell" and order_type == "market":
            o.update(status="filled", filled_qty=str(qty), filled_avg_price="0.40")
            self.positions[symbol] = 0
        self.orders[oid] = o
        self.submitted.append(o)
        return o

    def order(self, oid):
        return self.orders[oid]

    def wait_terminal(self, oid, timeout_secs, poll=1.0):
        return self.orders[oid]

    def cancel(self, oid):
        if self.orders[oid]["status"] != "filled":
            self.orders[oid]["status"] = "canceled"

    def position_qty(self, symbol):
        return self.positions.get(symbol, 0)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "datetime", _FrozenDatetime)
    monkeypatch.setattr(runner.audit, "record_zerodte", lambda trades: None)
    return Config("k", "s", "https://paper", "iex", "indicative", 0.20, 0.80, 1.10, 2.0, 0.05, 3,
                  str(tmp_path), False, "")


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.combine(DAY, time(10, 1), ET)


def _state(cfg) -> ZeroDteTrade:
    return ZeroDteTrade.model_validate_json(runner._state_path(cfg, DAY).read_text())


def test_enter_buys_breakout_contract_and_places_take_profit(cfg):
    c = FakeClient()
    runner.enter(cfg, c)
    buy, tp = c.submitted
    assert buy["symbol"] == "SPY260928C00505000" and buy["qty"] == 2 and buy["limit"] == 0.95
    assert tp["side"] == "sell" and tp["limit"] == 1.90
    s = _state(cfg)
    assert s.status == "open" and s.entry_price == 0.95 and s.tp_order_id == tp["id"]


def test_enter_twice_never_buys_twice(cfg):
    c = FakeClient()
    runner.enter(cfg, c)
    runner.enter(cfg, c)
    assert len([o for o in c.submitted if o["side"] == "buy"]) == 1


def test_enter_skips_at_pdt_limit(cfg):
    c = FakeClient(daytrades=3)
    runner.enter(cfg, c)
    assert c.submitted == [] and _state(cfg).status == "skipped"


def test_enter_skips_when_market_closed(cfg):
    c = FakeClient(now_open=False)
    runner.enter(cfg, c)
    assert c.submitted == [] and "closed" in _state(cfg).reason


def test_unfilled_entry_is_canceled_and_recorded(cfg):
    c = FakeClient(fill=False)
    runner.enter(cfg, c)
    assert c.orders["o0"]["status"] == "canceled"
    assert _state(cfg).status == "unfilled" and len(c.submitted) == 1


def test_exit_sells_at_cutoff_when_take_profit_not_hit(cfg):
    c = FakeClient()
    runner.enter(cfg, c)
    runner.exit_(cfg, c)
    s = _state(cfg)
    assert c.orders[s.tp_order_id]["status"] == "canceled"
    assert s.status == "cutoff" and s.exit_price == 0.40
    assert s.pnl == pytest.approx((0.40 - 0.95) * 100 * 2)


def test_exit_records_take_profit_fill(cfg):
    c = FakeClient(tp_fills=True)
    runner.enter(cfg, c)
    runner.exit_(cfg, c)
    s = _state(cfg)
    assert s.status == "take_profit" and s.exit_price == 1.90
    assert not [o for o in c.submitted if o["type"] == "market"]


def test_exit_flags_position_closed_elsewhere(cfg):
    c = FakeClient()
    runner.enter(cfg, c)
    c.positions.clear()   # e.g. broker auto-closed the expiring contract
    runner.exit_(cfg, c)
    s = _state(cfg)
    assert s.status == "error" and s.pnl is None
