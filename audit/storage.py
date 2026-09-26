"""
Audit trail — appends pipeline output to Hetzner Object Storage (S3-compatible).

Each stage writes one JSONL file per day:
  snapshots/{YYYY-MM-DD}.jsonl
  consensus/{YYYY-MM-DD}.jsonl
  approved/{YYYY-MM-DD}.jsonl
  executions/{YYYY-MM-DD}.jsonl

Set AUDIT_S3_ENDPOINT to enable. If unset, all calls are silent no-ops so local
dev and dry-run environments are unaffected.

Required env vars (when AUDIT_S3_ENDPOINT is set):
  AUDIT_S3_ENDPOINT   — e.g. https://fsn1.your-objectstorage.com
  AUDIT_S3_BUCKET     — bucket name
  AUDIT_S3_ACCESS_KEY — access key ID
  AUDIT_S3_SECRET_KEY — secret access key
  AUDIT_S3_REGION     — optional, defaults to fsn1
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

log = logging.getLogger("audit")


def _client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["AUDIT_S3_ENDPOINT"],
        aws_access_key_id=os.environ["AUDIT_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["AUDIT_S3_SECRET_KEY"],
        region_name=os.getenv("AUDIT_S3_REGION", "fsn1"),
        config=Config(s3={"addressing_style": "path"}),
    )


def _append_jsonl(stage: str, records: list[dict], run_at: str) -> None:
    """Download today's JSONL for this stage, append new records, re-upload."""
    bucket = os.environ["AUDIT_S3_BUCKET"]
    key = f"{stage}/{run_at[:10]}.jsonl"  # e.g. snapshots/2026-03-27.jsonl

    client = _client()

    existing = ""
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        existing = resp["Body"].read().decode()
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("NoSuchKey", "404"):
            raise

    new_lines = "\n".join(json.dumps({"run_at": run_at, **r}, default=str) for r in records)
    content = (existing.rstrip("\n") + "\n" + new_lines + "\n").lstrip("\n")

    client.put_object(Bucket=bucket, Key=key, Body=content.encode())
    log.info("Audit: appended %d record(s) to s3://%s/%s", len(records), bucket, key)


def _is_enabled() -> bool:
    return bool(os.getenv("AUDIT_S3_ENDPOINT"))


def record_snapshots(snapshots: list, run_at: str | None = None) -> None:
    """Append fetcher output. No-op if AUDIT_S3_ENDPOINT is unset."""
    if not _is_enabled():
        return
    try:
        ts = run_at or datetime.now(timezone.utc).isoformat()
        records = [s.model_dump(mode="json") for s in snapshots]
        _append_jsonl("snapshots", records, ts)
    except Exception:
        log.exception("Audit write failed (snapshots) — pipeline continues")


def record_consensus(results: list, run_at: str | None = None) -> None:
    """Append all consensus results (passed and non-passed). No-op if AUDIT_S3_ENDPOINT is unset."""
    if not _is_enabled():
        return
    try:
        ts = run_at or datetime.now(timezone.utc).isoformat()
        records = [r.model_dump(mode="json") for r in results]
        _append_jsonl("consensus", records, ts)
    except Exception:
        log.exception("Audit write failed (consensus) — pipeline continues")


def record_approved(trades: list, run_at: str | None = None) -> None:
    """Append gate-approved trades. No-op if AUDIT_S3_ENDPOINT is unset."""
    if not _is_enabled():
        return
    try:
        ts = run_at or datetime.now(timezone.utc).isoformat()
        records = [t.model_dump(mode="json") for t in trades]
        _append_jsonl("approved", records, ts)
    except Exception:
        log.exception("Audit write failed (approved) — pipeline continues")


def record_zerodte(trades: list, run_at: str | None = None) -> None:
    """Append 0DTE runner outcomes (zerodte/{date}.jsonl). No-op if AUDIT_S3_ENDPOINT is unset."""
    if not _is_enabled():
        return
    try:
        ts = run_at or datetime.now(timezone.utc).isoformat()
        records = [t.model_dump(mode="json") for t in trades]
        _append_jsonl("zerodte", records, ts)
    except Exception:
        log.exception("Audit write failed (zerodte) — pipeline continues")


def record_executions(results: list, run_at: str | None = None) -> None:
    """Append executor results. No-op if AUDIT_S3_ENDPOINT is unset."""
    if not _is_enabled():
        return
    try:
        ts = run_at or datetime.now(timezone.utc).isoformat()
        records = [r.model_dump(mode="json") for r in results]
        _append_jsonl("executions", records, ts)
    except Exception:
        log.exception("Audit write failed (executions) — pipeline continues")
