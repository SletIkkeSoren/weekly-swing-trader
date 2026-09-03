"""
Builds the end-of-day Discord summary from today's approved trades, what those
approvals actually did at the broker, and the account's rolling per-ticker
track record.

Read-only visibility layer — never feeds back into the consensus prompt (see
CLAUDE.md core rules: models never recall market facts or P&L). It surfaces
the exact same TickerStats numbers driving gate.filters.dynamic_confidence_threshold,
so the report is a window into the learning layer, not a separate signal.

The report reads the executions audit trail, not just approvals. Reporting
approvals alone is how three days of 422-rejected orders (2026-08-31 → 09-02)
were still summarised every evening as trades taken.
"""

from datetime import date, timedelta

from audit.performance import TickerStats
from gate.filters import dynamic_confidence_threshold

_ACTION_EMOJI = {"BUY_CALL": "📈", "BUY_PUT": "📉", "CLOSE": "🔄", "HOLD": "⏸️"}
_STATUS_EMOJI = {
    "filled": "✅", "submitted": "📨", "skipped": "⏭️", "error": "❌", "dry_run": "🧪",
}
_REACHED_MARKET = ("filled", "submitted")
_TREND_MIN_TRADES = 3   # only surface a per-ticker adjustment once it's meaningful
_FIELD_LIMIT = 1000     # Discord caps an embed field value at 1024 chars


def _week_bucket(trades: list, today: date, weeks_ago: int) -> list:
    """weeks_ago=0 -> the 7 days ending today; weeks_ago=1 -> the 7 days before that."""
    start = today - timedelta(days=7 * (weeks_ago + 1) - 1)
    end = today - timedelta(days=7 * weeks_ago)
    return [t for t in trades if start <= date.fromisoformat(t.closed_at[:10]) <= end]


def _win_rate(trades: list) -> float | None:
    return sum(t.win for t in trades) / len(trades) if trades else None


def _clip(lines: list[str]) -> str:
    out = "\n".join(lines)
    return out if len(out) <= _FIELD_LIMIT else out[:_FIELD_LIMIT - 1] + "…"


def _execution_line(ex: dict) -> str:
    emoji = _STATUS_EMOJI.get(ex.get("status"), "•")
    price = ex.get("filled_avg_price")
    price_str = f" @ ${float(price):.2f}" if price is not None else ""
    line = f"{emoji} {ex.get('ticker')} {ex.get('action')} ×{ex.get('contracts', 0)}{price_str}"
    reason = (ex.get("reason") or "").strip()
    if reason and ex.get("status") in ("error", "skipped"):
        line += f" — {reason[:120]}"
    return line


def build_report(
    ticker_stats: dict[str, TickerStats],
    approved_today: list[dict],
    executions_today: list[dict],
    today: date,
    base_min_confidence: float,
) -> dict:
    today_str = today.isoformat()
    fields = []

    if approved_today:
        for record in approved_today:
            p = record["proposal"]
            emoji = _ACTION_EMOJI.get(p["action"], "•")
            thesis = p["all_reasoning"][0] if p["all_reasoning"] else "—"
            fields.append({
                "name": f"{emoji} {p['ticker']} — {p['action']}",
                "value": f"confidence {p['confidence']:.0%} | {thesis[:200]}",
                "inline": False,
            })
    else:
        fields.append({"name": "Trades today", "value": "No approved trades.", "inline": False})

    # ── What the broker actually did with those approvals ─────────────────
    reached_market = [e for e in executions_today if e.get("status") in _REACHED_MARKET]
    failed = [e for e in executions_today if e.get("status") in ("error", "skipped")]

    if executions_today:
        fields.append({
            "name": f"Orders at the broker ({len(reached_market)}/{len(executions_today)} reached the market)",
            "value": _clip([_execution_line(e) for e in executions_today]),
            "inline": False,
        })
    elif approved_today:
        fields.append({
            "name": "⚠️ Orders at the broker",
            "value": (
                f"{len(approved_today)} trade(s) approved but the executor recorded "
                f"nothing today — check the executor CronJob."
            ),
            "inline": False,
        })

    if failed:
        fields.append({
            "name": f"⚠️ {len(failed)} order(s) never reached the market",
            "value": _clip([_execution_line(e) for e in failed]),
            "inline": False,
        })

    closes_today = [
        t for stats in ticker_stats.values() for t in stats.trades
        if t.closed_at[:10] == today_str
    ]
    if closes_today:
        lines = [f"{'🟢' if t.win else '🔴'} {t.ticker}: {t.return_pct:+.0%}" for t in closes_today]
        fields.append({"name": "Closed today", "value": _clip(lines), "inline": False})

    adjusted = {
        ticker: dynamic_confidence_threshold(base_min_confidence, stats)
        for ticker, stats in ticker_stats.items()
        if stats.n >= _TREND_MIN_TRADES
    }
    moved = {t: thr for t, thr in adjusted.items() if abs(thr - base_min_confidence) >= 0.01}
    if moved:
        lines = [
            f"{ticker}: {base_min_confidence:.2f} → {thr:.2f}"
            for ticker, thr in sorted(moved.items(), key=lambda kv: -kv[1])
        ]
        fields.append({"name": "Adjusted entry bar (track record)", "value": _clip(lines), "inline": False})

    all_trades = [t for stats in ticker_stats.values() for t in stats.trades]
    this_week = _week_bucket(all_trades, today, 0)
    last_week = _week_bucket(all_trades, today, 1)
    this_wr, last_wr = _win_rate(this_week), _win_rate(last_week)
    if this_wr is not None or last_wr is not None:
        this_str = f"{this_wr:.0%} ({len(this_week)})" if this_wr is not None else "no trades"
        last_str = f"{last_wr:.0%} ({len(last_week)})" if last_wr is not None else "no trades"
        fields.append({
            "name": "Win rate — this week vs last",
            "value": f"{this_str} vs {last_str}",
            "inline": False,
        })

    return {
        "embeds": [{
            "title": f"\U0001F4CA End-of-day report — {today_str}",
            "color": 0xE74C3C if failed else 0x3498DB,
            "fields": fields,
            "footer": {"text": "paper trading — models stay blind, this is a human-facing summary only"},
        }]
    }
