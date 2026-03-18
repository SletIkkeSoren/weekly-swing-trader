"""
Fetcher entrypoint — runs as a k3s CronJob.

Output: writes snapshots.json to OUTPUT_DIR and prints JSON to stdout
so the pipeline can consume it from a shared volume or log capture.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from fetcher.client import AlpacaClient
from fetcher.config import Config
from fetcher.indicators import compute
from fetcher.models import MarketSnapshot

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("fetcher")


def run(cfg: Config) -> list[MarketSnapshot]:
    client = AlpacaClient(cfg)
    snapshots: list[MarketSnapshot] = []

    # Fetch open positions once for all tickers
    try:
        all_positions = client.fetch_open_positions()
        log.info("Fetched %d open option position(s)", len(all_positions))
    except Exception:
        log.exception("Failed to fetch open positions — continuing without position data")
        all_positions = []

    positions_by_ticker: dict[str, list] = {}
    for pos in all_positions:
        positions_by_ticker.setdefault(pos.ticker, []).append(pos)

    for ticker in cfg.tickers:
        try:
            log.info("Fetching %s", ticker)
            bars = client.fetch_bars(ticker)
            indicators = compute(bars)
            snapshot = MarketSnapshot(
                ticker=ticker,
                as_of=datetime.now(timezone.utc),
                latest_close=bars[-1].close,
                bars=bars,
                indicators=indicators,
                open_positions=positions_by_ticker.get(ticker, []),
            )
            snapshots.append(snapshot)
            log.info(
                "%s — close=%.2f rsi=%.1f hv20=%.1f%% open_positions=%d",
                ticker,
                snapshot.latest_close,
                snapshot.indicators.rsi_14,
                snapshot.indicators.hv_20 * 100,
                len(snapshot.open_positions),
            )
        except Exception:
            log.exception("Failed to process %s — skipping", ticker)

    return snapshots


def main() -> None:
    cfg = Config.from_env()
    log.info("Fetcher starting — tickers=%s lookback=%d", cfg.tickers, cfg.lookback_days)

    snapshots = run(cfg)
    if not snapshots:
        log.error("No snapshots produced; exiting with error")
        sys.exit(1)

    payload = [s.model_dump(mode="json") for s in snapshots]
    output_json = json.dumps(payload, indent=2, default=str)

    os.makedirs(cfg.output_dir, exist_ok=True)
    out_path = os.path.join(cfg.output_dir, "snapshots.json")
    with open(out_path, "w") as f:
        f.write(output_json)
    log.info("Wrote %d snapshots to %s", len(snapshots), out_path)

    # Also emit to stdout for k3s log capture / pipeline chaining
    print(output_json)


if __name__ == "__main__":
    main()
