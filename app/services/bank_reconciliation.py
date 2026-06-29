from __future__ import annotations

from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path

from app.db import db, log_action
from app.parsers.bank_statement_generic import GenericTransaction, parse_generic_statement


class ReconciliationError(ValueError):
    pass


def _sim(a, b):
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _money(v):
    return Decimal(str(v or "0"))


def _date_diff(a: str, b: str) -> int:
    if not a or not b:
        return 0  # treat missing dates as no gap
    from datetime import date
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def reconcile_files(left_path: str | Path, right_path: str | Path, reconciliation_type: str = "general_file_reconciliation") -> int:
    allowed_types = {
        "general_file_reconciliation",
        "bank_vs_system",
        "system_vs_raw",
        "bank_vs_raw",
        "left_vs_right",
    }
    if reconciliation_type not in allowed_types:
        raise ReconciliationError("Invalid reconciliation type")
    stored_reconciliation_type = reconciliation_type
    if stored_reconciliation_type in {"general_file_reconciliation", "left_vs_right"}:
        # Backward-compatible storage value for existing local SQLite files that
        # were created before the generic workspace existed. The UI labels this
        # as a general two-file reconciliation.
        stored_reconciliation_type = "system_vs_raw"
    bank_rows = parse_generic_statement(left_path)
    system_rows = parse_generic_statement(right_path)
    used_system: set[int] = set()
    items = []
    for b in bank_rows:
        candidates = []
        for idx, s in enumerate(system_rows):
            if idx in used_system:
                continue
            amount_diff = abs(b.amount - s.amount)
            days = _date_diff(b.date, s.date)
            text = max(_sim(b.reference, s.reference), _sim(b.description, s.description))
            if amount_diff <= Decimal("0.05") and days <= 3:
                status = "matched"
                score = 1.0 + text
            elif amount_diff <= Decimal("0.05") and days <= 30:
                status = "date_difference"
                score = 0.75 + text
            elif amount_diff <= Decimal("100.00") and days <= 3:
                status = "amount_difference"
                score = 0.70 + text
            elif text >= 0.72 and days <= 7:
                status = "possible_duplicate"
                score = 0.50 + text
            else:
                continue
            candidates.append((score, idx, s, status, amount_diff, days, text))
        if candidates:
            candidates.sort(reverse=True, key=lambda x: x[0])
            _, idx, s, status, amount_diff, days, text = candidates[0]
            used_system.add(idx)
            items.append({"status": status, "bank": b, "system": s, "diff": amount_diff, "reason": f"amount diff {amount_diff}; date gap {days}; text score {text:.2f}"})
        else:
            items.append({"status": "bank_only", "bank": b, "system": None, "diff": abs(b.amount), "reason": "No matching File B row found"})
    for idx, s in enumerate(system_rows):
        if idx not in used_system:
            items.append({"status": "system_only", "bank": None, "system": s, "diff": abs(s.amount), "reason": "No matching File A row found"})
    with db() as conn:
        conn.execute("BEGIN")
        try:
            cur = conn.execute(
                "INSERT INTO bank_reconciliation_runs (bank_filename, system_filename, reconciliation_type) VALUES (?, ?, ?)",
                (Path(left_path).name, Path(right_path).name, stored_reconciliation_type),
            )
            run_id = cur.lastrowid
            for it in items:
                b = it["bank"]
                s = it["system"]
                conn.execute(
                    """INSERT INTO bank_reconciliation_items
                       (run_id, status, bank_date, bank_reference, bank_description, bank_amount,
                        system_date, system_reference, system_description, system_amount, amount_difference, reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, it["status"], b.date if b else None, b.reference if b else None, b.description if b else None, str(b.amount) if b else None,
                     s.date if s else None, s.reference if s else None, s.description if s else None, str(s.amount) if s else None,
                     str(it["diff"]), it["reason"]),
                )
            log_action(conn, "reconciliation_workspace", "reconciliation_run", run_id, {"items": len(items), "type": reconciliation_type})
            conn.execute("COMMIT")
            return run_id
        except Exception:
            conn.execute("ROLLBACK")
            raise

# Backward-compatible name for older tests/imports.
BankReconciliationError = ReconciliationError
