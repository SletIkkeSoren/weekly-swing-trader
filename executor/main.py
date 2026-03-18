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

from dotenv import load_dotenv

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


def execute_close(
    trade: ApprovedTrade, client: AlpacaOptionsClient, cfg: Config
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
            results.append(ExecutionResult(
                ticker=p.ticker, occ_symbol=occ_symbol, action="CLOSE",
                contracts=qty, order_id=order.get("id"), order_type="market",
                status="submitted", executed_at=now,
            ))
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
    return ExecutionResult(
        ticker=p.ticker, occ_symbol=occ_symbol, action=p.action,
        strike=actual_strike, expiry=str(p.expiry),
        contracts=p.suggested_contracts, order_id=order.get("id"),
        order_type=order_type, limit_price=limit_price,
        status="submitted", executed_at=now,
    )


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

    print(json.dumps(payload, indent=2, default=str))

    # Non-zero exit if any trade errored — alerts k8s to the failure
    if any(r.status == "error" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
