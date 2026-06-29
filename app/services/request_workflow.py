"""app/services/request_workflow.py

Business logic for payment-request workflow transitions.

Functions here receive an already-open SQLite connection (and the relevant
payment_request row as sqlite3.Row) so they can be called from any router
without duplicating the DB boilerplate.  They are also easily unit-testable
because they accept plain data rather than FastAPI Request objects.
"""
from __future__ import annotations

import logging

from app.db import log_action

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auto-generate a draft Journal Entry when a payment request is approved
# ---------------------------------------------------------------------------

def auto_create_draft_je(conn, pr) -> int | None:
    """Attempt to create a draft JE linked to *pr* using historical patterns.

    Returns the new journal_entry.id on success, or None if skipped/failed.
    The function is intentionally best-effort: any exception is logged and
    swallowed so it never blocks the status-update transaction.

    Args:
        conn:  Open sqlite3 connection (must already be inside a transaction
               that the caller will commit).
        pr:    sqlite3.Row (or dict) for the payment_request being approved.
    """
    request_id = pr["id"]

    # Idempotency guard — only create one JE per request.
    existing = conn.execute(
        "SELECT id FROM journal_entries WHERE payment_request_id = ? LIMIT 1",
        (request_id,),
    ).fetchone()
    if existing:
        return None

    try:
        from app.services.historical_journal_learning import match_all_historical_patterns

        company_id_int = _resolve_company_id(conn, pr["company_id"])
        description = (pr["description"] or pr["payee_name"] or "").strip()

        matched = match_all_historical_patterns(
            conn,
            description=description,
            company_id=company_id_int,
        )
        if not matched:
            return None

        # Insert the draft JE header
        je_cur = conn.execute(
            """INSERT INTO journal_entries
               (company_id, entry_date, description, status, payment_request_id)
               VALUES (?, date('now'), ?, 'draft', ?)""",
            (
                company_id_int,
                f"Auto: {description[:200]}" if description else f"Auto JE for request #{request_id}",
                request_id,
            ),
        )
        je_id = je_cur.lastrowid

        # Insert one line per matched account
        amount_str = str(pr["amount"] or "0")
        for order, m in enumerate(matched, start=1):
            acct_row = conn.execute(
                "SELECT id FROM chart_of_accounts WHERE code = ? LIMIT 1",
                (m["account_code"],),
            ).fetchone()
            if acct_row is None:
                continue
            is_debit = m.get("normal_side", "debit") == "debit"
            conn.execute(
                """INSERT INTO journal_entry_lines
                   (journal_entry_id, line_order, account_id, debit, credit, description)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    je_id,
                    order,
                    acct_row["id"],
                    amount_str if is_debit else "0",
                    "0" if is_debit else amount_str,
                    m.get("description") or description,
                ),
            )

        log_action(
            conn,
            "auto_create",
            "journal_entry",
            je_id,
            {"source": "payment_request", "payment_request_id": request_id},
            user_id="system",
        )
        return je_id

    except Exception:
        logger.exception(
            "auto_create_draft_je failed for payment_request #%s — JE skipped",
            request_id,
        )
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_company_id(conn, value) -> int:
    """Return a valid company_id integer from a raw DB value.

    Falls back to 1 (the default company) if the value is None or zero.
    """
    if value:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    # Use the first company in the DB as the fallback
    row = conn.execute("SELECT id FROM companies ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else 1
