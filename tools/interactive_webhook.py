"""
Interactive approval webhook for local testing.

Opens a browser-based review UI for each trade proposal that the gate sends.
You click Approve or Reject; the gate waits (up to WEBHOOK_TIMEOUT_SECS) for
your response before continuing.

Usage:
    pip install fastapi uvicorn
    python tools/interactive_webhook.py

Then set APPROVAL_WEBHOOK_URL=http://localhost:8080 when running gate.
"""

import asyncio
import json
import logging
import uuid
import webbrowser
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger("webhook")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# proposal_id → {"proposal": dict, "event": asyncio.Event, "approved": bool | None}
_pending: dict[str, dict] = {}

TIMEOUT_SECS = 280  # slightly under gate's WEBHOOK_TIMEOUT_SECS


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Interactive webhook ready at http://localhost:8080")
    log.info("Set APPROVAL_WEBHOOK_URL=http://localhost:8080 in your gate run")
    yield


app = FastAPI(lifespan=lifespan)


# ── Gate calls this ────────────────────────────────────────────────────────────

@app.post("/")
async def receive_proposal(request: Request):
    proposal = await request.json()
    proposal_id = str(uuid.uuid4())[:8]
    event = asyncio.Event()
    _pending[proposal_id] = {"proposal": proposal, "event": event, "approved": None}

    ticker = proposal.get("ticker", "?")
    log.info("[%s] Proposal received — open http://localhost:8080 to review", ticker)
    webbrowser.open("http://localhost:8080")

    try:
        await asyncio.wait_for(event.wait(), timeout=TIMEOUT_SECS)
    except asyncio.TimeoutError:
        _pending.pop(proposal_id, None)
        log.warning("[%s] Timed out waiting for decision — rejecting", ticker)
        return JSONResponse({"approved": False, "reason": "timed out waiting for review"})

    entry = _pending.pop(proposal_id)
    approved = entry["approved"]
    log.info("[%s] Decision: %s", ticker, "APPROVED" if approved else "REJECTED")
    return JSONResponse({"approved": approved, "reason": "manual review"})


# ── User loads this in their browser ──────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def review_ui():
    if not _pending:
        return HTMLResponse(_no_pending_html())

    proposal_id, entry = next(iter(_pending.items()))
    p = entry["proposal"]
    return HTMLResponse(_proposal_html(proposal_id, p))


# ── Browser posts the decision here ───────────────────────────────────────────

@app.post("/decide/{proposal_id}")
async def decide(proposal_id: str, request: Request):
    body = await request.json()
    if proposal_id not in _pending:
        raise HTTPException(status_code=404, detail="Proposal not found or already decided")
    _pending[proposal_id]["approved"] = body.get("approved", False)
    _pending[proposal_id]["event"].set()
    return {"ok": True}


# ── HTML helpers ───────────────────────────────────────────────────────────────

def _proposal_html(proposal_id: str, p: dict) -> str:
    reasoning = "".join(f"<li>{r}</li>" for r in p.get("all_reasoning", []))
    conditions = "".join(f"<li>{c}</li>" for c in p.get("all_invalidating_conditions", []))
    confidence_pct = int(p.get("confidence", 0) * 100)
    action_color = "#2ecc71" if "CALL" in p.get("action", "") else "#e74c3c"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trade Approval — {p.get('ticker')}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #0f0f0f; color: #e0e0e0; padding: 2rem; }}
    h1 {{ font-size: 1.4rem; color: #aaa; margin-bottom: 1.5rem; }}
    .card {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; padding: 1.5rem; max-width: 640px; }}
    .ticker {{ font-size: 2rem; font-weight: 700; color: #fff; }}
    .action {{ display: inline-block; margin-top: 0.4rem; padding: 0.2rem 0.8rem;
               border-radius: 4px; font-weight: 600; color: #fff;
               background: {action_color}; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin: 1.5rem 0; }}
    .stat {{ background: #111; border-radius: 6px; padding: 0.8rem 1rem; }}
    .stat-label {{ font-size: 0.75rem; color: #777; text-transform: uppercase; letter-spacing: 0.05em; }}
    .stat-value {{ font-size: 1.25rem; font-weight: 600; color: #fff; margin-top: 0.2rem; }}
    .confidence-bar {{ height: 6px; background: #2a2a2a; border-radius: 3px; margin-top: 0.5rem; }}
    .confidence-fill {{ height: 100%; border-radius: 3px; background: #f39c12;
                        width: {confidence_pct}%; }}
    h3 {{ font-size: 0.85rem; color: #777; text-transform: uppercase;
           letter-spacing: 0.05em; margin-bottom: 0.5rem; }}
    ul {{ padding-left: 1.2rem; color: #bbb; font-size: 0.9rem; line-height: 1.7; }}
    section {{ margin-top: 1.2rem; }}
    .buttons {{ display: flex; gap: 1rem; margin-top: 2rem; }}
    button {{ flex: 1; padding: 0.9rem; font-size: 1rem; font-weight: 600;
              border: none; border-radius: 6px; cursor: pointer; transition: opacity 0.15s; }}
    button:hover {{ opacity: 0.85; }}
    .approve {{ background: #27ae60; color: #fff; }}
    .reject  {{ background: #c0392b; color: #fff; }}
    .pending {{ display: none; color: #aaa; text-align: center; margin-top: 1rem; font-size: 0.9rem; }}
  </style>
</head>
<body>
  <h1>Pending trade approval</h1>
  <div class="card">
    <div class="ticker">{p.get('ticker', '?')}</div>
    <div class="action">{p.get('action', '?')}</div>

    <div class="grid">
      <div class="stat">
        <div class="stat-label">Strike</div>
        <div class="stat-value">${p.get('strike', '?')}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Expiry</div>
        <div class="stat-value">{p.get('expiry', '?')}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Contracts</div>
        <div class="stat-value">{p.get('suggested_contracts', '?')}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Risk (USD)</div>
        <div class="stat-value">${p.get('risk_usd', 0):.0f}</div>
      </div>
    </div>

    <div class="stat">
      <div class="stat-label">Confidence — {confidence_pct}%</div>
      <div class="confidence-bar"><div class="confidence-fill"></div></div>
    </div>

    <section>
      <h3>Reasoning</h3>
      <ul>{reasoning or "<li>No reasoning provided</li>"}</ul>
    </section>

    <section>
      <h3>Invalidating conditions</h3>
      <ul>{conditions or "<li>None listed</li>"}</ul>
    </section>

    <div class="buttons">
      <button class="approve" onclick="decide(true)">Approve</button>
      <button class="reject"  onclick="decide(false)">Reject</button>
    </div>
    <div class="pending" id="pending">Sending decision…</div>
  </div>

  <script>
    async function decide(approved) {{
      document.querySelector('.buttons').style.display = 'none';
      document.getElementById('pending').style.display = 'block';
      await fetch('/decide/{proposal_id}', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{approved}})
      }});
      document.getElementById('pending').textContent =
        approved ? '✓ Approved — gate is placing the trade.' : '✗ Rejected — gate will skip this trade.';
    }}
  </script>
</body>
</html>"""


def _no_pending_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Approval Webhook</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #0f0f0f; color: #777;
           display: flex; align-items: center; justify-content: center; height: 100vh; }
    p { font-size: 1.1rem; }
  </style>
  <meta http-equiv="refresh" content="3">
</head>
<body><p>No pending proposals — waiting for the gate to send one…</p></body>
</html>"""


if __name__ == "__main__":
    uvicorn.run(app, host="localhost", port=8080, log_level="warning")
