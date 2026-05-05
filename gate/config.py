import os
from dataclasses import dataclass


@dataclass
class Config:
    input_dir: str
    output_dir: str

    # Risk filters
    min_confidence: float   # reject if consensus_confidence < this
    min_agreement: int      # reject if agreement_count < this (set to 3 for unanimous)
    max_open_trades: int    # reject if approved trades this run would exceed this

    # Position sizing
    account_size: float     # USD — used to calculate contract count
    risk_pct: float         # fraction of account to risk per trade (e.g. 0.01 = 1%)
    max_contracts: int      # hard cap regardless of sizing formula

    # Hold confidence threshold — HOLD below this triggers an auto-CLOSE
    min_hold_confidence: float

    # Human approval webhook
    # POST JSON payload, expect {"approved": true/false, "reason": "..."}
    # Set to empty string to run in dry-run mode (logs only, no webhook call)
    webhook_url: str
    webhook_timeout_secs: int

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            input_dir=os.getenv("INPUT_DIR", "/tmp/consensus"),
            output_dir=os.getenv("OUTPUT_DIR", "/tmp/gate"),
            min_confidence=float(os.getenv("MIN_CONFIDENCE", "0.65")),
            min_agreement=int(os.getenv("MIN_AGREEMENT", "2")),
            max_open_trades=int(os.getenv("MAX_OPEN_TRADES", "3")),
            account_size=float(os.environ["ACCOUNT_SIZE"]),
            risk_pct=float(os.getenv("RISK_PCT", "0.01")),
            max_contracts=int(os.getenv("MAX_CONTRACTS", "5")),
            min_hold_confidence=float(os.getenv("MIN_HOLD_CONFIDENCE", "0.75")),
            webhook_url=os.getenv("APPROVAL_WEBHOOK_URL", ""),
            webhook_timeout_secs=int(os.getenv("WEBHOOK_TIMEOUT_SECS", "300")),
        )
