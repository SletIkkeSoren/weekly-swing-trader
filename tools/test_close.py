"""
Manual close test — bypasses the full pipeline and fires execute_close() directly.

Usage:
    python tools/test_close.py TSLA
    python tools/test_close.py TSLA --dry-run    # default; won't place a real order
    python tools/test_close.py TSLA --live        # actually closes the position

Requires Alpaca credentials in the environment (direnv / .envrc).
"""

import argparse
import json
import os
import sys

# Ensure project root is on sys.path when running as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from executor.client import AlpacaOptionsClient
from executor.config import Config
from executor.main import execute_close
from gate.models import ApprovedTrade, TradeProposal


def main() -> None:
    parser = argparse.ArgumentParser(description="Test close execution for a ticker")
    parser.add_argument("ticker", help="Underlying ticker, e.g. TSLA")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Place a real sell-to-close order (default: dry-run)",
    )
    args = parser.parse_args()

    cfg = Config.from_env()

    # Override dry_run unless --live was passed
    if not args.live:
        cfg.dry_run = True

    client = AlpacaOptionsClient(cfg)

    # First, show what open positions exist for this ticker
    print(f"\nLooking up open positions for {args.ticker}...")
    positions = client.get_open_positions_for_ticker(args.ticker)
    if not positions:
        print(f"No open option positions found for {args.ticker}. Nothing to close.")
        sys.exit(0)

    print(f"Found {len(positions)} position(s):")
    for p in positions:
        print(f"  {p['symbol']}  qty={p['qty']}  "
              f"avg={p['avg_entry_price']}  current={p['current_price']}")

    # Build a minimal ApprovedTrade with action=CLOSE
    proposal = TradeProposal(
        ticker=args.ticker.upper(),
        action="CLOSE",
        strike=None,
        expiry=None,
        confidence=1.0,
        agreement_count=3,
        suggested_contracts=0,  # ignored by execute_close — it reads live qty
        risk_usd=0.0,
        all_reasoning=["manual test close"],
        all_invalidating_conditions=[],
        as_of=datetime.now(timezone.utc),
    )
    trade = ApprovedTrade(
        proposal=proposal,
        approved_at=datetime.now(timezone.utc),
        webhook_reason="manual test",
    )

    mode = "LIVE" if args.live else "DRY-RUN"
    print(f"\nExecuting CLOSE [{mode}]...")
    results = execute_close(trade, client, cfg)

    print("\nResults:")
    print(json.dumps([r.model_dump(mode="json") for r in results], indent=2, default=str))

    if any(r.status == "error" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
