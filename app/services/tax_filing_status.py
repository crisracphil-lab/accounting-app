"""Track filing status per (form_code, year, period_index)."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Optional
from app.db import db, log_action

VALID_STATUSES = ("not_started", "in_process", "filed")


@dataclass
class FilingRecord:
    form_code: str
    year: int
    period_index: int
    status: str
    filed_date: Optional[str]
    reference_number: Optional[str]
    notes: Optional[str]
    company_id: int = 1


def get_status(conn, form_code, year, period_index, company_id: int = 1):
    row = conn.execute(
        "SELECT * FROM tax_filings WHERE company_id = ? AND form_code = ? AND year = ? AND period_index = ?",
        (company_id, form_code, year, period_index)).fetchone()
    if row is None:
        return None
    return FilingRecord(form_code=row["form_code"], year=row["year"],
                        period_index=row["period_index"], status=row["status"],
                        filed_date=row["filed_date"],
                        reference_number=row["reference_number"], notes=row["notes"],
                        company_id=row["company_id"])


def set_status(form_code, year, period_index, status,
               reference_number=None, notes=None, company_id: int = 1):
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status {status!r}; allowed: {VALID_STATUSES}")
    filed_date = date.today().isoformat() if status == "filed" else None
    with db() as conn:
        conn.execute(
            "INSERT INTO tax_filings (company_id, form_code, year, period_index, status, filed_date, "
            "reference_number, notes, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(company_id, form_code, year, period_index) DO UPDATE SET "
            "  status=excluded.status, "
            "  filed_date=COALESCE(excluded.filed_date, tax_filings.filed_date), "
            "  reference_number=COALESCE(excluded.reference_number, tax_filings.reference_number), "
            "  notes=COALESCE(excluded.notes, tax_filings.notes), "
            "  updated_at=datetime('now')",
            (company_id, form_code, year, period_index, status, filed_date, reference_number, notes))
        log_action(conn, "set_filing_status", "tax_filing", None,
                   {"company_id": company_id, "form_code": form_code, "year": year,
                    "period_index": period_index, "status": status, "filed_date": filed_date})


def get_all_for_period(conn, year, period_index, company_id: int = 1):
    rows = conn.execute(
        "SELECT * FROM tax_filings WHERE company_id = ? AND year = ? AND period_index = ?",
        (company_id, year, period_index)).fetchall()
    out = {}
    for r in rows:
        out[r["form_code"]] = FilingRecord(
            form_code=r["form_code"], year=r["year"], period_index=r["period_index"],
            status=r["status"], filed_date=r["filed_date"],
            reference_number=r["reference_number"], notes=r["notes"],
            company_id=r["company_id"])
    return out
