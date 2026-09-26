"""
0DTE runner — buys one same-day SPY option on an opening-range breakout and is
flat again by 15:30 ET. Two CronJobs in America/New_York time (k8s/zerodte-cronjob.yaml):

    python -m zerodte.main enter    # 10:01 ET
    python -m zerodte.main exit     # 15:30 ET

The rule is the one that survived backtest/zerodte.py out-of-sample; it is a
near break-even, high-variance bet, sized as a fraction of current equity.

State for the day lives in $STATE_DIR/{date}.json between the two jobs. Guards
before any order, each skipping the day loudly rather than guessing:
  - market open, a full session (half days skipped), and now inside 10:00–10:10 ET
  - no state file for today yet (a re-run must never buy twice)
  - PDT: fewer than MAX_DAY_TRADES day trades in the rolling window under $25k
"""

import json
import logging
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

from audit import storage as audit
from zerodte import strategy
from zerodte.client import AlpacaError, ZeroDteClient
from zerodte.config import Config
from zerodte.models import ZeroDteTrade
from zerodte.strategy import ET

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full request URLs at INFO — that includes the Discord webhook token
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("zerodte")

ENTRY_WINDOW = (time(10, 0), time(10, 10))
FILL_TIMEOUT_SECS = 60
PDT_EQUITY = 25_000


def _state_path(cfg: Config, day) -> Path:
    return Path(cfg.state_dir) / f"{day}.json"


def _save(cfg: Config, trade: ZeroDteTrade) -> None:
    path = _state_path(cfg, trade.day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(trade.model_dump_json(indent=2))


def _notify(cfg: Config, text: str) -> None:
    log.info(text)
    if not cfg.discord_webhook_url:
        return
    try:
        httpx.post(cfg.discord_webhook_url, json={"content": text}, timeout=10).raise_for_status()
    except Exception:
        log.exception("Discord notification failed")


def _finish(cfg: Config, trade: ZeroDteTrade, text: str) -> None:
    _save(cfg, trade)
    audit.record_zerodte([trade])
    _notify(cfg, text)


# ── enter ──────────────────────────────────────────────────────────────────


def enter(cfg: Config, client: ZeroDteClient) -> None:
    now = datetime.now(ET)
    today = now.date()
    if _state_path(cfg, today).exists():
        log.warning("State for %s already exists — refusing to enter twice", today)
        return

    def skip(reason: str, status: str = "skipped", **kw) -> None:
        _finish(cfg, ZeroDteTrade(day=today, status=status, reason=reason, **kw),
                f"⏭️ **0DTE {today}** — no trade: {reason}")

    clock = client.clock()
    if not clock["is_open"]:
        return skip("market closed")
    close_at = datetime.fromisoformat(clock["next_close"]).astimezone(ET)
    if close_at.time() < time(16, 0):
        return skip(f"half day (closes {close_at:%H:%M} ET)")
    if not ENTRY_WINDOW[0] <= now.time() <= ENTRY_WINDOW[1]:
        return skip(f"outside the 10:00–10:10 ET entry window (now {now:%H:%M})")

    acct = client.account()
    equity = float(acct["equity"])
    if equity < PDT_EQUITY and int(acct.get("daytrade_count", 0)) >= cfg.max_day_trades:
        return skip(f"PDT limit — {acct['daytrade_count']} day trades in the last 5 days", equity_before=equity)

    bars = client.spy_minute_bars(datetime.combine(today, time(9, 30), ET),
                                  datetime.combine(today, strategy.DECIDE, ET))
    side, price, why = strategy.breakout_signal(bars)
    if side is None:
        return skip(why, equity_before=equity)

    quotes = client.option_quotes(strategy.candidate_symbols(today, side, price))
    pick = strategy.pick_contract(quotes, cfg.premium_min, cfg.premium_max, cfg.max_quote_spread)
    if pick is None:
        return skip(f"{why}, but no contract with mid in ${cfg.premium_min:.2f}–{cfg.premium_max:.2f} "
                    f"and spread ≤ ${cfg.max_quote_spread:.2f}", side=side, equity_before=equity)

    qty = strategy.contracts_to_buy(equity, float(acct.get("options_buying_power") or acct["cash"]),
                                    pick.ask, cfg.risk_fraction)
    if qty < 1:
        return skip(f"can't afford 1 × {pick.symbol} at ${pick.ask:.2f}", side=side, symbol=pick.symbol,
                    equity_before=equity)

    trade = ZeroDteTrade(day=today, status="open", reason=why, side=side, symbol=pick.symbol,
                         qty=qty, equity_before=equity)
    if cfg.dry_run:
        trade.status, trade.entry_price = "dry_run", pick.ask
        return _finish(cfg, trade, f"🧪 **0DTE {today}** DRY RUN — would buy {qty} × {pick.symbol} "
                                   f"@ ${pick.ask:.2f} ({why})")

    # Write state before ordering: a crash after this point can't lead to a second buy
    _save(cfg, trade)
    order = client.submit(pick.symbol, qty, "buy", "limit", pick.ask)
    order = client.wait_terminal(order["id"], FILL_TIMEOUT_SECS)
    filled = int(float(order.get("filled_qty") or 0))
    if order.get("status") != "filled":
        client.cancel(order["id"])
        order = client.wait_terminal(order["id"], 10)
        filled = int(float(order.get("filled_qty") or 0))
    if filled == 0:
        trade.status, trade.qty = "unfilled", 0
        return _finish(cfg, trade, f"⏭️ **0DTE {today}** — limit buy {pick.symbol} @ ${pick.ask:.2f} "
                                   f"not filled in {FILL_TIMEOUT_SECS}s, no trade")

    trade.qty = filled
    trade.entry_price = float(order["filled_avg_price"])
    trade.tp_price = strategy.take_profit_price(trade.entry_price, cfg.take_profit)
    tp = client.submit(pick.symbol, filled, "sell", "limit", trade.tp_price)
    trade.tp_order_id = tp["id"]
    kind = "CALL" if side == "C" else "PUT"
    _finish(cfg, trade, f"🎰 **0DTE {today}** — bought {filled} × {pick.symbol} ({kind}) @ "
                        f"${trade.entry_price:.2f} = ${trade.entry_price * 100 * filled:,.0f} "
                        f"({trade.entry_price * 100 * filled / equity:.0%} of ${equity:,.0f}). "
                        f"Take profit @ ${trade.tp_price:.2f}. {why}")


# ── exit ───────────────────────────────────────────────────────────────────


def exit_(cfg: Config, client: ZeroDteClient) -> None:
    today = datetime.now(ET).date()
    path = _state_path(cfg, today)
    if not path.exists():
        log.info("No 0DTE state for %s — nothing to exit", today)
        return
    trade = ZeroDteTrade.model_validate_json(path.read_text())
    if trade.status != "open":
        log.info("0DTE %s status is %s — nothing to exit", today, trade.status)
        return

    tp_qty, tp_price = 0, 0.0
    if trade.tp_order_id:
        client.cancel(trade.tp_order_id)
        tp = client.wait_terminal(trade.tp_order_id, 15)
        tp_qty = int(float(tp.get("filled_qty") or 0))
        tp_price = float(tp.get("filled_avg_price") or 0)

    left = client.position_qty(trade.symbol)
    sold_qty, sold_price = 0, 0.0
    if left:
        sale = client.wait_terminal(client.submit(trade.symbol, left, "sell", "market")["id"], 30)
        sold_qty = int(float(sale.get("filled_qty") or 0))
        sold_price = float(sale.get("filled_avg_price") or 0)
        if sold_qty < left:
            trade.status, trade.reason = "error", f"cutoff sale filled {sold_qty}/{left}: {sale.get('status')}"
            return _finish(cfg, trade, f"❌ **0DTE {today}** — could not fully close {trade.symbol} "
                                       f"({trade.reason}). CLOSE IT MANUALLY before expiry.")

    exited = tp_qty + sold_qty
    if exited < trade.qty:
        trade.status = "error"
        trade.reason = (f"only {exited}/{trade.qty} contracts exited through the runner — "
                        f"closed elsewhere (broker auto-close?)")
        return _finish(cfg, trade, f"⚠️ **0DTE {today}** — {trade.symbol}: {trade.reason}. "
                                   f"Check the Alpaca order history; P&L not recorded.")
    trade.exit_price = (tp_qty * tp_price + sold_qty * sold_price) / exited if exited else 0.0
    trade.status = "take_profit" if tp_qty == trade.qty else "cutoff"
    pnl = trade.pnl or 0.0
    icon = "✅" if pnl > 0 else "🔻"
    equity = float(client.account()["equity"])
    _finish(cfg, trade, f"{icon} **0DTE {today}** — {trade.symbol} {trade.status.replace('_', ' ')}: "
                        f"${trade.entry_price:.2f} → ${trade.exit_price:.2f} × {trade.qty} = "
                        f"{'+' if pnl >= 0 else '−'}${abs(pnl):,.0f}. Account ${equity:,.0f}")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("enter", "exit"):
        sys.exit("usage: python -m zerodte.main enter|exit")
    cfg = Config.from_env()
    client = ZeroDteClient(cfg)
    try:
        enter(cfg, client) if sys.argv[1] == "enter" else exit_(cfg, client)
    except AlpacaError as exc:
        _notify(cfg, f"❌ **0DTE {sys.argv[1]}** — Alpaca error: {exc}")
        raise


if __name__ == "__main__":
    main()
