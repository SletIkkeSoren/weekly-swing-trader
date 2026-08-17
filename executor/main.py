"""
Executor entrypoint — runs as a k3s CronJob after the decision gate.

Reads:  $INPUT_DIR/approved.json    (gate output)
Writes: $OUTPUT_DIR/executions.json (order receipts)
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

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


def _notify_fill(result: ExecutionResult, cfg: Config) -> None:
    """Post a compact fill notification to Discord. Best-effort — never raises."""
    if not cfg.discord_webhook_url:
        return
    action_emoji = "📈" if result.action == "BUY_CALL" else ("📉" if result.action == "BUY_PUT" else "🔄")
    color = 0x2ECC71 if result.action == "BUY_CALL" else (0xE74C3C if result.action == "BUY_PUT" else 0x95A5A6)
    price_str = f"${result.limit_price:.2f}" if result.limit_price else "market"
    fields = [
        {"name": "Symbol",    "value": result.occ_symbol or result.ticker, "inline": True},
        {"name": "Contracts", "value": str(result.contracts),              "inline": True},
        {"name": "Price",     "value": price_str,                          "inline": True},
    ]
    if result.order_id:
        fields.append({"name": "Order ID", "value": result.order_id, "inline": False})
    embed = {
        "embeds": [{
            "title": f"{action_emoji} FILLED — {result.ticker} {result.action}",
            "color": color,
            "fields": fields,
            "footer": {"text": f"status: {result.status}"},
            "timestamp": result.executed_at.isoformat(),
        }]
    }
    try:
        httpx.post(cfg.discord_webhook_url, json=embed, timeout=10).raise_for_status()
    except Exception:
        log.warning("Discord fill notification failed for %s", result.ticker)


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
                status="submitted", executed_at=now,
            )
            _notify_fill(r, cfg)
            results.append(r)
        except Exception as exc:
            log.error("[%s] Close failed for %s: %s", p.ticker, occ_symbol, exc)
            results.append(ExecutionResult(
                ticker=p.ticker, occ_symbol=occ_symbol, action="CLOSE",
                contracts=qty, order_type="market", status="error", reason=str(exc),
                executed_at=now,
            ))
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

    # 2. Determine limit price (fall back to market if no quote available)
    order_type = cfg.order_type
    limit_price: float | None = None
    if cfg.order_type == "limit":
        ask = client.get_ask_price(p.ticker, occ_symbol)
        if ask is None:
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
        status="submitted", executed_at=now,
    )
    _notify_fill(result, cfg)
    return result


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
    log.info("Loaded %d approved trade(s)", len(trades))

    results: list[ExecutionResult] = []
    for trade in trades:
        if trade.proposal.action == "CLOSE":
            results.extend(execute_close(trade, client, cfg))
        else:
            results.append(execute_trade(trade, client, cfg))

    os.makedirs(cfg.output_dir, exist_ok=True)
    out_path = os.path.join(cfg.output_dir, "executions.json")
    payload = [r.model_dump(mode="json") for r in results]
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    log.info("Wrote %d execution result(s) to %s", len(results), out_path)
    audit.record_executions(results)

    print(json.dumps(payload, indent=2, default=str))

    # Non-zero exit if any trade errored — alerts k8s to the failure
    if any(r.status == "error" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
