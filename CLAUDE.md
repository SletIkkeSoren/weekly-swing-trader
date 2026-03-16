# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

## Project: Alpaca Options Trading Bot

### Architecture
- Data fetcher → Consensus engine (Claude + GPT-4o + Gemini) → Decision gate → Executor
- Runs on k3s as CronJobs
- Paper trading first via Alpaca sandbox API

### Core Rules (never violate these)
- LLMs reason about data, they never recall market facts
- All TA indicators computed deterministically before model calls
- Models receive OHLCV data + computed indicators, never raw P&L history
- Structured JSON output required; invalid schema = auto-reject
- Human approval webhook before any live execution

### Output Schema (all model calls must return this)
{ action, strike, expiry, confidence, reasoning[], invalidating_conditions[] }

### Stack
- Python 3.11
- alpaca-trade-api, ta (not pandas-ta — removed from PyPI), httpx, FastAPI
- Kubernetes manifests in /k8s

## Development environment

- Running on Windows via WSL2 (FedoraLinux-42)
- Copy `.env.example` → `.env` and fill in Alpaca paper-trading credentials before running locally

```bash
pip install -r requirements.txt

# Run fetcher locally (reads .env automatically via python-dotenv)
python -m fetcher.main
```

## Module: fetcher/

Pipeline stage 1. Fetches OHLCV bars from Alpaca, computes all TA indicators, writes `snapshots.json` to `OUTPUT_DIR`.

- `config.py` — env-based `Config` dataclass
- `client.py` — `AlpacaClient.fetch_bars(ticker)` → `list[OHLCVBar]`
- `indicators.py` — `compute(bars)` → `TechnicalIndicators` (uses `ta` library, deterministic)
- `models.py` — Pydantic models: `OHLCVBar`, `TechnicalIndicators`, `MarketSnapshot`
- `main.py` — CronJob entrypoint; writes `$OUTPUT_DIR/snapshots.json` and prints JSON to stdout

Indicators computed: EMA 8/21/50/200, RSI-14, MACD(12,26,9), Bollinger Bands(20,2σ), ATR-14, HV-20 (annualised), volume ratio, nearest S/R pivots.

k8s manifest: `k8s/fetcher-cronjob.yaml` — runs Mon–Fri 21:00 UTC; reads Alpaca credentials from `alpaca-credentials` secret.

## Module: consensus/

Pipeline stage 2. Reads `$INPUT_DIR/snapshots.json`, fans out to Claude + GPT-4o + Gemini in parallel, validates schema, aggregates votes, writes `$OUTPUT_DIR/consensus.json` (passed results only).

- `config.py` — API keys and model names from env
- `models.py` — `ModelVote`, `ConsensusResult`; `passed=True` requires ≥ 2/3 agreement on a non-HOLD action
- `prompt.py` — `SYSTEM_PROMPT` + `build_user_message(snapshot)`: last 20 bars + all indicators; models are explicitly forbidden from recalling training-data market facts
- `callers/claude.py`, `callers/gemini.py`, `callers/gemini2.py` — one async `call(snapshot, cfg) → ModelVote | None` per provider; returns None on any error or schema violation
- `engine.py` — `evaluate_all(snapshots, cfg)` runs all tickers concurrently; per ticker: gathers 3 votes in parallel, counts action agreement, averages strike/confidence from agreeing votes
- `main.py` — CronJob entrypoint

```bash
python -m consensus.main
```

k8s manifest: `k8s/consensus-cronjob.yaml` — runs Mon–Fri 21:30 UTC (30 min after fetcher); reads LLM keys from `llm-credentials` secret.

## Module: gate/

Pipeline stage 3. Reads `$INPUT_DIR/consensus.json`, applies risk filters, sizes positions, sends proposals to a human approval webhook, writes `$OUTPUT_DIR/approved.json`.

- `config.py` — all thresholds and sizing params from env; `ACCOUNT_SIZE` is required
- `models.py` — `TradeProposal` (filter-cleared), `ApprovedTrade` (webhook-approved, consumed by executor)
- `filters.py` — `run_all()` chains: min confidence → min agreement → max open trades → strike sanity (>15% from close rejected); `suggested_contracts()` sizes via `(account_size * risk_pct) / 200`
- `webhook.py` — POSTs `TradeProposal` JSON to `APPROVAL_WEBHOOK_URL`, expects `{"approved": bool, "reason": "..."}`. Empty URL = dry-run mode (logs proposal, approves nothing)
- `main.py` — CronJob entrypoint; `backoffLimit: 0` in k8s to prevent double-firing the webhook

```bash
python -m gate.main
```

k8s manifest: `k8s/gate-cronjob.yaml` — runs Mon–Fri 22:00 UTC; `APPROVAL_WEBHOOK_URL` from `gate-secrets` secret.
