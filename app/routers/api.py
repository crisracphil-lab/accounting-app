"""app/routers/api.py — JSON API endpoints (/api/*)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.db import db
from app.deps import require_accounting_calendar_access

router = APIRouter()


@router.get("/api/health")
def api_health(request: Request) -> Response:
    """Real readiness check: verifies SQLite is reachable and schema exists."""
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        tables = conn.execute("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'").fetchone()["n"]
    return {"status": "ok", "database": "reachable", "tables": tables, "users": users}


@router.get("/api/dashboard/stats")
def api_dashboard_stats(request: Request) -> Response:
    """Return live dashboard counts from SQLite; no placeholder or fallback values."""
    with db() as conn:
        return {
            "uploads": conn.execute("SELECT COUNT(*) AS n FROM uploaded_files").fetchone()["n"],
            "transactions": conn.execute("SELECT COUNT(*) AS n FROM bank_transactions").fetchone()["n"],
            "draft_journal_entries": conn.execute("SELECT COUNT(*) AS n FROM journal_entries WHERE status='draft'").fetchone()["n"],
            "approved_journal_entries": conn.execute("SELECT COUNT(*) AS n FROM journal_entries WHERE status='approved'").fetchone()["n"],
            "payment_instructions": conn.execute("SELECT COUNT(*) AS n FROM payment_instructions").fetchone()["n"],
            "closing_runs": conn.execute("SELECT COUNT(*) AS n FROM closing_runs").fetchone()["n"],
        }


@router.get("/api/calendar/events")
def api_calendar_events(request: Request, visibility: str = "visible") -> Response:
    user = require_accounting_calendar_access(request)
    if visibility == "shared":
        where = "visibility = 'shared'"
        params = []
    elif visibility == "private":
        where = "visibility = 'private' AND owner_user_id = ?"
        params = [user["id"]]
    else:
        where = "visibility = 'shared' OR owner_user_id = ?"
        params = [user["id"]]
    with db() as conn:
        rows = conn.execute(
            f"""SELECT id, title, event_date, start_time, end_time, description, location,
                       visibility, owner_user_id, reminder_minutes, is_done
                FROM calendar_events
                WHERE {where}
                ORDER BY event_date, COALESCE(start_time, '00:00'), id""",
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]
