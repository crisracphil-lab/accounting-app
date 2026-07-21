"""app/routers/requests.py — Payment requests (list, create, detail, status)."""
from __future__ import annotations

import contextlib
import logging
import mimetypes
import shutil
import tempfile
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from app.db import db, log_action
from app.deps import (
    REQUEST_UPLOAD_DIR,
    _absolute_app_link,
    _can_access_request,
    _current_user,
    _get_request_form_accounts,
    _is_accounting_user,
    _is_operations_manager,
    _needs_operations_approval,
    _notify_accounting,
    _notify_once,
    _notify_operations_managers,
    _notify_requesting_department,
    _operations_access_companies,
    _operations_access_company_ids,
    _parse_expense_rows_from_journal,
    _recent_month_options,
    _send_email_notification,
    _sha256_file,
    _validate_iso_date_or_400,
    templates,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# ── File-size guard ───────────────────────────────────────────────────────────
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


def _safe_float(val) -> float:
    """Convert a DB amount value to float, tolerating comma-formatted strings."""
    if val is None:
        return 0.0
    return float(str(val).replace(",", "").strip() or 0)


def _resolve_content_type(client_content_type: str | None, filename: str) -> str:
    """Determine a reliable MIME type for a stored upload.

    Browsers don't always send an accurate (or any) Content-Type for a given
    file part of a multipart/form-data upload — this is especially common
    for PDFs on certain OS/browser combinations, where the part arrives as
    the generic 'application/octet-stream'. Templates use the stored value
    to decide which icon/preview to show (e.g. checking for 'pdf' or
    'image' as a substring), so a missing or generic value silently breaks
    that UI. Prefer the client-supplied type when it looks specific; only
    fall back to guessing from the filename extension when it doesn't.
    """
    client_ct = (client_content_type or "").strip().lower()
    if client_ct and client_ct != "application/octet-stream":
        return client_ct
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or client_ct or "application/octet-stream"


def _check_upload_size(upload: UploadFile) -> bytes:
    """Read the upload into memory, enforce the 10 MB cap, and return the bytes.

    Using .file.read() on the SpooledTemporaryFile is synchronous and safe;
    it works in both sync and async route handlers.  The bytes are returned
    so callers can write them directly without a second read.
    """
    content = upload.file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        size_mb = len(content) / (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=(
                f"'{upload.filename}' is {size_mb:.1f} MB — the maximum allowed "
                f"file size is 10 MB. Please compress or split the file before uploading."
            ),
        )
    return content



# ---------- Private helpers --------------------------------------------------

def _co_id(conn, value):
    """Return a usable company_id integer.

    Falls back to the first active company when *value* is None or 0 —
    never hard-codes company id=1 so the app works after company 1 is deleted.
    """
    cid = int(value) if value else 0
    if cid:
        row = conn.execute("SELECT id FROM companies WHERE id = ?", (cid,)).fetchone()
        if row:
            return cid
    row = conn.execute("SELECT id FROM companies WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def _request_form_response(request: Request, error: str):
    user = _current_user(request)
    with db() as conn:
        company_id = _co_id(conn, user["company_id"])
        departments = conn.execute(
            "SELECT * FROM departments WHERE is_active = 1 AND company_id = ? ORDER BY name",
            (company_id,),
        ).fetchall()
        companies = conn.execute("SELECT * FROM companies WHERE is_active = 1 ORDER BY name").fetchall()
        accounts = _get_request_form_accounts(conn)
    return templates.TemplateResponse(request, "request_form.html", {
        "departments": departments, "companies": companies,
        "accounts": accounts, "user": user, "error": error,
    })


def _get_request_attachment_for_user(request: Request, request_id: int, attachment_id: int):
    user = _current_user(request)
    with db() as conn:
        pr = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        if not _can_access_request(conn, user, pr):
            raise HTTPException(status_code=403, detail="Attachment access denied")
        attachment = conn.execute(
            "SELECT * FROM request_attachments WHERE id = ? AND request_id = ?", (attachment_id, request_id)
        ).fetchone()
        if attachment is None:
            raise HTTPException(status_code=404, detail="Attachment not found")
    raw_path = attachment["stored_path"] or ""
    path = Path(raw_path).resolve()
    # Guard against path traversal: stored_path must be within REQUEST_UPLOAD_DIR
    try:
        path.relative_to(REQUEST_UPLOAD_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid attachment path")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Attachment file is missing")
    return attachment, path


# ---------- Requests list and form -------------------------------------------

@router.get("/requests", response_class=HTMLResponse)
def requests_page(request: Request, status: str = "", company_id: int = 0,
                  message: str = "", start: str = "", end: str = "", search: str = "",
                  period: str = "") -> Response:
    user = _current_user(request)
    # Normalise date range filter
    date_start = start.strip() if start.strip() else None
    date_end   = end.strip()   if end.strip()   else None

    # Default to the current month when the caller didn't specify any date
    # range at all — otherwise the list shows every request ever made, which
    # gets unwieldy. Explicitly choosing "All time" in the UI sends
    # period=all, which bypasses this default.
    if not date_start and not date_end and period.strip().lower() != "all":
        _today = date.today()
        _first_day = _today.replace(day=1)
        _next_month = (_first_day.replace(day=28) + timedelta(days=4)).replace(day=1)
        _last_day = _next_month - timedelta(days=1)
        date_start = _first_day.isoformat()
        date_end = _last_day.isoformat()

    with db() as conn:
        selected_company_id = _co_id(conn, user["company_id"])
        ops_companies = []

        # ── Build base WHERE + params ─────────────────────────────────────────
        if _is_operations_manager(user):
            ops_companies = _operations_access_companies(conn, user)
            allowed_company_ids = [int(c["id"]) for c in ops_companies]
            if company_id and company_id in allowed_company_ids:
                selected_company_id = company_id
            elif allowed_company_ids:
                selected_company_id = allowed_company_ids[0]
            where_parts = [
                "pr.company_id = ?",
                "pr.request_type = 'reimbursement'",
                "CAST(REPLACE(pr.amount, ',', '') AS REAL) > 50000",
                "pr.is_draft = 0",
            ]
            params_list = [selected_company_id]
        elif _is_accounting_user(user):
            where_parts = ["pr.company_id = ?", "(pr.is_draft = 0 OR pr.requester_user_id = ?)"]
            params_list = [selected_company_id, user["id"]]
        else:
            where_parts = ["pr.requester_user_id = ?"]
            params_list = [user["id"]]

        if status:
            where_parts.append("pr.status = ?")
            params_list.append(status)
        if date_start:
            where_parts.append("date(pr.created_at) >= ?")
            params_list.append(date_start)
        if date_end:
            where_parts.append("date(pr.created_at) <= ?")
            params_list.append(date_end)
        search_term = search.strip()
        if search_term:
            where_parts.append("(pr.payee_name LIKE ? OR pr.description LIKE ? OR pr.supplier_name LIKE ?)")
            like_term = f"%{search_term}%"
            params_list.extend([like_term, like_term, like_term])

        # where_parts contains only literal SQL condition strings; all user
        # values travel through ? placeholders in params_list.  We join
        # without an f-string so no user text ever reaches the SQL string.
        where_sql = "WHERE " + " AND ".join(where_parts)

        rows = conn.execute(
            "SELECT pr.*, u.username, u.full_name, d.name AS department_name,"
            "       COUNT(ra.id) AS attachment_count,"
            "       c.name AS company_name,"
            "       coa.code AS account_code, coa.name AS account_title"
            "  FROM payment_requests pr"
            "  JOIN users u ON u.id = pr.requester_user_id"
            "  LEFT JOIN departments d ON d.id = pr.department_id"
            "  LEFT JOIN companies c ON c.id = pr.company_id"
            "  LEFT JOIN request_attachments ra ON ra.request_id = pr.id"
            "  LEFT JOIN chart_of_accounts coa ON coa.id = pr.account_id"
            "  " + where_sql +
            "  GROUP BY pr.id"
            "  ORDER BY pr.created_at DESC, pr.id DESC"
            "  LIMIT 500",
            tuple(params_list),
        ).fetchall()

        # ── Aging / SLA flag: is this request's due date approaching or
        # already passed? Computed from payment_requests.due_date, already
        # present on each row in `rows` — no extra query needed. Warning =
        # due within 3 days; critical = due date already passed.
        aging_by_id: dict[int, dict] = {}
        _AGING_STATUSES = {"submitted", "for_review", "for_process", "approved"}
        _today = date.today()
        for _r in rows:
            if _r["status"] not in _AGING_STATUSES or not _r["due_date"]:
                continue
            try:
                _due = date.fromisoformat((_r["due_date"] or "")[:10])
            except ValueError:
                continue
            _days_left = (_due - _today).days
            if _days_left < 0:
                aging_by_id[_r["id"]] = {"level": "critical", "text": f"{abs(_days_left)}d overdue"}
            elif _days_left <= 3:
                aging_by_id[_r["id"]] = {"level": "warning", "text": "due today" if _days_left == 0 else f"due in {_days_left}d"}

        # ── Status badge counts (unfiltered by date, always show full picture) ─
        if _is_operations_manager(user):
            stats = conn.execute(
                """SELECT status, COUNT(*) AS n, SUM(CAST(REPLACE(amount,',','') AS REAL)) AS total
                   FROM payment_requests
                   WHERE company_id=? AND request_type='reimbursement'
                     AND CAST(REPLACE(amount,',','') AS REAL)>50000 AND is_draft=0 GROUP BY status""",
                (selected_company_id,)).fetchall()
        elif _is_accounting_user(user):
            stats = conn.execute(
                """SELECT status, COUNT(*) AS n, SUM(CAST(REPLACE(amount,',','') AS REAL)) AS total
                   FROM payment_requests WHERE company_id=? AND is_draft=0 GROUP BY status""",
                (selected_company_id,)).fetchall()
        else:
            stats = conn.execute(
                """SELECT status, COUNT(*) AS n, SUM(CAST(REPLACE(amount,',','') AS REAL)) AS total
                   FROM payment_requests WHERE requester_user_id=? GROUP BY status""",
                (user["id"],)).fetchall()

        # ── By-account summary using line items where available ───────────────
        account_summary = []
        if rows and (_is_accounting_user(user) or _is_operations_manager(user)):
            active_ids = [r["id"] for r in rows if r["status"] not in ("cancelled", "rejected")]
            acct_pivot: dict[str, dict] = {}

            if active_ids:
                # placeholders is derived from len(active_ids), never from the
                # ID values themselves, so "?" * n is safe to interpolate.
                placeholders = ",".join("?" * len(active_ids))
                li_rows = conn.execute(
                    "SELECT li.request_id, li.amount,"
                    "       coa.code AS account_code, coa.name AS account_title"
                    "  FROM request_line_items li"
                    "  LEFT JOIN chart_of_accounts coa ON coa.id = li.account_id"
                    "  WHERE li.request_id IN (" + placeholders + ")",
                    tuple(active_ids),
                ).fetchall()

                requests_with_li = {r["request_id"] for r in li_rows}

                for li in li_rows:
                    key = li["account_title"] or "Unclassified"
                    if key not in acct_pivot:
                        acct_pivot[key] = {"code": li["account_code"] or "", "total": 0.0, "count": 0}
                    acct_pivot[key]["total"] += float(li["amount"] or 0)

                for r in rows:
                    if r["status"] in ("cancelled", "rejected"):
                        continue
                    if r["id"] in requests_with_li:
                        continue
                    key = r["account_title"] or "Unclassified"
                    if key not in acct_pivot:
                        acct_pivot[key] = {"code": r["account_code"] or "", "total": 0.0, "count": 0}
                    acct_pivot[key]["total"] += float(r["amount"] or 0)

                for r in rows:
                    if r["status"] in ("cancelled", "rejected"):
                        continue
                    if r["id"] in requests_with_li:
                        continue
                    key = r["account_title"] or "Unclassified"
                    acct_pivot[key]["count"] += 1
                req_acct_seen: set = set()
                for li in li_rows:
                    key = li["account_title"] or "Unclassified"
                    bucket_key = (li["request_id"], key)
                    if bucket_key not in req_acct_seen:
                        req_acct_seen.add(bucket_key)
                        if key in acct_pivot:
                            acct_pivot[key]["count"] += 1

            account_summary = sorted(acct_pivot.items(), key=lambda x: -x[1]["total"])

    # Build stats dicts
    stats_count = {r["status"]: r["n"] for r in stats}
    stats_total = {r["status"]: float(r["total"] or 0) for r in stats}
    pending_statuses = ("submitted", "for_review", "for_process", "approved")
    pending_total = sum(stats_total.get(s, 0) for s in pending_statuses)
    paid_total    = stats_total.get("paid", 0)

    with db() as conn:
        my_drafts = conn.execute(
            """SELECT id, request_type, payee_name, description, amount, created_at, updated_at
               FROM payment_requests
               WHERE requester_user_id = ? AND is_draft = 1
               ORDER BY COALESCE(updated_at, created_at) DESC""",
            (user["id"],),
        ).fetchall()

    def _safe_amt(v):
        try:
            return Decimal(str(v).replace(",", ""))
        except InvalidOperation:
            return Decimal(0)
    requests_total = sum(_safe_amt(r["amount"]) for r in rows
                         if (r["status"] not in ("cancelled", "rejected")))

    # ── Duplicate detection for current result set ───────────────────────────
    _grp: dict = defaultdict(list)
    for r in rows:
        _k = (str(r["payee_name"] or "").strip().lower(), str(r["amount"] or "").strip())
        _grp[_k].append(r)
    duplicate_request_ids: set = set()
    for _reqs in _grp.values():
        if len(_reqs) < 2:
            continue
        _dated = []
        for _r in _reqs:
            try:
                _dated.append((date.fromisoformat((_r["created_at"] or "")[:10]), _r["id"]))
            except ValueError:
                _dated.append((None, _r["id"]))
        for _i in range(len(_dated)):
            for _j in range(_i + 1, len(_dated)):
                _d1, _d2 = _dated[_i][0], _dated[_j][0]
                if _d1 and _d2 and abs((_d1 - _d2).days) <= 30:
                    duplicate_request_ids.add(_dated[_i][1])
                    duplicate_request_ids.add(_dated[_j][1])

    return templates.TemplateResponse(request, "requests.html", {
        "requests": rows,
        "my_drafts": my_drafts,
        "status": status,
        "search": search_term,
        "date_start": date_start or "",
        "date_end":   date_end   or "",
        "message": message or None,
        "stats": stats_count,
        "stats_total": stats_total,
        "pending_total": pending_total,
        "paid_total": paid_total,
        "requests_total": float(requests_total),
        "account_summary": account_summary,
        "is_accounting": (_is_accounting_user(user) and not _is_operations_manager(user)),
        "is_operations_manager": _is_operations_manager(user),
        "ops_companies": ops_companies,
        "selected_company_id": selected_company_id,
        "export_month": date.today().strftime("%Y-%m"),
        "export_month_options": _recent_month_options(),
        "duplicate_request_ids": duplicate_request_ids,
        "aging_by_id": aging_by_id,
    })


@router.post("/requests/parse-expenses")
async def parse_expense_journal(request: Request, journal_file: UploadFile = File(...)) -> Response:
    """Parse an uploaded journal/ledger file and return auto-classified expense line items as JSON."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Login required"}, status_code=401)

    allowed = (".xlsx", ".xlsm", ".xls", ".csv")
    fname = (journal_file.filename or "").lower()
    if not any(fname.endswith(ext) for ext in allowed):
        return JSONResponse({"error": "Please upload a journal file as .xlsx, .xlsm, .xls, or .csv."}, status_code=400)

    safe_name = Path(journal_file.filename).name
    tmp_dir = Path(tempfile.mkdtemp())
    tmp_path = tmp_dir / safe_name
    try:
        with tmp_path.open("wb") as out:
            shutil.copyfileobj(journal_file.file, out)

        raw_rows = _parse_expense_rows_from_journal(tmp_path)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return JSONResponse({"error": f"Could not read file: {exc}"}, status_code=500)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    classified = []
    with db() as conn:
        company_id = _co_id(conn, user["company_id"])
        acct_by_code = {
            r["code"].strip(): r
            for r in conn.execute(
                "SELECT id, code, name FROM chart_of_accounts WHERE is_active = 1"
            ).fetchall()
        }

        for row in raw_rows:
            raw_code  = (row["raw_code"] or "").strip()
            raw_title = (row["raw_title"] or "").strip()
            desc      = row["description"]
            amount    = row["amount"]

            if raw_code and raw_code in acct_by_code:
                acct = acct_by_code[raw_code]
                classified.append({
                    "account_id":   acct["id"],
                    "account_code": acct["code"],
                    "account_name": acct["name"],
                    "description":  raw_title or desc,
                    "amount":       amount,
                    "confidence":   0.98,
                    "source":       "direct_match",
                })
                continue

            from app.services.classifier import classify as _classify
            try:
                clf = _classify(conn, description=desc, company_id=company_id)
                classified.append({
                    "account_id":   clf.target_account_id,
                    "account_code": clf.target_account_code,
                    "account_name": clf.target_account_name,
                    "description":  desc,
                    "amount":       amount,
                    "confidence":   clf.confidence,
                    "source":       "rule" if clf.rule_pattern else "unclassified",
                })
            except Exception:
                logger.warning("Classification failed for description %r", desc, exc_info=True)
                classified.append({
                    "account_id":   None,
                    "account_code": "",
                    "account_name": "— Unclassified —",
                    "description":  desc,
                    "amount":       amount,
                    "confidence":   0.0,
                    "source":       "unclassified",
                })

    return JSONResponse({"items": classified, "count": len(classified)})


@router.post("/requests/parse-receipt")
async def parse_receipt_image(request: Request, receipt_file: UploadFile = File(...)) -> Response:
    """OCR an uploaded receipt photo and return a best-guess payee/amount/date as JSON.

    This only ever returns suggestions for the requester to review — it never
    writes anything to the database by itself.
    """
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Login required"}, status_code=401)

    allowed = (".jpg", ".jpeg", ".png", ".webp")
    fname = (receipt_file.filename or "").lower()
    if not any(fname.endswith(ext) for ext in allowed):
        return JSONResponse({"error": "Please upload a receipt photo as .jpg, .jpeg, .png, or .webp."}, status_code=400)

    try:
        image_bytes = await receipt_file.read()
        from app.services.receipt_ocr import extract_receipt_fields, ocr_image_bytes
        text = ocr_image_bytes(image_bytes)
        fields = extract_receipt_fields(text)
        with db() as conn:
            def _resolve_account_id(code):
                if not code:
                    return None
                row = conn.execute(
                    "SELECT id FROM chart_of_accounts WHERE code = ? AND is_active = 1", (code,)
                ).fetchone()
                return row["id"] if row else None
            fields["account_id"] = _resolve_account_id(fields.get("account_code"))
            for item in fields.get("line_items", []):
                item["account_id"] = _resolve_account_id(item.get("account_code"))
    except RuntimeError as exc:
        # OCR dependency (pytesseract/Pillow, or the Tesseract program itself) missing.
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception as exc:
        return JSONResponse({"error": f"Could not read receipt image: {exc}"}, status_code=500)

    return JSONResponse(fields)


@router.post("/requests/templates")
def request_template_create(request: Request,
                             name: str = Form(...),
                             request_type: str = Form("supplier_payment"),
                             payee_name: str = Form(""),
                             description: str = Form(""),
                             amount: str = Form(""),
                             account_id: int = Form(0)) -> Response:
    """Save the current form's fields as a reusable, personal template."""
    user = _current_user(request)
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Template name is required")
    if request_type not in {"supplier_payment", "reimbursement"}:
        request_type = "supplier_payment"
    with db() as conn:
        valid_acct_ids = {r["id"] for r in conn.execute("SELECT id FROM chart_of_accounts WHERE is_active = 1").fetchall()}
        clean_account_id = account_id if account_id in valid_acct_ids else None
        conn.execute(
            "INSERT INTO request_templates (owner_user_id, name, request_type, payee_name, description, amount, account_id)"
            " VALUES (?,?,?,?,?,?,?)",
            (user["id"], clean_name, request_type, payee_name.strip() or None,
             description.strip() or None, amount.strip() or None, clean_account_id),
        )
    return RedirectResponse("/requests/new", status_code=303)


@router.post("/requests/templates/{template_id}/delete")
def request_template_delete(request: Request, template_id: int) -> Response:
    user = _current_user(request)
    with db() as conn:
        tpl = conn.execute("SELECT * FROM request_templates WHERE id = ?", (template_id,)).fetchone()
        if tpl is None or tpl["owner_user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail="Template not found")
        conn.execute("DELETE FROM request_templates WHERE id = ?", (template_id,))
    return RedirectResponse("/requests/new", status_code=303)


@router.get("/requests/new", response_class=HTMLResponse)
def request_new_page(request: Request, edit_draft: int = 0) -> Response:
    user = _current_user(request)
    with db() as conn:
        company_id = _co_id(conn, user["company_id"])
        departments = conn.execute(
            "SELECT * FROM departments WHERE is_active = 1 AND company_id = ? ORDER BY name",
            (company_id,),
        ).fetchall()
        companies = conn.execute("SELECT * FROM companies WHERE is_active = 1 ORDER BY name").fetchall()
        accounts = _get_request_form_accounts(conn)
        saved_templates = conn.execute(
            "SELECT * FROM request_templates WHERE owner_user_id = ? ORDER BY name", (user["id"],)
        ).fetchall()
        import json as _json
        templates_json = _json.dumps([
            {
                "id": t["id"],
                "request_type": t["request_type"],
                "payee_name": t["payee_name"] or "",
                "supplier_name": t["supplier_name"] or "",
                "description": t["description"] or "",
                "amount": t["amount"] or "",
                "account_id": t["account_id"] or 0,
            }
            for t in saved_templates
        ])
        draft = None
        draft_line_items = []
        if edit_draft:
            draft = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (edit_draft,)).fetchone()
            if draft is None or draft["requester_user_id"] != user["id"] or int(draft["is_draft"] or 0) != 1:
                raise HTTPException(status_code=404, detail="Draft not found")
            draft_line_items = conn.execute(
                "SELECT * FROM request_line_items WHERE request_id = ? ORDER BY sort_order", (edit_draft,)
            ).fetchall()
    return templates.TemplateResponse(request, "request_form.html", {
        "departments": departments, "companies": companies,
        "accounts": accounts, "user": user, "error": None,
        "draft": draft, "draft_line_items": draft_line_items,
        "saved_templates": saved_templates, "templates_json": templates_json,
    })


@router.post("/requests/create", response_class=HTMLResponse)
async def request_create(request: Request,
                         request_type: str = Form("supplier_payment"),
                         department_name: str = Form(""),
                         supplier_name: str = Form(""),
                         payee_name: str = Form(...),
                         description: str = Form(...),
                         amount: str = Form("0"),
                         due_date: str = Form(""),
                         account_id: int = Form(0),
                         requester_email: str = Form(""),
                         action: str = Form("submit"),
                         file: list[UploadFile] | None = File(None)) -> Response:
    is_draft_save = (action or "submit").strip().lower() == "draft"
    # --- Parse multi-value line-item fields from raw form data ---
    form_data = await request.form()
    raw_li_accounts     = form_data.getlist("li_account_id")
    raw_li_descriptions = form_data.getlist("li_description")
    raw_li_amounts      = form_data.getlist("li_amount")

    line_items = []
    for acct_raw, desc_raw, amt_raw in zip(raw_li_accounts, raw_li_descriptions, raw_li_amounts, strict=False):
        try:
            li_amount = Decimal(str(amt_raw).replace(",", "").strip())
        except (InvalidOperation, ValueError, ArithmeticError):
            continue
        if li_amount == 0:
            continue
        line_items.append({
            "account_id": int(acct_raw) if str(acct_raw).strip().isdigit() else None,
            "description": str(desc_raw).strip(),
            "amount": li_amount,
        })

    user = _current_user(request)
    if request_type not in {"supplier_payment", "reimbursement"}:
        return _request_form_response(request, "Invalid request type.")
    payee_name = payee_name.strip()
    description = description.strip()
    supplier_name = supplier_name.strip() or payee_name
    if not payee_name or not description:
        return _request_form_response(request, "Payee and description are required.")

    if line_items:
        clean_amount = str(sum(li["amount"] for li in line_items))
    else:
        try:
            clean_amount = str(Decimal(amount.replace(",", "")))
        except (InvalidOperation, ValueError, ArithmeticError):
            return _request_form_response(request, "Amount must be numeric, or add at least one line item.")
        if Decimal(clean_amount) <= 0:
            return _request_form_response(request, "Amount must be greater than zero.")

    if due_date:
        due_date = _validate_iso_date_or_400(due_date, "due date")

    uploaded_files = [f for f in (file or []) if f is not None and f.filename]
    if not uploaded_files and not is_draft_save:
        return _request_form_response(request, "Please attach at least one PO, invoice, receipt, or reimbursement form before submitting.")
    if len(uploaded_files) > 10:
        return _request_form_response(request, "You can attach a maximum of 10 files.")

    REQUEST_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        company_id = _co_id(conn, user["company_id"])
        department_id = user.get("department_id", None)
        dept_name = department_name.strip()
        if dept_name and department_id is None and company_id:
            matches = conn.execute(
                "SELECT id FROM departments WHERE lower(trim(name))=lower(trim(?)) AND is_active = 1 AND company_id = ?",
                (dept_name, company_id),
            ).fetchall()
            if len(matches) > 1:
                return _request_form_response(request, "Invalid or duplicate department. Please select one valid department for your company.")
            if len(matches) == 1:
                department_id = matches[0]["id"]
            else:
                cur_dept = conn.execute("INSERT INTO departments (name, company_id) VALUES (?, ?)", (dept_name, company_id))
                department_id = cur_dept.lastrowid
        if department_id and not conn.execute(
            "SELECT id FROM departments WHERE id = ? AND is_active = 1 AND company_id = ?",
            (department_id, company_id)
        ).fetchone():
            return _request_form_response(request, "Selected department does not belong to your company.")
        duplicate_request = conn.execute(
            """SELECT id FROM payment_requests
               WHERE requester_user_id = ?
                 AND request_type = ?
                 AND COALESCE(department_id, 0) = COALESCE(?, 0)
                 AND lower(trim(payee_name)) = lower(trim(?))
                 AND lower(trim(description)) = lower(trim(?))
                 AND amount = ?
                 AND created_at >= datetime('now', '-5 minutes')
               ORDER BY id DESC LIMIT 1""",
            (user["id"], request_type, department_id, payee_name, description, clean_amount),
        ).fetchone()
        if duplicate_request:
            dup_message = f"Duplicate request detected. Request #{duplicate_request['id']} was already submitted recently and cannot be processed again."
            _notify_once(conn, user["id"], "Duplicate request blocked", dup_message, f"/requests/{duplicate_request['id']}", send_email=False)
            return templates.TemplateResponse(request, "request_submit_result.html", {
                "outcome": "duplicate",
                "payee_name": payee_name,
                "request_type": request_type,
                "existing_id": duplicate_request["id"],
                "message": dup_message,
            })

        resolved_account_id: int | None = None
        if not line_items:
            resolved_account_id = account_id if account_id else None
            if not resolved_account_id:
                sup_row = conn.execute(
                    "SELECT default_expense_account_id FROM suppliers WHERE lower(trim(name)) = lower(trim(?)) AND is_active = 1 LIMIT 1",
                    (supplier_name,),
                ).fetchone()
                if sup_row and sup_row["default_expense_account_id"]:
                    resolved_account_id = int(sup_row["default_expense_account_id"])
            if resolved_account_id and not conn.execute(
                "SELECT id FROM chart_of_accounts WHERE id = ? AND is_active = 1", (resolved_account_id,)
            ).fetchone():
                resolved_account_id = None

        clean_requester_email = requester_email.strip() or None
        cur = conn.execute(
            """INSERT INTO payment_requests
               (company_id, request_type, requester_user_id, department_id, supplier_name, payee_name, description, amount, due_date, account_id, requester_email, is_draft)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (company_id, request_type, user["id"], department_id, supplier_name, payee_name, description, clean_amount, due_date or None, resolved_account_id, clean_requester_email, 1 if is_draft_save else 0),
        )
        request_id = cur.lastrowid

        valid_acct_ids = {r["id"] for r in conn.execute("SELECT id FROM chart_of_accounts WHERE is_active = 1").fetchall()}
        for idx, li in enumerate(line_items):
            li_account_id = li["account_id"] if li["account_id"] in valid_acct_ids else None
            conn.execute(
                "INSERT INTO request_line_items (request_id, account_id, description, amount, sort_order) VALUES (?,?,?,?,?)",
                (request_id, li_account_id, li["description"] or "", float(li["amount"]), idx),
            )
        invoice_id = None
        for idx, upload in enumerate(uploaded_files, start=1):
            file_bytes = _check_upload_size(upload)
            safe_name = Path(upload.filename).name
            stored = REQUEST_UPLOAD_DIR / f"request_{request_id}_{idx}_{safe_name}"
            with stored.open("wb") as out:
                out.write(file_bytes)
            file_hash = _sha256_file(stored)
            file_size = stored.stat().st_size
            conn.execute(
                """INSERT OR IGNORE INTO invoice_uploads (filename, file_size, sha256, parsed_count)
                   VALUES (?, ?, ?, 1)""",
                (f"Request {request_id}: {safe_name}", file_size, file_hash),
            )
            upload_row = conn.execute("SELECT id FROM invoice_uploads WHERE sha256 = ?", (file_hash,)).fetchone()
            if upload_row is None:
                import logging as _log
                _log.warning("[request_create] upload_row unexpectedly None for sha256=%s; skipping invoice record", file_hash)
                this_invoice_id = None
            else:
                uploaded_file_id = upload_row["id"]
                try:
                    inv = conn.execute(
                        """INSERT INTO invoices
                           (uploaded_file_id, invoice_number, invoice_date, supplier_name, description,
                            gross_amount, vat_amount, ewt_amount, net_amount, due_date, source_filename, status)
                           VALUES (?, ?, date('now'), ?, ?, ?, NULL, NULL, ?, ?, ?, 'unmatched')""",
                        (uploaded_file_id, f"REQ-{request_id}-{idx}", supplier_name, description, clean_amount, clean_amount, due_date or None, safe_name),
                    )
                    this_invoice_id = inv.lastrowid
                except Exception as _inv_exc:
                    import logging as _log
                    _log.warning("[request_create] Could not create invoice record for request %s file %s: %s", request_id, safe_name, _inv_exc)
                    this_invoice_id = None
            if invoice_id is None:
                invoice_id = this_invoice_id
                conn.execute("UPDATE payment_requests SET invoice_id = ? WHERE id = ?", (invoice_id, request_id))
            conn.execute(
                """INSERT INTO request_attachments
                   (request_id, filename, stored_path, content_type, file_size, sha256, invoice_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (request_id, safe_name, str(stored), _resolve_content_type(upload.content_type, safe_name), file_size, file_hash, this_invoice_id),
            )
        link = f"/requests/{request_id}"
        submitted_message = f"Your {request_type.replace('_', ' ')} request for {payee_name} amounting to {clean_amount} was submitted to Accounting."
        if is_draft_save:
            log_action(conn, "create_draft", "payment_request", request_id,
                       {"status": "draft", "company_id": company_id}, user_id=user["username"])
        else:
            _notify_requesting_department(conn, request_id, company_id, department_id, user["id"], "Request submitted", submitted_message)
            if _needs_operations_approval(request_type, clean_amount):
                _notify_operations_managers(conn, "Approval needed", f"High-value reimbursement for {payee_name} amounting to {clean_amount} needs General Manager approval.", link, company_id=company_id)
                _notify_accounting(conn, "High-value reimbursement submitted", f"{payee_name} reimbursement for {clean_amount} was submitted and needs General Manager approval.", link, company_id=company_id)
            else:
                _notify_accounting(conn, "New payment request", f"{payee_name} request for {clean_amount} was submitted.", link, company_id=company_id)
            log_action(conn, "create", "payment_request", request_id, {"status": "submitted", "invoice_id": invoice_id, "company_id": company_id}, user_id=user["username"])
    if is_draft_save:
        return RedirectResponse(f"/requests/{request_id}?message=Draft+saved", status_code=303)
    return templates.TemplateResponse(request, "request_submit_result.html", {
        "outcome": "success",
        "request_id": request_id,
        "payee_name": payee_name,
        "request_type": request_type,
        "clean_amount": clean_amount,
        "line_items": line_items,
        "description": description,
        "needs_ops_approval": _needs_operations_approval(request_type, clean_amount),
    })


# Export routes moved to requests_export.py
# Invoice routes moved to invoices.py

@router.post("/requests/{request_id}/submit-draft")
def request_submit_draft(request: Request, request_id: int) -> Response:
    """Requester submits a previously-saved draft for real: notifications fire,
    is_draft flips to 0, and the request becomes visible/actionable per the
    normal _can_access_request rules from this point on."""
    user = _current_user(request)
    with db() as conn:
        pr = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        if pr["requester_user_id"] != user["id"] or int(pr["is_draft"] or 0) != 1:
            raise HTTPException(status_code=404, detail="Request not found")

        conn.execute(
            "UPDATE payment_requests SET is_draft = 0, updated_at = datetime('now') WHERE id = ?",
            (request_id,),
        )

        company_id = int(pr["company_id"] or 1)
        department_id = pr["department_id"]
        payee_name = pr["payee_name"]
        clean_amount = pr["amount"]
        request_type = pr["request_type"]
        link = f"/requests/{request_id}"
        submitted_message = f"Your {request_type.replace('_', ' ')} request for {payee_name} amounting to {clean_amount} was submitted to Accounting."

        _notify_requesting_department(conn, request_id, company_id, department_id, user["id"], "Request submitted", submitted_message)
        if _needs_operations_approval(request_type, clean_amount):
            _notify_operations_managers(conn, "Approval needed", f"High-value reimbursement for {payee_name} amounting to {clean_amount} needs General Manager approval.", link, company_id=company_id)
            _notify_accounting(conn, "High-value reimbursement submitted", f"{payee_name} reimbursement for {clean_amount} was submitted and needs General Manager approval.", link, company_id=company_id)
        else:
            _notify_accounting(conn, "New payment request", f"{payee_name} request for {clean_amount} was submitted.", link, company_id=company_id)

        log_action(conn, "submit_draft", "payment_request", request_id,
                   {"status": "submitted", "company_id": company_id}, user_id=user["username"])

    return RedirectResponse(f"/requests/{request_id}?message=Request+submitted", status_code=303)


@router.post("/requests/{request_id}/update-draft")
async def request_update_draft(request: Request, request_id: int,
                               request_type: str = Form("supplier_payment"),
                               department_name: str = Form(""),
                               supplier_name: str = Form(""),
                               payee_name: str = Form(...),
                               description: str = Form(...),
                               amount: str = Form("0"),
                               due_date: str = Form(""),
                               account_id: int = Form(0),
                               requester_email: str = Form(""),
                               action: str = Form("draft")) -> Response:
    """Save edits to an existing draft in place. If action=submit, the draft
    is updated and then immediately submitted for real via the same
    notification path as request_submit_draft."""
    user = _current_user(request)
    form_data = await request.form()
    raw_li_accounts     = form_data.getlist("li_account_id")
    raw_li_descriptions = form_data.getlist("li_description")
    raw_li_amounts      = form_data.getlist("li_amount")

    line_items = []
    for acct_raw, desc_raw, amt_raw in zip(raw_li_accounts, raw_li_descriptions, raw_li_amounts):
        try:
            li_amount = Decimal(str(amt_raw).replace(",", "").strip())
        except (InvalidOperation, ValueError, ArithmeticError):
            continue
        if li_amount == 0:
            continue
        line_items.append({
            "account_id": int(acct_raw) if str(acct_raw).strip().isdigit() else None,
            "description": str(desc_raw).strip(),
            "amount": li_amount,
        })

    with db() as conn:
        pr = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None or pr["requester_user_id"] != user["id"] or int(pr["is_draft"] or 0) != 1:
            raise HTTPException(status_code=404, detail="Draft not found")

        payee_name = payee_name.strip()
        description = description.strip()
        supplier_name = supplier_name.strip() or payee_name
        if not payee_name or not description:
            raise HTTPException(status_code=400, detail="Payee and description are required.")

        if line_items:
            clean_amount = str(sum(li["amount"] for li in line_items))
        else:
            try:
                clean_amount = str(Decimal(amount.replace(",", "")))
            except (InvalidOperation, ValueError, ArithmeticError):
                raise HTTPException(status_code=400, detail="Amount must be numeric, or add at least one line item.")

        conn.execute(
            """UPDATE payment_requests
               SET request_type = ?, department_id = COALESCE(
                       (SELECT id FROM departments WHERE lower(trim(name)) = lower(trim(?)) AND company_id = ? AND is_active = 1),
                       department_id),
                   supplier_name = ?, payee_name = ?, description = ?, amount = ?,
                   due_date = ?, requester_email = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (request_type, department_name, int(pr["company_id"] or 1), supplier_name, payee_name,
             description, clean_amount, due_date or None, requester_email.strip() or None, request_id),
        )
        conn.execute("DELETE FROM request_line_items WHERE request_id = ?", (request_id,))
        valid_acct_ids = {r["id"] for r in conn.execute("SELECT id FROM chart_of_accounts WHERE is_active = 1").fetchall()}
        for idx, li in enumerate(line_items):
            li_account_id = li["account_id"] if li["account_id"] in valid_acct_ids else None
            conn.execute(
                "INSERT INTO request_line_items (request_id, account_id, description, amount, sort_order) VALUES (?,?,?,?,?)",
                (request_id, li_account_id, li["description"] or "", float(li["amount"]), idx),
            )
        log_action(conn, "update_draft", "payment_request", request_id,
                   {"payee_name": payee_name}, user_id=user["username"])

    if (action or "draft").strip().lower() == "submit":
        return request_submit_draft(request, request_id)
    return RedirectResponse(f"/requests/{request_id}?message=Draft+updated", status_code=303)


def _money_for_pdf(value) -> str:
    try:
        return f"{float(str(value).replace(',', '')):,.2f}"
    except (TypeError, ValueError):
        return str(value or "")


def _build_request_pdf(pr, line_items, attachments) -> bytes:
    """Render a single payment request as a printable PDF voucher."""
    import io
    from datetime import datetime

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=18 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    story = [Paragraph(f"BookPoint — Payment Request #{pr['id']}", styles["Title"]), Spacer(1, 6)]

    info_rows = [
        ("Status", (pr["status"] or "").replace("_", " ").title()),
        ("Request type", (pr["request_type"] or "").replace("_", " ").title()),
        ("Payee / Supplier", pr["payee_name"] or ""),
        ("Requester", pr["full_name"] or pr["username"] or ""),
        ("Department", pr["department_name"] or ""),
        ("Company", pr["company_name"] or ""),
        ("Amount", _money_for_pdf(pr["amount"])),
        ("Due date", pr["due_date"] or ""),
        ("Created", (pr["created_at"] or "")[:16]),
    ]
    try:
        account_code = pr["account_code"]
    except (KeyError, IndexError):
        account_code = None
    if account_code:
        info_rows.append(("GL account", f"{pr['account_code']} — {pr['account_title']}"))
    if pr["description"]:
        info_rows.append(("Description", pr["description"]))
    if pr["accounting_notes"]:
        info_rows.append(("Accounting notes", pr["accounting_notes"]))
    if pr["operations_notes"]:
        info_rows.append(("Operations notes", pr["operations_notes"]))

    info_table = Table(
        [[Paragraph(f"<b>{k}</b>", styles["Normal"]), Paragraph(str(v), styles["Normal"])] for k, v in info_rows],
        colWidths=[110, 360],
    )
    info_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f8fafc")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 14))

    if line_items:
        story.append(Paragraph("Line items", styles["Heading3"]))
        data = [["Description", "GL account", "Amount"]]
        total = 0.0
        for li in line_items:
            try:
                li_account_code = li["account_code"]
                li_account_title = li["account_title"]
            except (KeyError, IndexError):
                li_account_code = li_account_title = None
            acct = f"{li_account_code} — {li_account_title}" if li_account_code else "—"
            amt = float(li["amount"] or 0)
            total += amt
            data.append([li["description"] or "", acct, _money_for_pdf(amt)])
        data.append(["", "Total", _money_for_pdf(total)])
        li_table = Table(data, colWidths=[250, 150, 70])
        li_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (2, 0), (2, -1), "RIGHT"),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ]))
        story.append(li_table)
        story.append(Spacer(1, 14))

    if attachments:
        story.append(Paragraph("Attachments", styles["Heading3"]))
        for a in attachments:
            story.append(Paragraph(f"• {a['filename']}", styles["Normal"]))
        story.append(Spacer(1, 14))

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        f"<font size=8 color='#6b7280'>Exported from BookPoint on {datetime.now().strftime('%Y-%m-%d %H:%M')}</font>",
        styles["Normal"],
    ))

    doc.build(story)
    return buf.getvalue()


@router.get("/requests/{request_id}/export-pdf")
def request_export_pdf(request: Request, request_id: int) -> Response:
    """Export a single payment request as a printable PDF voucher.

    Reuses _can_access_request() — the same gatekeeper as the on-screen
    request detail page — so nothing new is exposed via this download that
    the user couldn't already see on screen.
    """
    user = _current_user(request)
    with db() as conn:
        pr = conn.execute(
            """SELECT pr.*, u.username, u.full_name, pr.requester_email, d.name AS department_name,
                      c.name AS company_name, coa.code AS account_code, coa.name AS account_title
               FROM payment_requests pr
               JOIN users u ON u.id = pr.requester_user_id
               LEFT JOIN departments d ON d.id = pr.department_id
               LEFT JOIN companies c ON c.id = pr.company_id
               LEFT JOIN chart_of_accounts coa ON coa.id = pr.account_id
               WHERE pr.id = ?""",
            (request_id,),
        ).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        if not _can_access_request(conn, user, pr):
            raise HTTPException(status_code=403, detail="Request access denied")
        line_items = conn.execute(
            """SELECT li.*, coa.code AS account_code, coa.name AS account_title
               FROM request_line_items li
               LEFT JOIN chart_of_accounts coa ON coa.id = li.account_id
               WHERE li.request_id = ? ORDER BY li.sort_order, li.id""",
            (request_id,),
        ).fetchall()
        attachments = conn.execute(
            "SELECT * FROM request_attachments WHERE request_id = ? ORDER BY id",
            (request_id,),
        ).fetchall()

    pdf_bytes = _build_request_pdf(pr, line_items, attachments)
    filename = f"bookpoint_request_{request_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _clean_money(value) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


@router.post("/requests/{request_id}/record-payment")
def request_record_payment(request: Request, request_id: int,
                            amount: str = Form(...),
                            paid_date: str = Form(...),
                            notes: str = Form("")) -> Response:
    """Record one installment payment against a supplier payment request.

    When the sum of recorded payments reaches the request's full amount, the
    request automatically moves to 'paid' — otherwise it's set to
    'partially_paid'. Reuses the same company-scoping rule as
    request_update_status() above.
    """
    user = _current_user(request)
    if not _is_accounting_user(user):
        raise HTTPException(status_code=403, detail="Accounting access required")
    with db() as conn:
        pr = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        if user["role"] != "admin" and int(pr["company_id"] or 0) != int(user["company_id"] or 0):
            raise HTTPException(status_code=403, detail="Accounting can only record payments for their own company")
        if pr["request_type"] != "supplier_payment":
            raise HTTPException(status_code=400, detail="Partial payments are only supported for supplier payments")
        if pr["status"] not in {"approved"}:
            raise HTTPException(status_code=400, detail="Request must be approved before recording a payment")

        clean_amount = _clean_money(amount)
        if clean_amount <= 0:
            raise HTTPException(status_code=400, detail="Payment amount must be greater than zero")

        conn.execute(
            "INSERT INTO payment_records (request_id, amount, paid_date, notes, recorded_by_user_id) VALUES (?,?,?,?,?)",
            (request_id, clean_amount, paid_date.strip(), notes.strip() or None, user["id"]),
        )

        total_paid = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM payment_records WHERE request_id = ?",
            (request_id,),
        ).fetchone()["total"]
        request_amount = _clean_money(pr["amount"])
        is_fully_paid = total_paid >= request_amount - 0.005
        remaining_after = round(request_amount - total_paid, 2)

        if is_fully_paid:
            conn.execute(
                "UPDATE payment_requests SET status = 'paid', updated_at = datetime('now'),"
                " paid_at = COALESCE(paid_at, datetime('now')) WHERE id = ?",
                (request_id,),
            )
        else:
            conn.execute(
                "UPDATE payment_requests SET updated_at = datetime('now') WHERE id = ?",
                (request_id,),
            )

        log_action(conn, "record_payment", "payment_request", request_id,
                   {"amount": clean_amount, "paid_date": paid_date.strip(),
                    "fully_paid": is_fully_paid, "remaining_balance": remaining_after},
                   user_id=user["username"])

        notif_title = "Payment recorded 💸" if is_fully_paid else "Partial payment recorded"
        status_note = "fully paid" if is_fully_paid else f"{remaining_after:,.2f} remaining"
        notif_body = f"A payment of {clean_amount:,.2f} was recorded for {pr['payee_name']} ({status_note})."
        _notify_once(conn, pr["requester_user_id"], notif_title, notif_body, f"/requests/{request_id}")

    return RedirectResponse(f"/requests/{request_id}?message=Payment+recorded", status_code=303)


@router.get("/requests/{request_id}", response_class=HTMLResponse)
def request_detail(request: Request, request_id: int, message: str = "") -> Response:
    user = _current_user(request)
    with db() as conn:
        pr = conn.execute(
            """SELECT pr.*, u.username, u.full_name, pr.requester_email, d.name AS department_name, c.name AS company_name,
                      ou.full_name AS operations_approved_by_name, ou.username AS operations_approved_by_username
               FROM payment_requests pr
               JOIN users u ON u.id = pr.requester_user_id
               LEFT JOIN departments d ON d.id = pr.department_id
               LEFT JOIN companies c ON c.id = pr.company_id
               LEFT JOIN users ou ON ou.id = pr.operations_approved_by_user_id
               WHERE pr.id = ?""", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        if not _can_access_request(conn, user, pr):
            raise HTTPException(status_code=403, detail="Request access denied")
        attachments = conn.execute("SELECT * FROM request_attachments WHERE request_id = ? ORDER BY id", (request_id,)).fetchall()
        invoice = conn.execute("SELECT * FROM invoices WHERE id = ?", (pr["invoice_id"],)).fetchone() if pr["invoice_id"] else None
        line_items = conn.execute(
            """SELECT li.*, coa.code AS account_code, coa.name AS account_title
               FROM request_line_items li
               LEFT JOIN chart_of_accounts coa ON coa.id = li.account_id
               WHERE li.request_id = ? ORDER BY li.sort_order, li.id""",
            (request_id,),
        ).fetchall()
        comments = conn.execute(
            """SELECT rc.*, u.full_name, u.username, u.role, u.department_id AS author_department_id
               FROM request_comments rc
               JOIN users u ON u.id = rc.author_user_id
               WHERE rc.request_id = ?
               ORDER BY rc.created_at ASC, rc.id ASC""",
            (request_id,),
        ).fetchall()
        can_view_comments = (
            _is_accounting_user(user)
            or int(pr["requester_user_id"] or 0) == int(user["id"] or 0)
        )
        if not can_view_comments:
            comments = []
        payment_records = conn.execute(
            "SELECT pay.*, u.full_name AS recorded_by_name, u.username AS recorded_by_username"
            " FROM payment_records pay"
            " LEFT JOIN users u ON u.id = pay.recorded_by_user_id"
            " WHERE pay.request_id = ? ORDER BY pay.paid_date ASC, pay.id ASC",
            (request_id,),
        ).fetchall()
        total_paid = sum(float(p["amount"] or 0) for p in payment_records)
        try:
            request_amount_for_balance = float(str(pr["amount"]).replace(",", ""))
        except (TypeError, ValueError):
            request_amount_for_balance = 0.0
        remaining_balance = round(request_amount_for_balance - total_paid, 2)
        timeline_rows = conn.execute(
            """SELECT al.*, u.full_name AS actor_full_name, u.username AS actor_username
               FROM audit_logs al
               LEFT JOIN users u ON u.username = al.user_id
               WHERE al.entity_type = 'payment_request' AND al.entity_id = ?
               ORDER BY al.timestamp ASC, al.id ASC""",
            (request_id,),
        ).fetchall()

        def _describe_timeline_event(action, details_json):
            import json as _json
            try:
                details = _json.loads(details_json) if details_json else {}
            except (ValueError, TypeError):
                details = {}
            if action == "create":
                return "Request submitted", "📝"
            if action == "create_draft":
                return "Draft created", "📝"
            if action == "submit_draft":
                return "Draft submitted for approval", "📤"
            if action == "update_draft":
                return "Draft edited", "✏️"
            if action == "update":
                return "Request details updated", "✏️"
            if action == "set_account":
                return "Account classification set", "🏷️"
            if action == "set_account_split":
                return "Expense split across accounts", "🏷️"
            if action == "cancel":
                remarks = (details.get("remarks") or "").strip()
                label = "Request cancelled" + (f' — "{remarks[:120]}"' if remarks else "")
                return label, "🚫"
            if action == "status_update":
                status = str(details.get("status") or "").replace("_", " ").title()
                return (f"Status changed to {status}" if status else "Status updated"), "🔄"
            if action == "upload_receipt":
                return "Payment receipt uploaded", "🧾"
            if action == "upload_journal_basis":
                return "Journal basis file uploaded", "📒"
            if action == "set_journal_basis_from_attachment":
                return "Journal basis set from an existing attachment", "📒"
            if action == "record_payment":
                amt = details.get("amount")
                amt_label = f"{float(amt):,.2f}" if amt is not None else ""
                if details.get("fully_paid"):
                    suffix = " — fully paid"
                else:
                    rem = details.get("remaining_balance")
                    suffix = f" — {float(rem):,.2f} remaining" if rem is not None else ""
                label = f"Payment of {amt_label} recorded" + suffix
                return label, "💵"
            if action == "comment":
                body = (details.get("body") or "").strip()
                label = "Comment added" + (f': "{body[:80]}"' if body else "")
                return label, "💬"
            return action.replace("_", " ").title(), "•"

        timeline = []
        for _row in timeline_rows:
            _label, _icon = _describe_timeline_event(_row["action"], _row["details_json"])
            timeline.append({
                "timestamp": _row["timestamp"],
                "actor_name": _row["actor_full_name"] or _row["actor_username"] or _row["user_id"],
                "label": _label,
                "icon": _icon,
            })

        aging = None
        if pr["status"] in {"submitted", "for_review", "for_process", "approved"} and pr["due_date"]:
            try:
                _due = date.fromisoformat((pr["due_date"] or "")[:10])
                _days_left = (_due - date.today()).days
                if _days_left < 0:
                    aging = {"level": "critical", "text": f"{abs(_days_left)}d overdue"}
                elif _days_left <= 3:
                    aging = {"level": "warning", "text": "due today" if _days_left == 0 else f"due in {_days_left}d"}
            except ValueError:
                pass

        accounts = _get_request_form_accounts(conn)
        is_accounting_user = _is_accounting_user(user) and not _is_operations_manager(user)
        is_ops_user = _is_operations_manager(user)
        operations_notes = pr["operations_notes"]
        receipts = conn.execute(
            """SELECT pr2.*, u.full_name AS uploader_name
               FROM payment_receipts pr2
               LEFT JOIN users u ON u.id = pr2.uploaded_by_user_id
               WHERE pr2.request_id = ? ORDER BY pr2.id""",
            (request_id,),
        ).fetchall()
        similar_requests = conn.execute(
            """SELECT id, status, created_at, description
               FROM payment_requests
               WHERE id != ?
                 AND company_id = ?
                 AND lower(trim(payee_name)) = lower(trim(?))
                 AND amount = ?
                 AND status NOT IN ('cancelled', 'rejected')
                 AND ABS(JULIANDAY(date(created_at)) - JULIANDAY(date(?))) <= 30
               ORDER BY created_at DESC""",
            (request_id, pr["company_id"], pr["payee_name"], pr["amount"], (pr["created_at"] or "")[:10]),
        ).fetchall()
    # Auto-suggest GL account(s) from learned patterns (journal learning)
    # Always shown to accounting users so they can see the AI-suggested entry
    # based on the uploaded JOURNAL.xls training data.
    suggested_account = None   # single best match (legacy, kept for template compat)
    suggested_je_lines = []    # full multi-account suggestion list
    if is_accounting_user:
        try:
            from app.services.historical_journal_learning import match_all_historical_patterns
            with db() as conn:
                search_text = f"{pr['payee_name'] or ''} {pr['description'] or ''}".strip()
                company_id_int = _co_id(conn, pr["company_id"])
                matched = match_all_historical_patterns(
                    conn, description=search_text, company_id=company_id_int
                )
                if matched:
                    # Build suggested_je_lines with resolved account IDs
                    for m in matched:
                        acct_row = conn.execute(
                            "SELECT id, code, name FROM chart_of_accounts WHERE code = ? AND is_active = 1",
                            (m["account_code"],),
                        ).fetchone()
                        suggested_je_lines.append({
                            "account_id":    acct_row["id"] if acct_row else None,
                            "account_code":  m["account_code"],
                            "account_title": m["account_title"],
                            "normal_side":   m.get("normal_side", "debit"),
                            "amount_ratio":  m.get("amount_ratio", 1.0),
                            "confidence":    m.get("confidence", 0.0),
                            "source":        m.get("source", ""),
                        })
                    # Keep single-match compat for templates that use suggested_account
                    best = suggested_je_lines[0]
                    suggested_account = {
                        "code":  best["account_code"],
                        "title": best["account_title"],
                        "id":    best["account_id"],
                    }
        except Exception:
            logger.warning("JE suggestion lookup failed for request %s", request_id, exc_info=True)
            suggested_account = None
            suggested_je_lines = []

    return templates.TemplateResponse(request, "request_detail.html", {
        "r": pr, "attachments": attachments, "invoice": invoice, "receipts": receipts,
        "line_items": line_items, "accounts": accounts,
 "comments": comments,
        "can_view_comments": can_view_comments,
        "timeline": timeline,
        "aging": aging,
        "payment_records": payment_records,
        "total_paid": total_paid,
        "remaining_balance": remaining_balance,
"is_accounting": is_accounting_user,
        "is_operations_manager": is_ops_user,
        "needs_operations_approval": _needs_operations_approval(pr["request_type"], pr["amount"]),
        "message": message or None,
        "statuses": ["submitted", "for_review", "for_process", "approved", "paid", "rejected", "cancelled"],
        "operations_notes": operations_notes,
        "is_requester": int(pr["requester_user_id"] or 0) == int(user["id"] or 0),
        "similar_requests": similar_requests,
        "requester_email": pr["requester_email"] or None,
        "journal_basis_filename": pr["journal_basis_filename"],
        "suggested_account": suggested_account,
        "suggested_je_lines": suggested_je_lines,
    })


@router.post("/requests/{request_id}/classify-items", response_class=HTMLResponse)
async def request_classify_items(request: Request, request_id: int) -> Response:
    """Accounting classifies/reclassifies account codes on each line item."""
    user = _current_user(request)
    if not (_is_accounting_user(user) or _is_operations_manager(user)):
        raise HTTPException(status_code=403, detail="Only Accounting can classify line items.")
    form_data = await request.form()
    li_ids      = form_data.getlist("li_id")
    li_acct_ids = form_data.getlist("li_account_id")
    with db() as conn:
        pr = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        valid_acct_ids = {r["id"] for r in conn.execute("SELECT id FROM chart_of_accounts WHERE is_active=1").fetchall()}
        for li_id_raw, acct_raw in zip(li_ids, li_acct_ids, strict=False):
            try:
                li_id = int(li_id_raw)
                acct_id = int(acct_raw) if str(acct_raw).strip().isdigit() else 0
            except ValueError:
                continue
            resolved = acct_id if acct_id in valid_acct_ids else None
            conn.execute(
                "UPDATE request_line_items SET account_id = ? WHERE id = ? AND request_id = ?",
                (resolved, li_id, request_id),
            )
        log_action(conn, "update", "payment_request", request_id,
                   {"action": "classify_line_items"}, user_id=user["username"])
    return RedirectResponse(f"/requests/{request_id}?message=Expense+accounts+updated", status_code=303)


@router.post("/requests/{request_id}/set-account")
def request_set_account(request: Request, request_id: int,
                         account_id: int = Form(0)) -> Response:
    """Accounting assigns a single GL account to a supplier-payment request."""
    user = _current_user(request)
    if not _is_accounting_user(user):
        raise HTTPException(status_code=403, detail="Only Accounting can assign accounts.")
    with db() as conn:
        pr = conn.execute("SELECT id FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        resolved = account_id if account_id > 0 else None
        conn.execute(
            "UPDATE payment_requests SET account_id = ?, updated_at = datetime('now') WHERE id = ?",
            (resolved, request_id),
        )
        log_action(conn, "set_account", "payment_request", request_id,
                   {"account_id": resolved}, user_id=user["username"])
    return RedirectResponse(f"/requests/{request_id}?message=Account+assigned", status_code=303)


@router.post("/requests/{request_id}/set-accounts")
async def request_set_accounts(request: Request, request_id: int) -> Response:
    """Accounting splits a single payment request across multiple GL accounts.

    Some supplier payments really cover several expense categories at once
    (e.g. a 5,000 reimbursement that is actually Transportation 2,400 +
    Representation 1,000 + Employee Benefits 1,600). The single-account
    "Save account" form above can only record one account for the whole
    request, so this route accepts parallel split_account_id[]/split_amount[]
    arrays from a dynamic multi-row form, requires the amounts to add up to
    the request total, and stores the split as request_line_items rows — the
    same table already used for multi-line-item reimbursements entered at
    request creation. Once this request has line items, the page
    automatically switches to the "Classify expense accounts" view and the
    export already emits one row per account for it.
    """
    user = _current_user(request)
    if not _is_accounting_user(user):
        raise HTTPException(status_code=403, detail="Only Accounting can assign accounts.")
    form_data = await request.form()
    raw_acct_ids = form_data.getlist("split_account_id")
    raw_amounts = form_data.getlist("split_amount")

    with db() as conn:
        pr = conn.execute("SELECT id, amount FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        valid_acct_ids = {row["id"] for row in conn.execute("SELECT id FROM chart_of_accounts WHERE is_active=1").fetchall()}

        rows = []
        for acct_raw, amt_raw in zip(raw_acct_ids, raw_amounts, strict=False):
            acct_raw = (acct_raw or "").strip()
            amt_raw = (amt_raw or "").strip()
            if not acct_raw and not amt_raw:
                continue  # blank spare row left over from the dynamic form — ignore
            try:
                acct_id = int(acct_raw)
            except ValueError:
                acct_id = 0
            try:
                amount = float(amt_raw)
            except ValueError:
                amount = 0.0
            if acct_id not in valid_acct_ids or amount <= 0:
                return RedirectResponse(
                    f"/requests/{request_id}?message=Error:+each+account+row+needs+a+valid+account+and+an+amount+greater+than+0",
                    status_code=303,
                )
            rows.append({"account_id": acct_id, "amount": amount})

        if len(rows) < 2:
            return RedirectResponse(
                f"/requests/{request_id}?message=Error:+add+at+least+2+accounts+to+split+this+payment+(use+Save+account+above+for+a+single+account)",
                status_code=303,
            )

        split_total = round(sum(row["amount"] for row in rows), 2)
        request_total = round(float(pr["amount"] or 0), 2)
        if abs(split_total - request_total) > 0.01:
            return RedirectResponse(
                f"/requests/{request_id}?message=Error:+split+amounts+total+{split_total:,.2f}+but+the+request+amount+is+{request_total:,.2f}",
                status_code=303,
            )

        conn.execute("DELETE FROM request_line_items WHERE request_id = ?", (request_id,))
        for idx, row in enumerate(rows):
            conn.execute(
                "INSERT INTO request_line_items (request_id, account_id, description, amount, sort_order) VALUES (?,?,?,?,?)",
                (request_id, row["account_id"], "", row["amount"], idx),
            )
        # Once the request has line items, the single account_id column is no
        # longer the source of truth (mirrors the convention already used at
        # request-creation time — see resolved_account_id above).
        conn.execute(
            "UPDATE payment_requests SET account_id = NULL, updated_at = datetime('now') WHERE id = ?",
            (request_id,),
        )
        log_action(conn, "set_account_split", "payment_request", request_id,
                   {"splits": rows}, user_id=user["username"])
    return RedirectResponse(
        f"/requests/{request_id}?message=Payment+split+across+{len(rows)}+accounts", status_code=303
    )


@router.get("/requests/{request_id}/attachments/{attachment_id}/preview")
def request_attachment_preview(request: Request, request_id: int, attachment_id: int) -> Response:
    attachment, path = _get_request_attachment_for_user(request, request_id, attachment_id)
    content_type = attachment["content_type"] or "application/octet-stream"
    filename = attachment["filename"] or path.name
    headers = {"Content-Disposition": f'inline; filename="{filename}"'}
    return FileResponse(path, media_type=content_type, headers=headers)


@router.get("/requests/{request_id}/attachments/{attachment_id}")
def request_attachment_download(request: Request, request_id: int, attachment_id: int) -> Response:
    attachment, path = _get_request_attachment_for_user(request, request_id, attachment_id)
    return FileResponse(path, media_type=attachment["content_type"] or "application/octet-stream",
                        filename=attachment["filename"])


@router.post("/requests/{request_id}/cancel")
def request_cancel(request: Request, request_id: int, remarks: str = Form(...)) -> Response:
    user = _current_user(request)
    remarks = (remarks or "").strip()
    if not remarks:
        raise HTTPException(status_code=400, detail="Cancellation remarks are required")
    with db() as conn:
        pr = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        if pr["requester_user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Only the requester can cancel this request")
        if pr["status"] in {"paid", "cancelled"}:
            raise HTTPException(status_code=400, detail="Paid or already-cancelled requests cannot be cancelled")
        note = f"Requester cancelled: {remarks}"
        conn.execute("UPDATE payment_requests SET status = 'cancelled', accounting_notes = ?, updated_at = datetime('now') WHERE id = ?", (note, request_id))
        link = f"/requests/{request_id}"
        _notify_accounting(conn, "Request cancelled", f"Request #{request_id} for {pr['payee_name']} was cancelled by the requester. Remarks: {remarks}", link, company_id=int(pr["company_id"] or 0))
        if _needs_operations_approval(pr["request_type"], pr["amount"]):
            _notify_operations_managers(conn, "Request cancelled", f"High-value reimbursement #{request_id} was cancelled by the requester. Remarks: {remarks}", link, company_id=int(pr["company_id"] or 0))
        log_action(conn, "cancel", "payment_request", request_id, {"remarks": remarks}, user_id=user["username"])
    return RedirectResponse(f"/requests/{request_id}?message=Request%20cancelled", status_code=303)


@router.post("/requests/{request_id}/status")
async def request_update_status(request: Request, request_id: int,
                                status: str = Form(...),
                                accounting_notes: str = Form(""),
                                operations_notes: str = Form(""),
                                receipt_file: UploadFile | None = File(None)) -> Response:
    user = _current_user(request)
    is_ops = _is_operations_manager(user) and not _is_accounting_user(user)
    if not (_is_accounting_user(user) or _is_operations_manager(user)):
        raise HTTPException(status_code=403, detail="Approval access required")
    if status not in {"submitted", "for_review", "for_process", "approved", "paid", "rejected", "cancelled"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    with db() as conn:
        pr = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        if is_ops:
            if int(pr["company_id"] or 0) not in _operations_access_company_ids(conn, user):
                raise HTTPException(status_code=403, detail="Operations approval access denied for this company")
            if status not in {"approved", "rejected", "for_review"}:
                raise HTTPException(status_code=403, detail="Operations managers can only approve, reject, or send for review")
            if not _needs_operations_approval(pr["request_type"], pr["amount"]):
                raise HTTPException(status_code=403, detail="Operations approval is only required above PHP 50,000 reimbursements")
        elif _is_accounting_user(user) and user["role"] != "admin" and int(pr["company_id"] or 0) != int(user["company_id"] or 0):
            raise HTTPException(status_code=403, detail="Accounting can only update requests for their own company")

        if is_ops:
            new_ops_notes = operations_notes.strip() or None
            new_acct_notes = pr["accounting_notes"]
        else:
            new_acct_notes = accounting_notes.strip() or None
            new_ops_notes = pr["operations_notes"]

        old_acct_notes = pr["accounting_notes"]
        old_ops_notes = pr["operations_notes"]
        if pr["status"] == status and (old_acct_notes or None) == new_acct_notes and (old_ops_notes or None) == new_ops_notes:
            return RedirectResponse(f"/requests/{request_id}?message=No%20changes%20made.%20Duplicate%20click%20was%20ignored", status_code=303)

        # paid_at_sql and ops_stamp_sql are fixed literal SQL fragments
        # chosen by equality checks on status (a validated enum value) and
        # a role flag — neither fragment is derived from raw user text.
        paid_at_sql = ", paid_at = COALESCE(paid_at, datetime('now'))" if status == "paid" else ""
        ops_stamp_sql = (
            ", operations_approved_by_user_id = ?, operations_approved_at = datetime('now')"
            if is_ops and status == "approved" else ""
        )
        params: list = [status, new_acct_notes, new_ops_notes]
        if ops_stamp_sql:
            params.append(user["id"])
        params.append(request_id)
        conn.execute(
            "UPDATE payment_requests"
            " SET status = ?, accounting_notes = ?, operations_notes = ?, updated_at = datetime('now')"
            + paid_at_sql + ops_stamp_sql +
            " WHERE id = ?",
            tuple(params),
        )
        if pr["invoice_id"] and status in {"for_process", "approved", "paid"}:
            new_invoice_status = "reviewed" if status in {"approved", "paid"} else "unmatched"
            conn.execute("UPDATE invoices SET status = ? WHERE id = ? AND status NOT IN ('matched_payment','posted')", (new_invoice_status, pr["invoice_id"]))

        status_label = status.replace('_', ' ').title()
        note_for_requester = new_ops_notes if is_ops else new_acct_notes
        note_suffix = f' Note: "{note_for_requester[:120]}"' if note_for_requester else ""
        if status == "approved":
            requester_title = "Request approved ✅"
            accounting_title = "Request approved"
        elif status == "paid":
            requester_title = "Request paid 💸"
            accounting_title = "Request paid"
        elif status == "rejected":
            requester_title = "Request rejected ❌"
            accounting_title = "Request rejected"
        elif status == "for_review":
            requester_title = "Request sent for review 🔍"
            accounting_title = "Request sent for review"
        else:
            requester_title = "Request status updated"
            accounting_title = "Request status updated"
        requester_body = f"Your request for {pr['payee_name']} is now {status_label}.{note_suffix}"
        _notify_once(conn, pr["requester_user_id"], requester_title, requester_body, f"/requests/{request_id}")
        dept_id = pr["department_id"]
        _notify_requesting_department(conn, request_id, pr["company_id"] or 0, dept_id, pr["requester_user_id"], requester_title, requester_body)
        if status in {"approved", "rejected", "paid", "for_review", "for_process"}:
            _notify_accounting(conn, accounting_title, f"Request #{request_id} for {pr['payee_name']} is now {status_label}.", f"/requests/{request_id}", company_id=pr["company_id"] or 0)

        receipt_stored_path: str | None = None
        if status == "paid" and receipt_file and receipt_file.filename:
            receipt_bytes = _check_upload_size(receipt_file)
            REQUEST_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            safe_name = Path(receipt_file.filename).name
            stored = REQUEST_UPLOAD_DIR / f"receipt_{request_id}_{safe_name}"
            with stored.open("wb") as out:
                out.write(receipt_bytes)
            receipt_stored_path = str(stored)
            conn.execute(
                """INSERT INTO payment_receipts
                   (request_id, filename, stored_path, content_type, file_size, uploaded_by_user_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (request_id, safe_name, receipt_stored_path,
                 _resolve_content_type(receipt_file.content_type, safe_name), stored.stat().st_size, user["id"]),
            )

        log_action(conn, "status_update", "payment_request", request_id, {"status": status}, user_id=user["username"])

        # ── Auto-generate draft JE when request is approved or for_process ───
        if status in {"approved", "for_process"}:
            from app.services.request_workflow import auto_create_draft_je
            auto_create_draft_je(conn, pr)

        requester_email = (pr["requester_email"] or "").strip()
        if requester_email:
            requester_name_row = conn.execute("SELECT full_name FROM users WHERE id = ?", (pr["requester_user_id"],)).fetchone()
            first_name = (requester_name_row["full_name"] or "there").split()[0] if requester_name_row else "there"
            link = _absolute_app_link(f"/requests/{request_id}")
            email_body = (
                f"Hi {first_name},\n\n"
                f"Your {pr['request_type'].replace('_', ' ')} request for {pr['payee_name']} "
                f"({pr['amount']} PHP) is now {status_label}.\n"
            )
            if note_for_requester:
                email_body += f"\nNote from reviewer:\n{note_for_requester}\n"
            email_body += f"\nView your request: {link}\n\nBookPoint"
            _send_email_notification(
                requester_email,
                f"BookPoint: {requester_title} — Request #{request_id}",
                email_body,
            )

    return RedirectResponse(f"/requests/{request_id}?message=Status%20updated", status_code=303)


@router.post("/requests/bulk-status")
async def request_bulk_update_status(request: Request,
                                     request_ids: list[int] = Form(...),
                                     status: str = Form(...),
                                     accounting_notes: str = Form(""),
                                     operations_notes: str = Form(""),
                                     company_id: int = Form(0)) -> Response:
    """Approve or reject multiple requests at once. Reuses the exact same
    per-request validation, notification, receipt/JE, and audit-log logic
    as request_update_status() above by calling it directly for each id —
    nothing here duplicates that logic."""
    if status not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="Bulk actions only support approve or reject")

    succeeded: list[int] = []
    failed: list[tuple[int, str]] = []
    for rid in request_ids:
        try:
            await request_update_status(
                request, rid,
                status=status,
                accounting_notes=accounting_notes,
                operations_notes=operations_notes,
                receipt_file=None,
            )
            succeeded.append(rid)
        except HTTPException as exc:
            failed.append((rid, str(exc.detail)))

    from urllib.parse import quote
    label = "approved" if status == "approved" else "rejected"
    msg = f"{len(succeeded)} request(s) {label}."
    if failed:
        msg += f" {len(failed)} could not be updated (e.g. #{failed[0][0]}: {failed[0][1]})."
    qs = f"company_id={company_id}&message={quote(msg)}" if company_id else f"message={quote(msg)}"
    return RedirectResponse(f"/requests?{qs}", status_code=303)


@router.post("/requests/{request_id}/upload-receipt", response_class=HTMLResponse)
async def upload_payment_receipt(request: Request, request_id: int,
                                  receipt_file: UploadFile = File(...)) -> Response:
    """Accounting can upload receipt(s) on already-paid requests at any time."""
    user = _current_user(request)
    if not _is_accounting_user(user):
        raise HTTPException(status_code=403, detail="Only Accounting can upload payment receipts")
    if not receipt_file.filename:
        return RedirectResponse(f"/requests/{request_id}?message=No+file+selected", status_code=303)
    receipt_bytes = _check_upload_size(receipt_file)
    with db() as conn:
        pr = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        if pr["status"] not in {"paid", "approved", "for_process"}:
            raise HTTPException(status_code=400, detail="Receipts can only be uploaded for paid or approved requests")
        REQUEST_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = Path(receipt_file.filename).name
        stored = REQUEST_UPLOAD_DIR / f"receipt_{request_id}_{safe_name}"
        with stored.open("wb") as out:
            out.write(receipt_bytes)
        _rcur = conn.execute(
            """INSERT INTO payment_receipts
               (request_id, filename, stored_path, content_type, file_size, uploaded_by_user_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (request_id, safe_name, str(stored), _resolve_content_type(receipt_file.content_type, safe_name), stored.stat().st_size, user["id"]),
        )
        receipt_id = _rcur.lastrowid
        _notify_once(conn, pr["requester_user_id"], "Payment receipt uploaded 🧾",
                     f"A payment receipt for your {pr['request_type'].replace('_', ' ')} request "
                     f"(#{request_id} — {pr['payee_name']}) has been uploaded by Accounting.",
                     f"/requests/{request_id}")
        requester_email = (pr["requester_email"] or "").strip()
        if requester_email:
            requester_name_row = conn.execute("SELECT full_name FROM users WHERE id = ?", (pr["requester_user_id"],)).fetchone()
            name = (requester_name_row["full_name"] or "there").split()[0] if requester_name_row else "there"
            link = _absolute_app_link(f"/requests/{request_id}")
            email_body = (
                f"Hi {name},\n\n"
                f"Accounting has uploaded a payment receipt for your request:\n"
                f"  Request: #{request_id} — {pr['payee_name']}\n"
                f"  Amount : {pr['amount']} PHP\n\n"
                f"You can view or download your receipt here: {link}\n\nBookPoint"
            )
            _send_email_notification(
                requester_email,
                f"BookPoint: Payment receipt for request #{request_id}",
                email_body,
            )
        log_action(conn, "upload_receipt", "payment_request", request_id, {"receipt_id": receipt_id}, user_id=user["username"])
    return RedirectResponse(f"/requests/{request_id}?message=Receipt+uploaded+and+requester+notified", status_code=303)


@router.post("/requests/{request_id}/upload-journal", response_class=HTMLResponse)
async def upload_journal_basis(request: Request, request_id: int,
                               journal_file: UploadFile = File(...)) -> Response:
    """Accounting uploads the journal/JV that serves as the accounting basis for this request."""
    user = _current_user(request)
    if not _is_accounting_user(user):
        raise HTTPException(status_code=403, detail="Only Accounting can upload journal basis documents")
    if not journal_file.filename:
        return RedirectResponse(f"/requests/{request_id}?message=No+file+selected", status_code=303)
    journal_bytes = _check_upload_size(journal_file)
    with db() as conn:
        pr = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        REQUEST_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = Path(journal_file.filename).name
        stored = REQUEST_UPLOAD_DIR / f"journal_{request_id}_{safe_name}"
        # Remove old journal file if one existed
        old_path = pr["journal_basis_path"]
        if old_path:
            with contextlib.suppress(OSError):
                Path(old_path).unlink(missing_ok=True)
        with stored.open("wb") as out:
            out.write(journal_bytes)
        conn.execute(
            "UPDATE payment_requests SET journal_basis_filename = ?, journal_basis_path = ?, updated_at = datetime('now') WHERE id = ?",
            (safe_name, str(stored), request_id),
        )
        log_action(conn, "upload_journal_basis", "payment_request", request_id, {"filename": safe_name}, user_id=user["username"])
    return RedirectResponse(f"/requests/{request_id}?message=Journal+basis+uploaded", status_code=303)


@router.post("/requests/{request_id}/use-attachment-as-journal/{attachment_id}")
def use_attachment_as_journal(request: Request, request_id: int, attachment_id: int) -> Response:
    """Promote one of the requester's uploaded attachments to be the journal basis document."""
    user = _current_user(request)
    if not _is_accounting_user(user):
        raise HTTPException(status_code=403, detail="Only Accounting can set the journal basis")
    with db() as conn:
        pr = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        att = conn.execute(
            "SELECT * FROM request_attachments WHERE id = ? AND request_id = ?",
            (attachment_id, request_id),
        ).fetchone()
        if att is None:
            raise HTTPException(status_code=404, detail="Attachment not found")

        att_path = Path(att["stored_path"] or att["filename"] or "").resolve()
        if not att_path.exists():
            raise HTTPException(status_code=404, detail="Attachment file is missing")

        # Copy the attachment to a journal basis path so it's independently stored
        safe_name = att["filename"] or att_path.name
        stored = REQUEST_UPLOAD_DIR / f"journal_{request_id}_{safe_name}"

        # Remove old journal file if one existed
        old_path = pr["journal_basis_path"]
        if old_path:
            with contextlib.suppress(OSError):
                Path(old_path).unlink(missing_ok=True)

        shutil.copy2(att_path, stored)

        conn.execute(
            "UPDATE payment_requests SET journal_basis_filename = ?, journal_basis_path = ?, updated_at = datetime('now') WHERE id = ?",
            (safe_name, str(stored), request_id),
        )
        log_action(conn, "set_journal_basis_from_attachment", "payment_request", request_id,
                   {"attachment_id": attachment_id, "filename": safe_name}, user_id=user["username"])
    return RedirectResponse(
        f"/requests/{request_id}?message=Journal+basis+set+from+attachment",
        status_code=303,
    )


@router.get("/requests/{request_id}/journal-basis")
def download_journal_basis(request: Request, request_id: int) -> Response:
    """Download the accounting journal basis document for a request."""
    user = _current_user(request)
    with db() as conn:
        pr = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        if not _can_access_request(conn, user, pr):
            raise HTTPException(status_code=403, detail="Access denied")
    journal_path = pr["journal_basis_path"]
    journal_name = pr["journal_basis_filename"]
    if not journal_path:
        raise HTTPException(status_code=404, detail="No journal basis document uploaded yet")
    path = Path(journal_path).resolve()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Journal basis file not found on server")
    return FileResponse(str(path), filename=journal_name or path.name)


@router.get("/requests/{request_id}/receipts/{receipt_id}")
def download_payment_receipt(request: Request, request_id: int, receipt_id: int) -> Response:
    user = _current_user(request)
    with db() as conn:
        pr = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        if not _can_access_request(conn, user, pr):
            raise HTTPException(status_code=403, detail="Access denied")
        rec = conn.execute(
            "SELECT * FROM payment_receipts WHERE id = ? AND request_id = ?", (receipt_id, request_id)
        ).fetchone()
        if rec is None:
            raise HTTPException(status_code=404, detail="Receipt not found")
    rec_path = Path(rec["stored_path"] or "").resolve()
    try:
        rec_path.relative_to(REQUEST_UPLOAD_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid receipt path")
    if not rec_path.exists():
        raise HTTPException(status_code=404, detail="Receipt file is missing")
    return FileResponse(rec_path, filename=rec["filename"],
                        media_type=rec["content_type"] or "application/octet-stream")


@router.get("/requests/{request_id}/receipts/{receipt_id}/preview")
def preview_payment_receipt(request: Request, request_id: int, receipt_id: int) -> Response:
    """Serve receipt inline so it can be embedded in an <img> or <iframe>."""
    user = _current_user(request)
    with db() as conn:
        pr = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        if not _can_access_request(conn, user, pr):
            raise HTTPException(status_code=403, detail="Access denied")
        rec = conn.execute(
            "SELECT * FROM payment_receipts WHERE id = ? AND request_id = ?", (receipt_id, request_id)
        ).fetchone()
        if rec is None:
            raise HTTPException(status_code=404, detail="Receipt not found")
    rec_path = Path(rec["stored_path"] or "").resolve()
    try:
        rec_path.relative_to(REQUEST_UPLOAD_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid receipt path")
    if not rec_path.exists():
        raise HTTPException(status_code=404, detail="Receipt file is missing")
    content_type = rec["content_type"] or "application/octet-stream"
    headers = {"Content-Disposition": f'inline; filename="{rec["filename"]}"'}
    return FileResponse(rec_path, media_type=content_type, headers=headers)



# Invoice routes moved to invoices.py
@router.post("/requests/{request_id}/comments")
async def request_add_comment(request: Request, request_id: int,
                               body: str = Form(...)) -> Response:
    user = _current_user(request)
    body = body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="Comment cannot be empty")

    with db() as conn:
        pr = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (request_id,)).fetchone()
        if pr is None:
            raise HTTPException(status_code=404, detail="Request not found")
        if not _can_access_request(conn, user, pr):
            raise HTTPException(status_code=403, detail="Request access denied")
        if (
            not _is_accounting_user(user)
            and int(pr["requester_user_id"] or 0) != int(user["id"] or 0)
        ):
            raise HTTPException(status_code=403, detail="Only Accounting and the requester can comment on this request")

        conn.execute(
            "INSERT INTO request_comments (request_id, author_user_id, body) VALUES (?, ?, ?)",
            (request_id, user["id"], body),
        )
        log_action(conn, "comment", "payment_request", request_id, {"body": body}, user_id=user["username"])

        link = f"/requests/{request_id}"
        commenter_is_accounting_or_ops = _is_accounting_user(user) or _is_operations_manager(user)
        commenter_is_requester_side = (
            int(pr["requester_user_id"] or 0) == int(user["id"] or 0)
            or (pr["department_id"] and pr["department_id"] == user["department_id"])
        )

        if commenter_is_accounting_or_ops:
            _notify_once(
                conn, pr["requester_user_id"],
                "New comment on your request",
                f"{user['full_name'] or user['username']} commented on request #{request_id} for {pr['payee_name']}.",
                link, send_email=False,
            )
        elif commenter_is_requester_side:
            _notify_accounting(
                conn, "New comment on a request",
                f"{user['full_name'] or user['username']} commented on request #{request_id} for {pr['payee_name']}.",
                link, company_id=pr["company_id"] or 0,
            )
            if _needs_operations_approval(pr["request_type"], pr["amount"]):
                _notify_operations_managers(
                    conn, "New comment on a request",
                    f"{user['full_name'] or user['username']} commented on request #{request_id} for {pr['payee_name']}.",
                    link, company_id=pr["company_id"] or 0,
                )

    return RedirectResponse(f"/requests/{request_id}?message=Comment+added", status_code=303)