"""app/routers/banking.py — File upload, transactions, payments, and journal-basis routes."""
from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
import uuid as _uuid
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.db import db
from app.deps import (
    ALL_PAYMENT_STATUSES,
    ALL_REMITTANCE_TYPES,
    BASE_DIR,
    _current_user,
    _parse_flexible_journal_basis,
    _payments_query,
    templates,
)
from app.parsers.bank_statement_generic import GenericStatementParseError
from app.parsers.ra260_payments import RA260ParseError
from app.parsers.subsidiary_ledger import parse_subsidiary_ledger
from app.services.file_upload import DuplicateUploadError, ingest_statement
from app.services.historical_journal_learning import (
    ensure_learning_tables,
    learn_from_historical_journal,
)
from app.services.payment_je_generator import generate_jes_for_payments
from app.services.ra260_ingest import DuplicatePaymentsUploadError, ingest_ra260

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Upload ────────────────────────────────────────────────────────────────────

def _upload_ctx(request: Request, *, error=None, result=None) -> dict:
    """Build template context for upload.html, scoping company to the logged-in user."""
    user = request.state.user
    is_admin = (user["role"] if user else "") == "admin"
    with db() as conn:
        companies = conn.execute(
            "SELECT id, name FROM companies WHERE is_active = 1 ORDER BY id"
        ).fetchall()
    _cids = [c["id"] for c in companies]
    user_company_id = int(user["company_id"]) if (user and user["company_id"]) else (_cids[0] if _cids else 0)
    user_company_name = next(
        (c["name"] for c in companies if c["id"] == user_company_id),
        "Unknown",
    )
    return {
        "error": error,
        "result": result,
        "companies": companies,
        "is_admin": is_admin,
        "user_company_id": user_company_id,
        "user_company_name": user_company_name,
    }


@router.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request) -> Response:
    return templates.TemplateResponse(request, "upload.html", _upload_ctx(request))


@router.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, file: UploadFile = File(...), company_id: int = Form(0)) -> Response:
    if not company_id:
        ctx = _upload_ctx(request)
        company_id = ctx["user_company_id"]
    if not file.filename.lower().endswith((".xlsx", ".xlsm", ".xls", ".csv")):
        return templates.TemplateResponse(request, "upload.html", _upload_ctx(
            request, error="Please upload a supported bank statement file: .xlsx, .xlsm, .xls, or .csv."
        ))

    tmp = Path(tempfile.mkdtemp()) / Path(file.filename).name
    with tmp.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    try:
        result = ingest_statement(tmp, company_id=company_id)
    except DuplicateUploadError as exc:
        return templates.TemplateResponse(request, "upload.html", _upload_ctx(
            request, error=f"Duplicate upload: {exc}"))
    except GenericStatementParseError as exc:
        return templates.TemplateResponse(request, "upload.html", _upload_ctx(
            request, error=f"Bank statement parse error: {exc}"))
    except Exception as exc:
        return templates.TemplateResponse(request, "upload.html", _upload_ctx(
            request, error=f"Upload failed: {exc}"))
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)

    return templates.TemplateResponse(request, "upload.html", _upload_ctx(request, result=result))


@router.post("/upload/learn-journal", response_class=HTMLResponse)
async def learn_journal_entries(request: Request, ledger_file: UploadFile = File(...), company_id: int = Form(0)) -> Response:
    if not company_id:
        ctx = _upload_ctx(request)
        company_id = ctx["user_company_id"]
    allowed = (".xlsx", ".xlsm", ".xls", ".csv")
    if not ledger_file.filename.lower().endswith(allowed):
        return templates.TemplateResponse(request, "upload.html", _upload_ctx(
            request, error="Please upload a journal entry/subsidiary ledger file as .xlsx, .xlsm, .xls, or .csv."
        ))
    safe_name = Path(ledger_file.filename).name
    tmp = Path(tempfile.mkdtemp()) / safe_name
    try:
        with tmp.open("wb") as out:
            shutil.copyfileobj(ledger_file.file, out)

        basis_dir = BASE_DIR.parent / "data" / "journal_learning" / str(company_id)
        basis_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{_uuid.uuid4().hex}_{safe_name}"
        stored_path = basis_dir / stored_name
        shutil.copy2(tmp, stored_path)

        try:
            ledger = parse_subsidiary_ledger(tmp)
        except Exception:
            ledger = _parse_flexible_journal_basis(tmp)

        suggestions = []
        with db() as conn:
            learned_lines = learn_from_historical_journal(
                conn, path=tmp, company_id=company_id,
                filename=ledger_file.filename, stored_path=str(stored_path),
            )
            conn.execute("""CREATE TABLE IF NOT EXISTS journal_learning_patterns_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL DEFAULT 1,
                account_code TEXT NOT NULL,
                account_title TEXT,
                normal_side TEXT NOT NULL,
                last_debit TEXT NOT NULL DEFAULT '0',
                last_credit TEXT NOT NULL DEFAULT '0',
                learned_from_filename TEXT,
                stored_path TEXT,
                last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(company_id, account_code)
            )""")
            for line in learned_lines:
                suggestions.append({
                    "code": line.account_code,
                    "title": line.account_title,
                    "rows": 1,
                    "debit": line.amount if line.normal_side == "debit" else Decimal("0"),
                    "credit": line.amount if line.normal_side == "credit" else Decimal("0"),
                    "basis": f"Future matching keywords: {line.keywords}",
                    "historical_basis": f"Learned from historical JE description: {line.description[:120]}",
                })
                conn.execute(
                    """INSERT INTO journal_learning_patterns_v2
                        (company_id, account_code, account_title, normal_side, last_debit, last_credit, learned_from_filename, stored_path, last_seen_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        ON CONFLICT(company_id, account_code) DO UPDATE SET
                          account_title=excluded.account_title, normal_side=excluded.normal_side,
                          last_debit=excluded.last_debit, last_credit=excluded.last_credit,
                          learned_from_filename=excluded.learned_from_filename, stored_path=excluded.stored_path,
                          last_seen_at=datetime('now')""",
                    (company_id, line.account_code, line.account_title, line.normal_side,
                     str(line.amount if line.normal_side == "debit" else Decimal("0")),
                     str(line.amount if line.normal_side == "credit" else Decimal("0")),
                     ledger_file.filename, str(stored_path)),
                )
        return templates.TemplateResponse(request, "journal_learn_result.html", {
            "ledger": ledger, "suggestions": suggestions[:200],
            "filename": ledger_file.filename, "company_id": company_id,
        })
    except Exception as exc:
        return templates.TemplateResponse(request, "upload.html", _upload_ctx(
            request, error=f"Could not learn journal entries from file: {exc}"
        ))
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            tmp.parent.rmdir()


# ── Journal Basis tab ─────────────────────────────────────────────────────────

@router.get("/journal-basis", response_class=HTMLResponse)
def journal_basis_page(request: Request, message: str = "", error: str = "") -> Response:
    """Dedicated page for uploading historical journal entry basis files and
    reviewing what the system has learned per company."""
    user = _current_user(request)
    is_admin = user["role"] == "admin"
    user_cid = int(user["company_id"] or 1)
    with db() as conn:
        ensure_learning_tables(conn)
        all_companies = conn.execute(
            "SELECT id, name FROM companies WHERE is_active = 1 ORDER BY id"
        ).fetchall()
        # Non-admin accountants only see their own company
        companies = all_companies if is_admin else [c for c in all_companies if c["id"] == user_cid]
        if is_admin:
            basis_files = conn.execute(
                """SELECT f.*, c.name AS company_name
                   FROM journal_learning_basis_files f
                   LEFT JOIN companies c ON c.id = f.company_id
                   ORDER BY f.uploaded_at DESC LIMIT 100"""
            ).fetchall()
            pattern_counts = {
                row["company_id"]: row["n"]
                for row in conn.execute(
                    "SELECT company_id, COUNT(*) AS n FROM journal_learning_description_patterns GROUP BY company_id"
                ).fetchall()
            }
            template_counts = {
                row["company_id"]: row["n"]
                for row in conn.execute(
                    "SELECT company_id, COUNT(DISTINCT group_key) AS n FROM journal_learning_entry_templates GROUP BY company_id"
                ).fetchall()
            }
        else:
            basis_files = conn.execute(
                """SELECT f.*, c.name AS company_name
                   FROM journal_learning_basis_files f
                   LEFT JOIN companies c ON c.id = f.company_id
                   WHERE f.company_id = ?
                   ORDER BY f.uploaded_at DESC LIMIT 100""",
                (user_cid,)
            ).fetchall()
            pattern_counts = {
                row["company_id"]: row["n"]
                for row in conn.execute(
                    "SELECT company_id, COUNT(*) AS n FROM journal_learning_description_patterns WHERE company_id = ? GROUP BY company_id",
                    (user_cid,)
                ).fetchall()
            }
            template_counts = {
                row["company_id"]: row["n"]
                for row in conn.execute(
                    "SELECT company_id, COUNT(DISTINCT group_key) AS n FROM journal_learning_entry_templates WHERE company_id = ? GROUP BY company_id",
                    (user_cid,)
                ).fetchall()
            }
    return templates.TemplateResponse(request, "journal_basis.html", {
        "message": message,
        "error": error,
        "companies": companies,
        "basis_files": basis_files,
        "pattern_counts": pattern_counts,
        "template_counts": template_counts,
        "active_company_id": None,
        "entry_templates": [],
    })


@router.post("/journal-basis/upload", response_class=HTMLResponse)
async def journal_basis_upload(
    request: Request,
    ledger_file: UploadFile = File(...),
    company_id: int = Form(0),
) -> Response:
    """Upload a historical journal entry / subsidiary ledger file as the
    learning basis for a company.  Accepted: .xlsx, .xlsm, .xls, .csv."""
    user = _current_user(request)
    is_admin = user["role"] == "admin"
    user_cid = int(user["company_id"] or 1)
    # Non-admin users are always scoped to their own company
    if not is_admin or not company_id:
        company_id = user_cid
    allowed = (".xlsx", ".xlsm", ".xls", ".csv")
    if not ledger_file.filename.lower().endswith(allowed):
        return RedirectResponse(
            "/journal-basis?error=Please+upload+a+journal+file+as+.xlsx+.xlsm+.xls+or+.csv",
            status_code=303,
        )

    safe_name = Path(ledger_file.filename).name
    tmp = Path(tempfile.mkdtemp()) / safe_name
    try:
        with tmp.open("wb") as out:
            shutil.copyfileobj(ledger_file.file, out)

        basis_dir = BASE_DIR.parent / "data" / "journal_learning" / str(company_id)
        basis_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{_uuid.uuid4().hex}_{safe_name}"
        stored_path = basis_dir / stored_name
        shutil.copy2(tmp, stored_path)

        with db() as conn:
            lines = learn_from_historical_journal(
                conn,
                path=tmp,
                company_id=company_id,
                filename=ledger_file.filename,
                stored_path=str(stored_path),
            )

        msg = (f"Learned {len(lines)} journal lines from {ledger_file.filename}. "
               f"Future bank and receipt uploads will use these patterns.")
        return RedirectResponse(
            f"/journal-basis?message={msg.replace(' ', '+')}",
            status_code=303,
        )

    except ValueError as exc:
        return RedirectResponse(
            f"/journal-basis?error={str(exc).replace(' ', '+')}",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            f"/journal-basis?error=Upload+failed:+{str(exc)[:200].replace(' ', '+')}",
            status_code=303,
        )
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            tmp.parent.rmdir()


@router.get("/journal-basis/patterns", response_class=HTMLResponse)
def journal_basis_patterns(request: Request, company_id: int = 0) -> Response:
    """Show all learned patterns for a company so the accountant can review
    what will be suggested for future uploads."""
    user = _current_user(request)
    is_admin = user["role"] == "admin"
    user_cid = int(user["company_id"] or 1)
    # Non-admin users are always scoped to their own company
    if not is_admin:
        company_id = user_cid
    with db() as conn:
        ensure_learning_tables(conn)
        all_companies = conn.execute(
            "SELECT id, name FROM companies WHERE is_active = 1 ORDER BY id"
        ).fetchall()
        companies = all_companies if is_admin else [c for c in all_companies if c["id"] == user_cid]
        if not company_id and companies:
            company_id = companies[0]["id"]
        patterns = conn.execute(
            """SELECT p.*, c.name AS company_name
               FROM journal_learning_description_patterns p
               LEFT JOIN companies c ON c.id = p.company_id
               WHERE p.company_id = ?
               ORDER BY p.times_seen DESC, p.last_seen_at DESC
               LIMIT 500""",
            (company_id,),
        ).fetchall()
        templates_q = conn.execute(
            """SELECT t.*, c.name AS company_name
               FROM journal_learning_entry_templates t
               LEFT JOIN companies c ON c.id = t.company_id
               WHERE t.company_id = ?
               ORDER BY t.times_seen DESC, t.last_seen_at DESC
               LIMIT 500""",
            (company_id,),
        ).fetchall()
    return templates.TemplateResponse(request, "journal_basis.html", {
        "message": "",
        "error": "",
        "companies": companies,
        "basis_files": [],
        "pattern_counts": {company_id: len(patterns)},
        "template_counts": {company_id: len({r["group_key"] for r in templates_q})},
        "patterns": patterns,
        "entry_templates": templates_q,
        "active_company_id": company_id,
    })


# ── Transactions ──────────────────────────────────────────────────────────────

@router.get("/transactions", response_class=HTMLResponse)
def list_transactions(request: Request, status: str = "", supplier_id: int = 0,
                      q: str = "", limit: int = 200) -> Response:
    # Build WHERE clause with a fixed base so no f-string ever receives user
    # text.  Filter values (status, supplier_id, search query) always travel
    # through ? placeholders in params; only literal SQL fragments are
    # concatenated into where_sql.
    where_sql = "WHERE 1=1"
    params: list = []
    if status:
        where_sql += " AND bt.status = ?"
        params.append(status)
    if supplier_id:
        where_sql += " AND bt.supplier_id = ?"
        params.append(supplier_id)
    if q:
        where_sql += " AND (bt.description LIKE ? OR bt.remarks LIKE ? OR bt.counterparty_name LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])

    with db() as conn:
        rows = conn.execute(
            "SELECT bt.*, s.name AS supplier_name,"
            "       je.id AS je_id, je.status AS je_status"
            " FROM bank_transactions bt"
            " LEFT JOIN suppliers s ON s.id = bt.supplier_id"
            " LEFT JOIN journal_entries je ON je.id = bt.journal_entry_id"
            " " + where_sql +
            " ORDER BY bt.transaction_date DESC, bt.id DESC"
            " LIMIT ?",
            (*params, limit),
        ).fetchall()
        suppliers = conn.execute(
            "SELECT id, name FROM suppliers WHERE is_active = 1 ORDER BY name"
        ).fetchall()
    return templates.TemplateResponse(request, "transactions.html", {
        "rows": rows, "suppliers": suppliers, "status": status,
        "supplier_id": supplier_id, "q": q,
    })


@router.get("/transactions/{tx_id}", response_class=HTMLResponse)
def transaction_detail(request: Request, tx_id: int) -> Response:
    with db() as conn:
        tx = conn.execute(
            """SELECT bt.*, s.name AS supplier_name, uf.filename AS source_file
               FROM bank_transactions bt
               LEFT JOIN suppliers s ON s.id = bt.supplier_id
               LEFT JOIN uploaded_files uf ON uf.id = bt.uploaded_file_id
               WHERE bt.id = ?""",
            (tx_id,),
        ).fetchone()
        if tx is None:
            raise HTTPException(404, f"Transaction {tx_id} not found")
        je = None
        je_lines: list = []
        if tx["journal_entry_id"]:
            je = conn.execute(
                "SELECT * FROM journal_entries WHERE id = ?", (tx["journal_entry_id"],)
            ).fetchone()
            je_lines = conn.execute(
                """SELECT l.*, a.code AS account_code, a.name AS account_name
                   FROM journal_entry_lines l
                   JOIN chart_of_accounts a ON a.id = l.account_id
                   WHERE l.journal_entry_id = ?
                   ORDER BY l.line_order""",
                (tx["journal_entry_id"],),
            ).fetchall()
    return templates.TemplateResponse(request, "transaction_detail.html", {
        "tx": tx, "je": je, "je_lines": je_lines,
    })


# ── Payments register ─────────────────────────────────────────────────────────

@router.get("/payments", response_class=HTMLResponse)
def payments_page(request: Request, status: str = "", remittance_type: str = "", q: str = "") -> Response:
    with db() as conn:
        rows = _payments_query(conn, status=status, remittance_type=remittance_type, q=q)
    total = sum(float(r["amount"] or 0) for r in rows)
    return templates.TemplateResponse(request, "payments.html", {
        "rows": rows, "status": status, "remittance_type": remittance_type, "q": q,
        "all_statuses": ALL_PAYMENT_STATUSES, "all_types": ALL_REMITTANCE_TYPES,
        "total_amount": total, "error": None, "upload_result": None,
    })


@router.post("/payments/upload", response_class=HTMLResponse)
async def payments_upload(request: Request, file: UploadFile = File(...)) -> Response:
    if not file.filename.lower().endswith((".xlsx", ".xlsm", ".xls", ".csv")):
        with db() as conn:
            rows = _payments_query(conn)
        return templates.TemplateResponse(request, "payments.html", {
            "rows": rows, "status": "", "remittance_type": "", "q": "",
            "all_statuses": ALL_PAYMENT_STATUSES, "all_types": ALL_REMITTANCE_TYPES,
            "total_amount": sum(float(r["amount"] or 0) for r in rows),
            "error": "Please upload a supported bank statement file: .xlsx, .xlsm, .xls, or .csv.",
            "upload_result": None,
        })

    tmp = Path(tempfile.mkdtemp()) / Path(file.filename).name
    try:
        with tmp.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        try:
            result = ingest_ra260(tmp)
            err = None
        except DuplicatePaymentsUploadError as exc:
            raise HTTPException(status_code=409, detail=f"Duplicate payments upload: {exc}") from exc
        except RA260ParseError as exc:
            raise HTTPException(status_code=422, detail=f"RA260 parse error: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)

    with db() as conn:
        rows = _payments_query(conn)
    return templates.TemplateResponse(request, "payments.html", {
        "rows": rows, "status": "", "remittance_type": "", "q": "",
        "all_statuses": ALL_PAYMENT_STATUSES, "all_types": ALL_REMITTANCE_TYPES,
        "total_amount": sum(float(r["amount"] or 0) for r in rows),
        "error": err, "upload_result": result,
    })


@router.post("/payments/{payment_id}/generate-je")
def payment_generate_je(payment_id: int) -> Response:
    res = generate_jes_for_payments(payment_ids=[payment_id])
    if res.je_generated == 0 and res.skipped_already_has_je == 0 and res.skipped_ineligible_status == 0:
        raise HTTPException(404, f"Payment {payment_id} not found")
    return RedirectResponse("/payments", status_code=303)


@router.post("/payments/file/{uploaded_file_id}/generate-jes")
def payments_file_generate_jes(uploaded_file_id: int) -> Response:
    generate_jes_for_payments(uploaded_file_id=uploaded_file_id)
    return RedirectResponse("/payments", status_code=303)


@router.post("/payments/generate-jes-all")
def payments_generate_jes_all() -> Response:
    generate_jes_for_payments()
    return RedirectResponse("/payments", status_code=303)
