"""app/routers/invoices.py — Invoice management routes."""
from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.db import db
from app.deps import templates
from app.parsers.invoice_upload import InvoiceUploadParseError
from app.services.invoice_matching import ingest_invoice_upload, match_invoices_and_payments

router = APIRouter()


@router.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, status: str = "", message: str = "") -> Response:
    with db() as conn:
        where = "WHERE status = ?" if status else ""
        params = (status,) if status else ()
        invoices = conn.execute(
            f"""SELECT i.*, je.status AS je_status
                FROM invoices i
                LEFT JOIN journal_entries je ON je.id = i.matched_journal_entry_id
                {where}
                ORDER BY i.id DESC LIMIT 300""",
            params,
        ).fetchall()
        uploads = conn.execute(
            "SELECT * FROM invoice_uploads ORDER BY id DESC LIMIT 20"
        ).fetchall()
        advance_rows = conn.execute(
            """SELECT im.*, bt.transaction_date AS bank_date, bt.description AS bank_description, bt.net_amount AS bank_amount,
                      pi.transaction_date AS payment_date, pi.beneficiary_name, pi.amount AS payment_amount,
                      je.status AS je_status
               FROM invoice_matches im
               LEFT JOIN bank_transactions bt ON bt.id = im.bank_transaction_id
               LEFT JOIN payment_instructions pi ON pi.id = im.payment_instruction_id
               LEFT JOIN journal_entries je ON je.id = im.journal_entry_id
               WHERE im.match_type = 'advance_payment'
               ORDER BY im.id DESC LIMIT 100"""
        ).fetchall()
        stats = {
            "unmatched": conn.execute("SELECT COUNT(*) AS n FROM invoices WHERE status='unmatched'").fetchone()["n"],
            "matched": conn.execute("SELECT COUNT(*) AS n FROM invoices WHERE status='matched_payment'").fetchone()["n"],
            "accrued": conn.execute("SELECT COUNT(*) AS n FROM invoices WHERE status='accrued_expense'").fetchone()["n"],
            "advances": conn.execute("SELECT COUNT(*) AS n FROM invoice_matches WHERE match_type='advance_payment'").fetchone()["n"],
        }
    return templates.TemplateResponse(request, "invoices.html", {
        "invoices": invoices, "uploads": uploads, "advance_rows": advance_rows,
        "stats": stats, "status": status, "message": message or None,
    })


@router.post("/invoices/upload", response_class=HTMLResponse)
async def invoices_upload(request: Request, file: UploadFile = File(...)) -> Response:
    if not file.filename.lower().endswith((".xlsx", ".xlsm", ".xls", ".csv")):
        raise HTTPException(status_code=422, detail="Invoice upload supports .xlsx and .xlsm files only.")
    tmp = Path(tempfile.mkdtemp()) / Path(file.filename).name
    try:
        with tmp.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        try:
            result = ingest_invoice_upload(tmp)
        except InvoiceUploadParseError as exc:
            raise HTTPException(status_code=422, detail=f"Invoice parse error: {exc}") from exc
        except Exception as exc:
            if "UNIQUE constraint failed: invoice_uploads.sha256" in str(exc):
                raise HTTPException(status_code=409, detail="This invoice file was already uploaded.") from exc
            raise
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    return RedirectResponse(f"/invoices?message=Uploaded%20{result['parsed_count']}%20invoice%20rows", status_code=303)


@router.post("/invoices/match")
def invoices_match() -> Response:
    result = match_invoices_and_payments()
    msg = f"Matched {result['matched_payment']}, accrued {result['accrued_expense']}, advances {result['advance_payment']}"
    return RedirectResponse("/invoices?message=" + msg.replace(" ", "%20"), status_code=303)


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(request: Request, invoice_id: int) -> Response:
    with db() as conn:
        invoice = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if invoice is None:
            raise HTTPException(404, "Invoice not found")
        matches = conn.execute(
            """SELECT im.*, bt.transaction_date AS bank_date, bt.description AS bank_description, bt.net_amount AS bank_amount,
                      pi.transaction_date AS payment_date, pi.beneficiary_name, pi.amount AS payment_amount
               FROM invoice_matches im
               LEFT JOIN bank_transactions bt ON bt.id = im.bank_transaction_id
               LEFT JOIN payment_instructions pi ON pi.id = im.payment_instruction_id
               WHERE im.invoice_id = ? ORDER BY im.id DESC""", (invoice_id,)
        ).fetchall()
        je = None
        lines = []
        if invoice["matched_journal_entry_id"]:
            je = conn.execute("SELECT * FROM journal_entries WHERE id = ?", (invoice["matched_journal_entry_id"],)).fetchone()
            lines = conn.execute(
                """SELECT l.*, a.code AS account_code, a.name AS account_name
                   FROM journal_entry_lines l JOIN chart_of_accounts a ON a.id = l.account_id
                   WHERE l.journal_entry_id = ? ORDER BY l.line_order""", (invoice["matched_journal_entry_id"],)
            ).fetchall()
    return templates.TemplateResponse(request, "invoice_detail.html", {
        "invoice": invoice, "matches": matches, "je": je, "lines": lines,
    })
