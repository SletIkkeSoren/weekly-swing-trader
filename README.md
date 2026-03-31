# Weekly Swing Trader

An automated options trading bot that uses a three-model LLM consensus (Claude + Gemini + GPT-4) to generate weekly swing trade signals on US equities. All LLM reasoning is grounded in deterministically computed technical indicators — models never recall market facts from training data.

Paper trading only by default. Live orders require explicit opt-in and human approval.

---

## How it works

The pipeline runs as four sequential Kubernetes CronJobs every hour during US market hours:

```
fetcher → consensus → gate → executor
  :30        :35       :40     :45  (past each hour, 13:30–19:45 UTC / 15:30–21:45 CEST)
```

### Stage 1 — Fetcher
Fetches OHLCV bars from Alpaca for each configured ticker and computes all technical indicators deterministically. Also checks the Alpaca market calendar — if today is a holiday, it writes an empty snapshot and exits cleanly so no downstream API calls are wasted.

Indicators computed: EMA 8/21/50/200, RSI-14, MACD(12,26,9), Bollinger Bands(20,2σ), ATR-14, HV-20 (annualised), volume ratio, nearest S/R pivots.

Output: `snapshots.json`

### Stage 2 — Consensus engine
Fans out to Claude, Gemini Flash, and GPT-4.1-nano in parallel. Each model receives the last 20 OHLCV bars + all computed indicators and must respond with structured JSON:

```json
{
  "action": "BUY_CALL" | "BUY_PUT" | "CLOSE" | "HOLD",
  "strike": 150.00,
  "expiry": "2026-04-11",
  "confidence": 0.78,
  "reasoning": ["..."],
  "invalidating_conditions": ["..."]
}
```

A result **passes** only when ≥ 2 of 3 models agree on the same non-HOLD action. Strike and confidence are averaged across agreeing votes.

Output: `consensus.json`

### Stage 3 — Decision gate
Applies risk filters to passed consensus results before sending them for human approval:

- Minimum confidence threshold (default 0.65)
- Minimum model agreement count (default 2/3)
- Maximum concurrent open trades (default 3)
- Strike sanity check — rejects strikes > 15% from current price

Position sizing: `floor((account_size × risk_pct) / $200)` contracts, clamped to `[1, max_contracts]`.

Proposals are POSTed to a configurable approval webhook (Discord or any HTTP endpoint that returns `{"approved": bool}`). Hard exits (stop-loss ≥ −50%, take-profit ≥ +150%, DTE ≤ 2) bypass the webhook entirely and are auto-approved.

Output: `approved.json`

### Stage 4 — Executor
Looks up the closest matching options contract on Alpaca, fetches the current ask price, and places a limit order (ask × 1.05 buffer by default). Falls back to market order if no quote is available.

For CLOSE actions, reads live Alpaca positions directly rather than trusting model output for the contract symbol.

Output: `executions.json`

---

## Prerequisites

- Python 3.11+
- An [Alpaca](https://alpaca.markets) account with paper trading and options trading enabled
- API keys for Anthropic, Google AI, and OpenAI
- A Discord webhook URL (optional — for trade approval and fill notifications)
- k3s / Kubernetes cluster (for production deployment)
- An S3-compatible object store (optional — for the audit trail)

---

## Local setup

```bash
git clone <repo>
cd weekly-swing-trader

pip install -r requirements.txt

cp .env.example .env
# Fill in your Alpaca paper credentials and LLM API keys
```

Enable options trading on your Alpaca paper account: **Account → Options Trading** in the Alpaca dashboard.

---

## Running locally

Each stage reads from the previous stage's output directory. Run them in order:

```bash
# Stage 1 — fetch market data
OUTPUT_DIR=/tmp/fetcher python -m fetcher.main

# Stage 2 — run LLM consensus
INPUT_DIR=/tmp/fetcher OUTPUT_DIR=/tmp/consensus python -m consensus.main

# Stage 3 — apply risk filters and request approval
INPUT_DIR=/tmp/consensus OUTPUT_DIR=/tmp/gate python -m gate.main

# Stage 4 — place orders
INPUT_DIR=/tmp/gate OUTPUT_DIR=/tmp/executor python -m executor.main
```

Set `DRY_RUN=true` in `.env` to skip actual order placement and log intent only.

Leave `APPROVAL_WEBHOOK_URL` empty to run the gate in dry-run mode (proposals are logged, nothing is approved).

---

## Kubernetes deployment

The pipeline runs on k3s as four CronJobs sharing a `PersistentVolumeClaim` for inter-stage data.

### 1. Create secrets

```bash
kubectl create secret generic alpaca-credentials \
  --from-literal=api_key=<KEY> \
  --from-literal=secret_key=<SECRET> \
  -n swing-trader

kubectl create secret generic llm-credentials \
  --from-literal=anthropic_api_key=<KEY> \
  --from-literal=google_api_key=<KEY> \
  --from-literal=openai_api_key=<KEY> \
  -n swing-trader

kubectl create secret generic gate-secrets \
  --from-literal=approval_webhook_url=<DISCORD_OR_WEBHOOK_URL> \
  -n swing-trader

# Optional — audit trail to S3-compatible storage
kubectl create secret generic audit-secrets \
  --from-literal=s3_endpoint=https://your-object-store.com \
  --from-literal=s3_bucket=swing-trader-audit \
  --from-literal=s3_access_key=<KEY> \
  --from-literal=s3_secret_key=<SECRET> \
  -n swing-trader
```

### 2. Configure tickers

Edit the `fetcher-config` ConfigMap in `k8s/fetcher-cronjob.yaml`:

```yaml
data:
  tickers: "SPY,QQQ,AAPL,MSFT,NVDA,TSLA,AMZN,META,GOOGL"
```

### 3. Apply manifests

```bash
kubectl apply -f k8s/
```

### Schedule

All times are CEST (UTC+2, summer). Update hour ranges from `13-19` to `14-20` in late October when clocks go back to CET.

| Stage | Cron (UTC) | CEST window |
|---|---|---|
| fetcher | `30 13-19 * * 1-5` | 15:30 – 21:30 |
| consensus | `35 13-19 * * 1-5` | 15:35 – 21:35 |
| gate | `40 13-19 * * 1-5` | 15:40 – 21:40 |
| executor | `45 13-19 * * 1-5` | 15:45 – **21:45** (15 min before close) |

---

## Audit trail

When `AUDIT_S3_ENDPOINT` is set, each stage appends its output to a JSONL file in S3-compatible object storage:

```
snapshots/YYYY-MM-DD.jsonl
consensus/YYYY-MM-DD.jsonl
approved/YYYY-MM-DD.jsonl
executions/YYYY-MM-DD.jsonl
```

If the env var is unset, all audit calls are silent no-ops — safe for local development.

---

## Safety features

- **Paper trading by default** — `ALPACA_BASE_URL` points to `paper-api.alpaca.markets`
- **Human approval required** — every BUY signal goes through the approval webhook before execution
- **Hard exits are automatic** — stop-loss (−50%), take-profit (+150%), and DTE ≤ 2 bypass the webhook and close immediately
- **No training data recall** — models are explicitly instructed to reason only from the provided data; the prompt enforces this
- **Schema validation** — invalid JSON or wrong schema from any model = that model's vote is discarded
- **Holiday detection** — fetcher checks the Alpaca market calendar on startup; exits cleanly on holidays so no LLM calls are made
- **Strike sanity check** — strikes more than 15% from current price are rejected by the gate

---

## Running tests

```bash
python -m pytest tests/ -v
```

97 tests covering filters, webhook logic, consensus engine, prompt construction, and TA indicators.
