"""
Executor entrypoint — runs as a k3s CronJob after the decision gate, while the
US options market is open (see k8s/executor-cronjob.yaml).

Reads:  $INPUT_DIR/approved.json    (gate output)
Writes: $OUTPUT_DIR/executions.json (order receipts)
        $OUTPUT_DIR/executed_batch.json (dedup marker, see _mark_executed)

Three preflight guards stand between approved.json and a live order, because
approved.json persists on the shared PVC and a blind re-read is a double-order:
  1. staleness  — the batch must be younger than MAX_APPROVAL_AGE_HOURS
  2. dedup      — a batch already executed is never executed twice
  3. market open— Alpaca rejects options orders outside regular hours (422)
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

from audit import storage as audit

from executor.client import AlpacaOptionsClient
from executor.config import Config
from executor.models import ExecutionResult
from gate.models import ApprovedTrade

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("executor")

_ACTION_TO_OPTION_TYPE = {"BUY_CALL": "call", "BUY_PUT": "put"}
_BATCH_MARKER = "executed_batch.json"


# ── Notifications ─────────────────────────────────────────────────────────────


def _notify_execution(result: ExecutionResult, cfg: Config) -> None:
    """Post the true outcome of one order to Discord. Best-effort — never raises.

    The title states what actually happened. It used to say FILLED for every
    submitted order, which is how three days of 422-rejected orders still read
    as successful trades.
    """
    if not cfg.discord_webhook_url:
        return

    if result.status == "error":
        title = f"❌ REJECTED — {result.ticker} {result.action}"
        color = 0xE74C3C
    elif result.status == "skipped":
        title = f"⏭️ SKIPPED — {result.ticker} {result.action}"
        color = 0xE67E22
    elif result.status == "dry_run":
        title = f"🧪 DRY RUN — {result.ticker} {result.action}"
        color = 0x95A5A6
    elif result.status == "filled":
        title = f"✅ FILLED — {result.ticker} {result.action}"
        color = 0x2ECC71 if result.action != "CLOSE" else 0x95A5A6
    else:  # submitted, fill unconfirmed
        title = f"📨 SUBMITTED — {result.ticker} {result.action}"
        color = 0x3498DB

    price_str = (
        f"${result.filled_avg_price:.2f} (filled)" if result.filled_avg_price is not None
        else (f"${result.limit_price:.2f} (limit)" if result.limit_price else "market")
    )
    fields = [
        {"name": "Symbol",    "value": result.occ_symbol or result.ticker, "inline": True},
        {"name": "Contracts", "value": str(result.contracts),              "inline": True},
        {"name": "Price",     "value": price_str,                          "inline": True},
    ]
    if result.order_id:
        fields.append({"name": "Order ID", "value": result.order_id, "inline": False})
    if result.reason:
        fields.append({"name": "Reason", "value": result.reason[:512], "inline": False})

    embed = {
        "embeds": [{
            "title": title,
            "color": color,
            "fields": fields,
            "footer": {"text": f"status: {result.status}"},
            "timestamp": result.executed_at.isoformat(),
        }]
    }
    try:
        httpx.post(cfg.discord_webhook_url, json=embed, timeout=10).raise_for_status()
    except Exception:
        log.warning("Discord notification failed for %s", result.ticker)


def _notify_abort(cfg: Config, message: str) -> None:
    """Announce that the whole batch was not executed. Best-effort."""
    if not cfg.discord_webhook_url:
        return
    try:
        httpx.post(
            cfg.discord_webhook_url,
            json={"content": f"⚠️ **executor did not place orders** — {message}"},
            timeout=10,
        ).raise_for_status()
    except Exception:
        log.warning("Discord abort notification failed")


# ── Preflight guards ──────────────────────────────────────────────────────────


def batch_key(trades: list[ApprovedTrade], approved_path: str) -> str:
    """Stable identity for one gate run: the newest approval timestamp in the file.

    Falls back to the file's mtime for an empty batch, which has no timestamps.
    """
    if trades:
        return max(t.approved_at for t in trades).isoformat()
    return datetime.fromtimestamp(os.path.getmtime(approved_path), tz=timezone.utc).isoformat()


def batch_age(trades: list[ApprovedTrade], approved_path: str, now: datetime) -> timedelta:
    """How long ago the gate produced this batch."""
    stamp = datetime.fromisoformat(batch_key(trades, approved_path))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return now - stamp


def already_executed(cfg: Config, key: str) -> bool:
    path = os.path.join(cfg.output_dir, _BATCH_MARKER)
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            return json.load(f).get("batch_key") == key
    except Exception:
        log.warning("Could not read %s — treating batch as not yet executed", path)
        return False


def mark_executed(cfg: Config, key: str, trade_count: int) -> None:
    """Record the batch as consumed *before* any order is placed.

    Written up front, not after: a crash midway through a batch must not let a
    re-run place the earlier orders a second time.
    """
    os.makedirs(cfg.output_dir, exist_ok=True)
    with open(os.path.join(cfg.output_dir, _BATCH_MARKER), "w") as f:
        json.dump(
            {
                "batch_key": key,
                "trade_count": trade_count,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            f,
            indent=2,
        )


# ── Order placement ───────────────────────────────────────────────────────────


def _poll_fill(client: AlpacaOptionsClient, order: dict, ticker: str) -> float | None:
    """Best-effort wait for a fill price. Never raises — missing fill data just
    means this trade won't be usable for performance stats later."""
    order_id = order.get("id")
    if not order_id:
        return None
    try:
        filled = client.wait_for_fill(order_id)
    except Exception:
        log.warning("[%s] Fill poll failed for order %s", ticker, order_id)
        return None
    price = filled.get("filled_avg_price")
    return float(price) if price else None


def execute_close(
    trade: ApprovedTrade, client: AlpacaOptionsClient, cfg: Config,
) -> list[ExecutionResult]:
    """Sell-to-close all open option contracts for the ticker.

    Contract details are read from Alpaca live positions — never from model output.
    """
    p = trade.proposal
    now = datetime.now(tz=timezone.utc)

    try:
        positions = client.get_open_positions_for_ticker(p.ticker)
    except Exception as exc:
        log.error("[%s] Position lookup failed: %s", p.ticker, exc)
        return [ExecutionResult(
            ticker=p.ticker, occ_symbol="", action="CLOSE",
            contracts=0, order_type="market", status="error", reason=str(exc),
            executed_at=now,
        )]

    if not positions:
        reason = f"no open option positions found for {p.ticker}"
        log.warning("[%s] %s", p.ticker, reason)
        return [ExecutionResult(
            ticker=p.ticker, occ_symbol="", action="CLOSE",
            contracts=0, order_type="market", status="error", reason=reason,
            executed_at=now,
        )]

    results: list[ExecutionResult] = []
    for pos in positions:
        occ_symbol = pos["symbol"]
        qty = abs(int(float(pos["qty"])))
        log.info("[%s] Closing %d × %s", p.ticker, qty, occ_symbol)

        if cfg.dry_run:
            log.info("[%s] DRY RUN — would sell-to-close %s", p.ticker, occ_symbol)
            results.append(ExecutionResult(
                ticker=p.ticker, occ_symbol=occ_symbol, action="CLOSE",
                contracts=qty, order_type="market", status="dry_run", executed_at=now,
            ))
            continue

        try:
            order = client.close_position(occ_symbol)
            log.info(
                "[%s] Close submitted: %s id=%s status=%s",
                p.ticker, occ_symbol, order.get("id"), order.get("status"),
            )
            filled_avg_price = _poll_fill(client, order, p.ticker)
            r = ExecutionResult(
                ticker=p.ticker, occ_symbol=occ_symbol, action="CLOSE",
                contracts=qty, order_id=order.get("id"), order_type="market",
                filled_avg_price=filled_avg_price,
                status="filled" if filled_avg_price is not None else "submitted",
                executed_at=now,
            )
            _notify_execution(r, cfg)
            results.append(r)
        except Exception as exc:
            log.error("[%s] Close failed for %s: %s", p.ticker, occ_symbol, exc)
            r = ExecutionResult(
                ticker=p.ticker, occ_symbol=occ_symbol, action="CLOSE",
                contracts=qty, order_type="market", status="error", reason=str(exc),
                executed_at=now,
            )
            _notify_execution(r, cfg)
            results.append(r)
    return results


def execute_trade(
    trade: ApprovedTrade, client: AlpacaOptionsClient, cfg: Config
) -> ExecutionResult:
    p = trade.proposal
    option_type = _ACTION_TO_OPTION_TYPE[p.action]
    now = datetime.now(tz=timezone.utc)

    # 1. Locate the closest matching contract on Alpaca
    try:
        contract = client.find_contract(p.ticker, p.expiry, p.strike, option_type)
    except Exception as exc:
        log.error("[%s] Contract lookup failed: %s", p.ticker, exc)
        return ExecutionResult(
            ticker=p.ticker, occ_symbol="", action=p.action,
            strike=p.strike, expiry=str(p.expiry),
            contracts=p.suggested_contracts, order_type=cfg.order_type,
            status="error", reason=str(exc), executed_at=now,
        )

    if contract is None:
        reason = f"no contract found near strike {p.strike} expiry {p.expiry}"
        log.error("[%s] %s", p.ticker, reason)
        return ExecutionResult(
            ticker=p.ticker, occ_symbol="", action=p.action,
            strike=p.strike, expiry=str(p.expiry),
            contracts=p.suggested_contracts, order_type=cfg.order_type,
            status="error", reason=reason, executed_at=now,
        )

    occ_symbol = contract["symbol"]
    actual_strike = float(contract["strike_price"])
    log.info("[%s] Matched contract %s (strike=%.2f)", p.ticker, occ_symbol, actual_strike)

    # 2. Determine limit price. No quote means no order: a market order on an
    #    option with an unknown spread can fill anywhere, so skip unless the
    #    fallback is explicitly enabled (ALLOW_MARKET_FALLBACK).
    order_type = cfg.order_type
    limit_price: float | None = None
    if cfg.order_type == "limit":
        ask = client.get_ask_price(occ_symbol)
        if ask is None:
            if not cfg.allow_market_fallback:
                reason = f"no ask quote for {occ_symbol} (feed={cfg.options_feed}) — skipped"
                log.error("[%s] %s", p.ticker, reason)
                return ExecutionResult(
                    ticker=p.ticker, occ_symbol=occ_symbol, action=p.action,
                    strike=actual_strike, expiry=str(p.expiry),
                    contracts=p.suggested_contracts, order_type=cfg.order_type,
                    status="skipped", reason=reason, executed_at=now,
                )
            log.warning("[%s] No ask price — falling back to market order", p.ticker)
            order_type = "market"
        else:
            limit_price = round(ask * cfg.limit_buffer, 2)
            log.info(
                "[%s] Limit price: %.2f (ask=%.2f × buffer=%.2f)",
                p.ticker, limit_price, ask, cfg.limit_buffer,
            )

    # 3. Dry-run short-circuit — log intent but skip actual order
    if cfg.dry_run:
        log.info(
            "[%s] DRY RUN — would place %s %d × %s @ %s",
            p.ticker, order_type, p.suggested_contracts, occ_symbol,
            f"{limit_price:.2f}" if limit_price else "market",
        )
        return ExecutionResult(
            ticker=p.ticker, occ_symbol=occ_symbol, action=p.action,
            strike=actual_strike, expiry=str(p.expiry),
            contracts=p.suggested_contracts, order_type=order_type,
            limit_price=limit_price, status="dry_run", executed_at=now,
        )

    # 4. Place the order
    try:
        order = client.place_order(occ_symbol, p.suggested_contracts, order_type, limit_price)
    except Exception as exc:
        log.error("[%s] Order placement failed: %s", p.ticker, exc)
        return ExecutionResult(
            ticker=p.ticker, occ_symbol=occ_symbol, action=p.action,
            strike=actual_strike, expiry=str(p.expiry),
            contracts=p.suggested_contracts, order_type=order_type,
            limit_price=limit_price, status="error", reason=str(exc), executed_at=now,
        )

    log.info("[%s] Order submitted: id=%s status=%s", p.ticker, order.get("id"), order.get("status"))
    filled_avg_price = _poll_fill(client, order, p.ticker)
    result = ExecutionResult(
        ticker=p.ticker, occ_symbol=occ_symbol, action=p.action,
        strike=actual_strike, expiry=str(p.expiry),
        contracts=p.suggested_contracts, order_id=order.get("id"),
        order_type=order_type, limit_price=limit_price,
        filled_avg_price=filled_avg_price,
        status="filled" if filled_avg_price is not None else "submitted",
        executed_at=now,
    )
    _notify_execution(result, cfg)
    return result


# ── Entrypoint ────────────────────────────────────────────────────────────────


def _write_results(cfg: Config, results: list[ExecutionResult]) -> None:
    os.makedirs(cfg.output_dir, exist_ok=True)
    out_path = os.path.join(cfg.output_dir, "executions.json")
    payload = [r.model_dump(mode="json") for r in results]
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    log.info("Wrote %d execution result(s) to %s", len(results), out_path)
    audit.record_executions(results)
    print(json.dumps(payload, indent=2, default=str))


def main() -> None:
    cfg = Config.from_env()
    client = AlpacaOptionsClient(cfg)

    approved_path = os.path.join(cfg.input_dir, "approved.json")
    if not os.path.exists(approved_path):
        log.error("approved.json not found at %s — run gate first", approved_path)
        sys.exit(1)

    with open(approved_path) as f:
        raw = json.load(f)

    trades = [ApprovedTrade.model_validate(t) for t in raw]
    key = batch_key(trades, approved_path)
    age = batch_age(trades, approved_path, datetime.now(timezone.utc))
    log.info("Loaded %d approved trade(s) — batch %s, age %.1fh", len(trades), key, age.total_seconds() / 3600)

    if not trades:
        log.info("Nothing to execute")
        _write_results(cfg, [])
        return

    # ── Guard 1: staleness ────────────────────────────────────────────────
    if age > timedelta(hours=cfg.max_approval_age_hours):
        msg = (
            f"approved.json is {age.total_seconds() / 3600:.1f}h old "
            f"(max {cfg.max_approval_age_hours}h) — the gate likely did not run today. "
            f"{len(trades)} trade(s) dropped."
        )
        log.error(msg)
        _notify_abort(cfg, msg)
        sys.exit(1)

    # ── Guard 2: this batch was already executed ──────────────────────────
    if already_executed(cfg, key):
        log.warning("Batch %s already executed — refusing to place duplicate orders", key)
        return

    # ── Guard 3: the market must be open ──────────────────────────────────
    if cfg.require_market_open and not cfg.dry_run:
        try:
            clock = client.get_clock()
        except Exception as exc:
            msg = f"could not read the Alpaca market clock: {exc}"
            log.error(msg)
            _notify_abort(cfg, msg)
            sys.exit(1)
        if not clock.get("is_open"):
            msg = (
                f"US options market is closed (next open {clock.get('next_open')}). "
                f"{len(trades)} trade(s) not placed — Alpaca rejects options orders "
                f"outside regular hours."
            )
            log.error(msg)
            _notify_abort(cfg, msg)
            sys.exit(1)
        log.info("Market is open (closes %s) — proceeding", clock.get("next_close"))

    # Claim the batch before placing anything, so a mid-batch crash cannot be
    # replayed into duplicate orders by a retry or a manual re-run.
    mark_executed(cfg, key, len(trades))

    results: list[ExecutionResult] = []
    for trade in trades:
        if trade.proposal.action == "CLOSE":
            results.extend(execute_close(trade, client, cfg))
        else:
            results.append(execute_trade(trade, client, cfg))

    _write_results(cfg, results)

    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    log.info("Execution summary: %s", by_status or "nothing executed")

    # Non-zero exit if any trade errored — alerts k8s to the failure
    if any(r.status == "error" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
