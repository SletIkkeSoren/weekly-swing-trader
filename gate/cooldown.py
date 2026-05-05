"""
Per-ticker reentry cooldown — prevents re-buying a ticker that was closed today.

State is stored as a JSON file on the shared PVC so it persists across hourly CronJob pods.
Cooldowns are scoped to the current calendar date; they auto-expire at midnight.
"""

import json
import logging
import os
from datetime import date

log = logging.getLogger(__name__)

_COOLDOWN_FILE = "cooldown.json"


def _path(output_dir: str) -> str:
    return os.path.join(output_dir, _COOLDOWN_FILE)


def _load(output_dir: str) -> dict[str, str]:
    p = _path(output_dir)
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict[str, str], output_dir: str) -> None:
    os.makedirs(output_dir, exist_ok=True)
    with open(_path(output_dir), "w") as f:
        json.dump(data, f, indent=2)


def is_on_cooldown(ticker: str, output_dir: str) -> bool:
    """Return True if this ticker was closed today — block re-entry."""
    closed_on = _load(output_dir).get(ticker)
    return closed_on == str(date.today())


def mark_closed(ticker: str, output_dir: str) -> None:
    """Record that a position in this ticker was closed today."""
    data = _load(output_dir)
    data[ticker] = str(date.today())
    _save(data, output_dir)
    log.info("[%s] Cooldown set — no re-entry until tomorrow", ticker)
