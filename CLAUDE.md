# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

## Project: Alpaca Options Trading Bot

> **Status (2026-09-26):** the daily LLM-consensus options pipeline (fetcher →
> consensus → gate → executor → report) is **retired** — its CronJobs carry
> `suspend: true`. Backtests in `backtest/` found no edge for it. The live strategy
> is now `zerodte/` (see below). The sections on the old stages are kept for reference.

### Architecture
- Data fetcher → Consensus engine (Claude + GPT-4o + Gemini) → Decision gate → Executor → EOD report
- Runs on k3s as CronJobs
- Paper trading first via Alpaca sandbox API

### Daily timing (all UTC, Mon–Fri)
The analysis stages run **pre-open** so the models reason on the last completed
daily bar; the executor runs **inside the session** because Alpaca rejects options
orders outside regular hours with a 422.

| Time  | Stage     | Why |
|-------|-----------|-----|
| 12:00 | fetcher   | pre-open; last bar = previous session's close |
| 12:30 | consensus | pre-open                                      |
| 13:00 | gate      | pre-open; must finish before 13:30 (earliest open) |
| 15:00 | executor  | **in-session year-round** (11:00 EDT / 10:00 EST) |
| 21:30 | report    | after the close                               |

Never schedule the executor outside 14:30–20:00 UTC — that window is the only one
open in both US DST regimes. Running it post-close silently killed all execution
from 2026-08-31 to 09-02.

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

k8s manifest: `k8s/fetcher-cronjob.yaml` — runs Mon–Fri 12:00 UTC (pre-open); reads Alpaca credentials from `alpaca-credentials` secret.

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

k8s manifest: `k8s/consensus-cronjob.yaml` — runs Mon–Fri 12:30 UTC (30 min after fetcher); reads LLM keys from `llm-credentials` secret.

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

k8s manifest: `k8s/gate-cronjob.yaml` — runs Mon–Fri 13:00 UTC; `APPROVAL_WEBHOOK_URL` from `gate-secrets` secret.

The Discord embed the gate posts is an **approval**, not a fill. Only the executor knows whether an order reached the market.

## Module: executor/

Pipeline stage 4. Reads `$INPUT_DIR/approved.json`, places option orders on Alpaca,
writes `$OUTPUT_DIR/executions.json` and posts the real outcome to Discord.

- `config.py` — order type/sizing plus the safety switches below
- `client.py` — `AlpacaOptionsClient`: contract lookup, `/v2/clock`, quotes, orders.
  All responses go through `_check()`, which raises `AlpacaError` **carrying the
  response body** — `raise_for_status()` alone reduced every rejection to a bare
  "422 Unprocessable Entity"
- `main.py` — CronJob entrypoint; `backoffLimit: 0` in k8s to prevent double-ordering

Three preflight guards run before any order, because `approved.json` persists on the
shared PVC and a blind re-read is a duplicate order:

1. **staleness** — batch older than `MAX_APPROVAL_AGE_HOURS` (6) is dropped; means the gate didn't run
2. **dedup** — `executed_batch.json` records the batch key *before* ordering, so a crash or manual re-run can't replay it
3. **market open** — `REQUIRE_MARKET_OPEN=true` checks `/v2/clock` and aborts loudly rather than collecting 422s

Quotes come from `/v1beta1/options/snapshots?symbols=…` (the multi-contract route;
the per-underlying route rejects a `symbols` filter with a 400). With no ask quote the
trade is **skipped**, not sent as a market order — set `ALLOW_MARKET_FALLBACK=true` to
restore the old behaviour.

Execution status is `filled` (fill confirmed) / `submitted` (accepted, fill unknown) /
`skipped` / `error` / `dry_run`. Only `filled` and `submitted` count as real orders in
`audit/performance.py`.

```bash
python -m executor.main
```

k8s manifest: `k8s/executor-cronjob.yaml` — runs Mon–Fri 15:00 UTC, **in-session**.

## Module: report/

Pipeline stage 5. Read-only. Posts the end-of-day Discord summary from the audit
trail; writes nothing.

- `build.py` — `build_report(ticker_stats, approved_today, executions_today, today, base_min_confidence)`
- `main.py` — CronJob entrypoint

It reads **both** approvals and executions and flags any gap between them. Reporting
approvals alone is how three days of rejected orders were still summarised as trades taken.

```bash
python -m report.main
```

k8s manifest: `k8s/report-cronjob.yaml` — runs Mon–Fri 21:30 UTC (after the close).

## Module: backtest/

Research only — never imported by a live stage. `pip install -r requirements-backtest.txt`
(yfinance). Data caches in `.cache/backtest/` (gitignored).

- `main.py` — TA signals traded as debit spreads (BS-priced, HV proxy)
- `shares.py` — RSI-2 pullback with ETF shares vs random-entry baseline
- `putspread.py` — SPY put credit spreads priced from VIX + skew; sizing is a % of current equity
- `zerodte.py` — 0DTE on **real** Alpaca minute bars (Feb 2024→) for SPY, QQQ, IWM, ….
  `fetch [--ticker QQQ]` needs Alpaca keys, `run [--ticker QQQ] [--band 0.80,1.10]` is
  offline. 2024 is in-sample, 2025→ out-of-sample
  - `compare [--tickers SPY,QQQ,IWM]` — the live rule (orb, $0.80–1.10, 2x, 15:30) on each
    ticker, out-of-sample only: trades, win rate, avg/trade, sequential P&L and max
    drawdown for $1,000 at 20% of current equity, vs a random-direction baseline (median
    of 200 seeds), plus how often tickers break out on the same day and in the same direction
  - Cache: SPY stays in `.cache/backtest/zerodte/*.csv` (pre-multi-ticker layout); other
    tickers go in `.cache/backtest/zerodte/<TICKER>/`
  - Strike grid per ticker in `STRIKE_GRIDS` (SPY/QQQ $1, IWM $0.50). It only controls
    which contracts are fetched, not the rules. Nothing is tuned per ticker
  - A cached day with no same-day option bars is reported as **uncovered** and skipped,
    never counted as "no signal". `run`/`compare` print coverage, uncovered days by
    weekday (to show when daily expiries started) and the strike spacing actually seen

Always compare against a baseline (random entry / buy-and-hold) and judge rules on
the out-of-sample period only.

## Module: zerodte/

The live strategy: the one rule that held up out-of-sample in `backtest/zerodte.py` —
opening-range breakout (9:30–9:44 ET range, decided at 10:00 ET) → buy one same-day
SPY call/put priced $0.80–1.10, limit sell at 2x, sell the rest at 15:30 ET. A near
break-even, high-variance bet; sized at `RISK_FRACTION` (5%) of **current** equity, which
on $1k is the 1-contract minimum. 20% compounded to ruin out-of-sample (`backtest.zerodte compare`).

Entry order: limit at `PREMIUM_MAX` (top of the band), working until 10:05 ET. It fills at
the ask, so it only pays above the quote when the price moved — never above the band.
Bidding the quoted ask for 60s missed fills right after breakouts. Every candidate quote
(bid/ask and quote time) is stored in the trade's `quotes` field and in the audit trail,
and a skip names the filter that blocked it (price band, `MAX_QUOTE_SPREAD`, stale quotes).

- `strategy.py` — pure logic (signal, OTM candidates, contract pick, sizing); tested
- `client.py` — Alpaca REST (clock, account, IEX minute bars, option snapshots, orders)
- `main.py` — `enter` (10:01 ET) / `exit` (15:30 ET); state in `$STATE_DIR/{date}.json`

Guards in `enter`: market open and a full session (half days skipped), now inside
10:00–10:10 ET, no state for today (never buys twice), PDT (< `MAX_DAY_TRADES` round
trips in the previous 4 business days under $25k — every trade here is a day trade).
The count comes from the runner's own state files: this account's `/v2/account`
payload has no `daytrade_count`, so reading Alpaca alone made the guard a no-op.

k8s: `k8s/zerodte-cronjob.yaml` — two CronJobs with `timeZone: America/New_York`, so
DST needs no UTC conversion. Uses a **separate $1,000 paper account**
(`alpaca-zerodte-credentials` secret), audit stage `zerodte/`.

## Operational notes

- The Docker image **must** contain `curl`: every CronJob's failure-alert hook shells
  out to it. Without it the alert dies with `curl: not found` and failures pass silently.
