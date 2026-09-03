"""
End-of-day report entrypoint — runs as a k3s CronJob after the executor.

Reads:  today's approved/{date}.jsonl and executions/{date}.jsonl records +
        the rolling per-ticker track record from the audit trail
        (audit/reader.py, audit/performance.py)
Writes: nothing — posts a Discord summary and exits. Read-only stage.
"""

import logging
import os
from datetime import date

import httpx
from dotenv import load_dotenv

from audit.performance import get_ticker_stats
from audit.reader import load_stage
from report.build import build_report

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("report")


def main() -> None:
    today = date.today()
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    base_min_confidence = float(os.getenv("MIN_CONFIDENCE", "0.65"))

    approved_today = load_stage("approved", today, today)
    executions_today = load_stage("executions", today, today)
    ticker_stats = get_ticker_stats()

    if not approved_today and not executions_today and not ticker_stats:
        log.info("No audit history yet (or audit S3 disabled) — nothing to report")
        return

    payload = build_report(
        ticker_stats, approved_today, executions_today, today, base_min_confidence
    )

    if not webhook_url:
        log.warning("DRY RUN — no DISCORD_WEBHOOK_URL set. Report:\n%s", payload)
        return

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(webhook_url, json=payload)
            resp.raise_for_status()
        log.info("End-of-day report posted to Discord")
    except Exception:
        log.exception("Discord report post failed")


if __name__ == "__main__":
    main()
