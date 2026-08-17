"""
Audit trail reader — pulls historical JSONL records back from S3.

Mirrors storage.py's layout ({stage}/{YYYY-MM-DD}.jsonl, one GET per day in range).
Read-only. Used by audit/performance.py to reconstruct past trade outcomes so the
gate can act on the account's own track record.

Same enable/disable contract as storage.py: if AUDIT_S3_ENDPOINT is unset, every
call returns [] rather than raising, so local dev and dry-run environments are
unaffected and callers can treat "no history" as the normal empty case.
"""

import json
import logging
import os
from datetime import date, timedelta

from botocore.exceptions import ClientError

from audit.storage import _client, _is_enabled

log = logging.getLogger("audit.reader")


def load_stage(stage: str, since: date, until: date | None = None) -> list[dict]:
    """Return all records for `stage` between `since` and `until` (inclusive), oldest first."""
    if not _is_enabled():
        return []
    until = until or date.today()
    bucket = os.environ["AUDIT_S3_BUCKET"]
    client = _client()

    records: list[dict] = []
    day = since
    while day <= until:
        key = f"{stage}/{day.isoformat()}.jsonl"
        try:
            resp = client.get_object(Bucket=bucket, Key=key)
            body = resp["Body"].read().decode()
            for line in body.splitlines():
                if line.strip():
                    records.append(json.loads(line))
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                log.exception("Audit read failed for %s", key)
        except Exception:
            log.exception("Audit read failed for %s", key)
        day += timedelta(days=1)

    records.sort(key=lambda r: r.get("executed_at") or r.get("run_at") or "")
    return records
