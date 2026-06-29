"""app/routers/accounting.py — Journal entries, suppliers, classification rules, account titles."""
from __future__ import annotations

import contextlib
import re as _re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import require_admin
from app.db import db, log_action
from app.deps import _current_user, _parse_account_titles_file, templates

router = APIRouter()


# ---------- Journal Entries --------------------------------------------------

@router.get("/journal-entries", response_class=HTMLResponse)
def list_journal_entries(request: Request, status: str = "draft",
                         company_id: int = 0, limit: int = 200) -> Response:
    with db() as conn:
        companies = conn.execute(
            "SELECT id, name FROM companies WHERE is_active = 1 ORDER BY id"
        ).fetchall()
        rows = conn.execute(
            """SELECT je.*,
                      c.name AS company_name,
                      (SELECT SUM(CAST(debit AS REAL)) FROM journal_entry_lines
                          WHERE journal_entry_id = je.id) AS total_debit,
                      (SELECT SUM(CAST(credit AS REAL)) FROM journal_entry_lines
                          WHERE journal_entry_id = je.id) AS total_credit
               FROM journal_entries je
               LEFT JOIN companies c ON c.id = je.company_id
               WHERE (? = '' OR je.status = ?)
                 AND (? = 0 OR je.company_id = ?)
               ORDER BY je.entry_date DESC, je.id DESC
               LIMIT ?""", (status, status, company_id, company_id, limit),
        ).fetchall()
    return templates.TemplateResponse(request, "journal_entries.html", {
        "rows": rows,
        "status": status,
        "companies": companies,
        "active_company_id": company_id,
    })


@router.get("/journal-entries/{je_id}", response_class=HTMLResponse)
def je_detail(request: Request, je_id: int) -> Response:
    with db() as conn:
        je = conn.execute("SELECT * FROM journal_entries WHERE id = ?", (je_id,)).fetchone()
        if je is None:
            raise HTTPException(404, f"Journal entry {je_id} not found")
        lines = conn.execute(
            """SELECT l.*, a.code AS account_code, a.name AS account_name
               FROM journal_entry_lines l
               JOIN chart_of_accounts a ON a.id = l.account_id
               WHERE l.journal_entry_id = ?
               ORDER BY l.line_order""", (je_id,)
        ).fetchall()
    return templates.TemplateResponse(request, "journal_entry_detail.html", {"je": je, "lines": lines})


@router.post("/journal-entries/{je_id}/approve")
def approve_je(request: Request, je_id: int) -> Response:
    require_admin(request)
    user = getattr(request.state, "user", None)
    username = user["username"] if user else "unknown"
    with db() as conn:
        je = conn.execute("SELECT * FROM journal_entries WHERE id = ?", (je_id,)).fetchone()
        if je is None:
            raise HTTPException(404, "JE not found")
        if je["status"] != "draft":
            raise HTTPException(400, f"JE is in status {je['status']}; only draft JEs can be approved")

        # Block approval if any line debits account 1202 (Advances to Officers and
        # Employees) and there is no supporting invoice/attachment on the linked
        # payment request.  Advances require a physical document before posting.
        advance_lines = conn.execute(
            """SELECT l.id
               FROM journal_entry_lines l
               JOIN chart_of_accounts a ON a.id = l.account_id
               WHERE l.journal_entry_id = ?
                 AND a.code = '1202'
                 AND CAST(l.debit AS REAL) > 0""",
            (je_id,),
        ).fetchall()
        if advance_lines:
            pr_id = je.get("payment_request_id", None)
            has_attachment = False
            if pr_id:
                has_attachment = bool(conn.execute(
                    "SELECT 1 FROM request_attachments WHERE request_id = ? LIMIT 1",
                    (pr_id,),
                ).fetchone())
            if not has_attachment:
                raise HTTPException(
                    400,
                    "Cannot approve: account 1202 (Advances to Officers and Employees) "
                    "requires a supporting invoice or attachment. Please attach a document "
                    "to the linked payment request before approving.",
                )

        conn.execute("UPDATE journal_entries SET status='approved' WHERE id = ?", (je_id,))
        log_action(conn, "approve", "journal_entry", je_id,
                   {"previous_status": "draft", "new_status": "approved", "approved_by": username})
    return RedirectResponse(f"/journal-entries/{je_id}", status_code=303)


@router.post("/journal-entries/{je_id}/reject")
def reject_je(request: Request, je_id: int, remarks: str = Form("")) -> Response:
    require_admin(request)
    user = getattr(request.state, "user", None)
    username = user["username"] if user else "unknown"
    with db() as conn:
        je = conn.execute("SELECT * FROM journal_entries WHERE id = ?", (je_id,)).fetchone()
        if je is None:
            raise HTTPException(404, "JE not found")
        if je["status"] not in ("draft", "approved"):
            raise HTTPException(400, f"JE is in status {je['status']}; cannot reject")
        conn.execute("UPDATE journal_entries SET status='rejected' WHERE id = ?", (je_id,))
        log_action(conn, "reject", "journal_entry", je_id,
                   {"previous_status": je["status"], "new_status": "rejected",
                    "rejected_by": username, "remarks": remarks})
    return RedirectResponse(f"/journal-entries/{je_id}", status_code=303)


# ---------- Journal Learning Upload -----------------------------------------

@router.post("/journal-learning/upload")
async def upload_journal_learning(
    request: Request,
    company_id: int = Form(...),
    file: UploadFile = File(...),
) -> Response:
    """Admin-only: upload a journal file (XLS/XLSX) to train account-matching patterns."""
    require_admin(request)
    if not file.filename:
        raise HTTPException(400, "No file provided")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".xls", ".xlsx", ".xlsm"):
        raise HTTPException(400, "Only .xls / .xlsx / .xlsm files are accepted")

    import os as _os
    from pathlib import Path as _Path

    from app.services.historical_journal_learning import learn_from_historical_journal

    # Use the same persistent-volume root as the database and uploads.
    # On Fly.io this is /data/; locally it falls back to <project>/data/.
    _data_root = _Path(_os.environ.get("ACCOUNTING_DB", "data/accounting.db")).parent
    save_dir = _data_root / "journal_learning" / str(company_id)
    save_dir.mkdir(parents=True, exist_ok=True)
    stored_path = save_dir / file.filename

    contents = await file.read()
    stored_path.write_bytes(contents)

    with db() as conn:
        learned_lines = learn_from_historical_journal(
            conn,
            path=stored_path,
            company_id=company_id,
            filename=file.filename,
            stored_path=str(stored_path),
        )

    pattern_count = len(learned_lines) if learned_lines else 0
    return RedirectResponse(
        f"/journal-entries?message=Journal+learned%3A+{pattern_count}+patterns+saved",
        status_code=303,
    )


# ---------- Suppliers --------------------------------------------------------

@router.get("/suppliers", response_class=HTMLResponse)
def list_suppliers(request: Request, message: str = "", error: str = "") -> Response:
    user = _current_user(request)
    with db() as conn:
        rows = conn.execute(
            """SELECT s.id, s.name, s.tin, s.is_active,
                      s.default_expense_account_id,
                      a.code AS default_expense_account_code,
                      a.name AS default_expense_account_name,
                      (SELECT COUNT(*) FROM bank_transactions WHERE supplier_id = s.id) AS tx_count
               FROM suppliers s
               LEFT JOIN chart_of_accounts a ON a.id = s.default_expense_account_id
               ORDER BY s.is_active DESC, s.name"""
        ).fetchall()
        aliases = conn.execute(
            "SELECT id, supplier_id, alias FROM supplier_aliases ORDER BY supplier_id, alias"
        ).fetchall()
        accounts = conn.execute(
            """SELECT id, code, name FROM chart_of_accounts
               WHERE is_active = 1 AND type = 'expense'
               ORDER BY code"""
        ).fetchall()
    aliases_by_sup: dict[int, list] = {}
    for a in aliases:
        aliases_by_sup.setdefault(a["supplier_id"], []).append({"id": a["id"], "alias": a["alias"]})
    return templates.TemplateResponse(request, "suppliers.html", {
        "rows": rows,
        "aliases_by_sup": aliases_by_sup,
        "accounts": accounts,
        "message": message or None,
        "error": error or None,
        "current_user": user,
    })


@router.post("/suppliers/create")
def create_supplier(request: Request,
                    name: str = Form(...),
                    tin: str = Form(""),
                    default_expense_account_id: int = Form(0),
                    aliases: str = Form(""),
                    is_active: str = Form("1")) -> Response:
    user = require_admin(request)
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Supplier name is required")
    account_id = default_expense_account_id or None
    active = 1 if is_active == "1" else 0
    alias_list = [a.strip() for a in aliases.replace("\n", ",").split(",") if a.strip()]
    with db() as conn:
        if account_id is not None:
            acct = conn.execute("SELECT id FROM chart_of_accounts WHERE id = ? AND type = 'expense'", (account_id,)).fetchone()
            if acct is None:
                raise HTTPException(status_code=400, detail="Default account must be an active expense account")
        try:
            cur = conn.execute(
                """INSERT INTO suppliers (name, tin, default_expense_account_id, is_active)
                   VALUES (?, ?, ?, ?)""",
                (name, tin.strip() or None, account_id, active),
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"Supplier could not be created: {exc}") from exc
        supplier_id = cur.lastrowid
        for alias in alias_list:
            conn.execute("INSERT OR IGNORE INTO supplier_aliases (supplier_id, alias) VALUES (?, ?)", (supplier_id, alias))
        log_action(conn, "create", "supplier", supplier_id,
                   {"name": name, "aliases": alias_list},
                   user_id=(user["username"] if user else "system"))
    return RedirectResponse("/suppliers?message=Supplier%20created", status_code=303)


@router.post("/suppliers/{supplier_id}/update")
def update_supplier(request: Request,
                    supplier_id: int,
                    name: str = Form(...),
                    tin: str = Form(""),
                    default_expense_account_id: int = Form(0),
                    is_active: str = Form("0")) -> Response:
    user = getattr(request.state, "user", None)
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Supplier name is required")
    account_id = default_expense_account_id or None
    active = 1 if is_active == "1" else 0
    with db() as conn:
        existing = conn.execute("SELECT id FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Supplier not found")
        if account_id is not None:
            acct = conn.execute("SELECT id FROM chart_of_accounts WHERE id = ? AND type = 'expense'", (account_id,)).fetchone()
            if acct is None:
                raise HTTPException(status_code=400, detail="Default account must be an active expense account")
        try:
            conn.execute(
                """UPDATE suppliers
                   SET name = ?, tin = ?, default_expense_account_id = ?, is_active = ?
                   WHERE id = ?""",
                (name, tin.strip() or None, account_id, active, supplier_id),
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"Supplier could not be updated: {exc}") from exc
        log_action(conn, "update", "supplier", supplier_id,
                   {"name": name, "is_active": active},
                   user_id=(user["username"] if user else "system"))
    return RedirectResponse("/suppliers?message=Supplier%20updated", status_code=303)


@router.post("/suppliers/{supplier_id}/delete")
def delete_supplier(request: Request, supplier_id: int) -> Response:
    user = require_admin(request)
    with db() as conn:
        existing = conn.execute("SELECT name FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Supplier not found")
        conn.execute("UPDATE suppliers SET is_active = 0 WHERE id = ?", (supplier_id,))
        log_action(conn, "deactivate", "supplier", supplier_id,
                   {"name": existing["name"], "is_active": 0},
                   user_id=user["username"])
    return RedirectResponse("/suppliers?message=Supplier+deactivated", status_code=303)


@router.post("/suppliers/{supplier_id}/aliases/add")
def add_supplier_alias(request: Request, supplier_id: int, alias: str = Form(...)) -> Response:
    user = require_admin(request)
    alias = alias.strip().upper()
    if not alias:
        return RedirectResponse("/suppliers?error=Alias+cannot+be+empty", status_code=303)
    with db() as conn:
        if conn.execute("SELECT id FROM suppliers WHERE id = ?", (supplier_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Supplier not found")
        try:
            conn.execute(
                "INSERT INTO supplier_aliases (supplier_id, alias) VALUES (?, ?)",
                (supplier_id, alias),
            )
        except Exception:
            return RedirectResponse(
                "/suppliers?error=Alias+already+exists+for+this+supplier", status_code=303
            )
        log_action(conn, "add_alias", "supplier", supplier_id,
                   {"alias": alias},
                   user_id=(user["username"] if user else "system"))
    return RedirectResponse("/suppliers?message=Alias+added", status_code=303)


@router.post("/suppliers/{supplier_id}/aliases/{alias_id}/delete")
def delete_supplier_alias(request: Request, supplier_id: int, alias_id: int) -> Response:
    user = require_admin(request)
    with db() as conn:
        existing = conn.execute(
            "SELECT alias FROM supplier_aliases WHERE id = ? AND supplier_id = ?",
            (alias_id, supplier_id),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Alias not found")
        conn.execute("DELETE FROM supplier_aliases WHERE id = ?", (alias_id,))
        log_action(conn, "delete_alias", "supplier", supplier_id,
                   {"alias": existing["alias"]},
                   user_id=(user["username"] if user else "system"))
    return RedirectResponse("/suppliers?message=Alias+removed", status_code=303)


# ---------- Classification Rules CRUD ----------------------------------------

@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, message: str = "", error: str = "") -> Response:
    require_admin(request)
    with db() as conn:
        rules = conn.execute(
            """SELECT r.*, a.code AS account_code, a.name AS account_name
               FROM classification_rules r
               LEFT JOIN chart_of_accounts a ON a.id = r.target_account_id
               ORDER BY r.priority, r.id"""
        ).fetchall()
        accounts = conn.execute(
            "SELECT id, code, name, type FROM chart_of_accounts WHERE is_active = 1 ORDER BY code"
        ).fetchall()
    return templates.TemplateResponse(request, "rules.html", {
        "rules": rules,
        "accounts": accounts,
        "message": message or None,
        "error": error or None,
    })


@router.post("/rules/create", response_class=HTMLResponse)
def rules_create(request: Request,
                 pattern: str = Form(...),
                 pattern_type: str = Form("keyword"),
                 target_account_id: int = Form(...),
                 direction: str = Form("auto"),
                 priority: int = Form(50),
                 description: str = Form("")) -> Response:
    require_admin(request)
    pattern = pattern.strip()
    if not pattern:
        return RedirectResponse("/rules?error=Pattern+is+required", status_code=303)
    with db() as conn:
        conn.execute(
            """INSERT INTO classification_rules
                 (pattern, pattern_type, target_account_id, direction, priority, description, is_active)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (pattern, pattern_type, target_account_id, direction, priority, description.strip()),
        )
        log_action(conn, "create", "classification_rule", None,
                   {"pattern": pattern, "pattern_type": pattern_type},
                   user_id=getattr(request.state, "user", {}).get("username", "admin"))
    return RedirectResponse("/rules?message=Rule+created", status_code=303)


@router.post("/rules/{rule_id}/update", response_class=HTMLResponse)
def rules_update(request: Request,
                 rule_id: int,
                 pattern: str = Form(...),
                 pattern_type: str = Form("keyword"),
                 target_account_id: int = Form(...),
                 direction: str = Form("auto"),
                 priority: int = Form(50),
                 description: str = Form("")) -> Response:
    require_admin(request)
    pattern = pattern.strip()
    if not pattern:
        return RedirectResponse("/rules?error=Pattern+is+required", status_code=303)
    with db() as conn:
        conn.execute(
            """UPDATE classification_rules
               SET pattern = ?, pattern_type = ?, target_account_id = ?,
                   direction = ?, priority = ?, description = ?
               WHERE id = ?""",
            (pattern, pattern_type, target_account_id, direction, priority, description.strip(), rule_id),
        )
        log_action(conn, "update", "classification_rule", rule_id,
                   {"pattern": pattern},
                   user_id=getattr(request.state, "user", {}).get("username", "admin"))
    return RedirectResponse("/rules?message=Rule+updated", status_code=303)


@router.post("/rules/{rule_id}/toggle", response_class=HTMLResponse)
def rules_toggle(request: Request, rule_id: int) -> Response:
    require_admin(request)
    with db() as conn:
        rule = conn.execute("SELECT is_active FROM classification_rules WHERE id = ?", (rule_id,)).fetchone()
        if rule is None:
            raise HTTPException(404, "Rule not found")
        new_state = 0 if rule["is_active"] else 1
        conn.execute("UPDATE classification_rules SET is_active = ? WHERE id = ?", (new_state, rule_id))
    return RedirectResponse("/rules?message=Rule+toggled", status_code=303)


@router.post("/rules/{rule_id}/delete", response_class=HTMLResponse)
def rules_delete(request: Request, rule_id: int) -> Response:
    require_admin(request)
    with db() as conn:
        conn.execute("DELETE FROM classification_rules WHERE id = ?", (rule_id,))
        log_action(conn, "delete", "classification_rule", rule_id, {},
                   user_id=getattr(request.state, "user", {}).get("username", "admin"))
    return RedirectResponse("/rules?message=Rule+deleted", status_code=303)


@router.post("/rules/test", response_class=HTMLResponse)
def rules_test(request: Request, description: str = Form("")) -> Response:
    """Test which rule would match a given bank transaction description."""
    require_admin(request)
    desc = description.strip()
    matches = []
    with db() as conn:
        rules = conn.execute(
            """SELECT r.*, a.code AS account_code, a.name AS account_name
               FROM classification_rules r
               LEFT JOIN chart_of_accounts a ON a.id = r.target_account_id
               WHERE r.is_active = 1
               ORDER BY r.priority, r.id"""
        ).fetchall()
        accounts = conn.execute(
            "SELECT id, code, name, type FROM chart_of_accounts WHERE is_active = 1 ORDER BY code"
        ).fetchall()
    for rule in rules:
        try:
            if rule["pattern_type"] == "regex":
                hit = bool(_re.search(rule["pattern"], desc, _re.IGNORECASE))
            else:
                hit = rule["pattern"].lower() in desc.lower()
        except Exception:
            hit = False
        matches.append({"rule": rule, "hit": hit})
    first_hit = next((m for m in matches if m["hit"]), None)
    return templates.TemplateResponse(request, "rules.html", {
        "rules": rules,
        "accounts": accounts,
        "message": None,
        "error": None,
        "test_description": desc,
        "test_matches": matches,
        "test_first_hit": first_hit,
    })


# ---------- Account Titles / Chart of Accounts --------------------------------

@router.get("/account-titles", response_class=HTMLResponse)
def account_titles_page(request: Request, message: str = "", error: str = "") -> Response:
    with db() as conn:
        accounts = conn.execute("SELECT * FROM chart_of_accounts ORDER BY code").fetchall()
    return templates.TemplateResponse(request, "account_titles.html", {
        "accounts": accounts,
        "message": message,
        "error": error,
    })


@router.post("/account-titles/upload", response_class=HTMLResponse)
async def account_titles_upload(request: Request, file: UploadFile = File(...)) -> Response:
    require_admin(request)
    tmp = Path(tempfile.mkdtemp()) / Path(file.filename).name
    try:
        with tmp.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        rows = _parse_account_titles_file(tmp)
        with db() as conn:
            for code, name, account_type, is_active in rows:
                conn.execute(
                    """INSERT INTO chart_of_accounts (code, name, type, is_active)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(code) DO UPDATE SET
                         name=excluded.name, type=excluded.type, is_active=excluded.is_active""",
                    (code, name, account_type, is_active),
                )
            log_action(conn, "upload", "chart_of_accounts", None, {"filename": file.filename, "rows": len(rows)})
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            tmp.parent.rmdir()
    return RedirectResponse(f"/account-titles?message={len(rows)}%20account%20titles%20uploaded", status_code=303)


@router.post("/account-titles/create", response_class=HTMLResponse)
def account_titles_create(request: Request,
                           code: str = Form(...),
                           name: str = Form(...),
                           acct_type: str = Form("expense")) -> Response:
    require_admin(request)
    code = code.strip()
    name = name.strip()
    valid_types = {"asset", "liability", "equity", "income", "expense"}
    if not code or not name:
        return RedirectResponse("/account-titles?error=Code+and+name+are+required", status_code=303)
    if acct_type not in valid_types:
        return RedirectResponse("/account-titles?error=Invalid+account+type", status_code=303)
    with db() as conn:
        existing = conn.execute("SELECT id FROM chart_of_accounts WHERE code = ?", (code,)).fetchone()
        if existing:
            return RedirectResponse(f"/account-titles?error=Account+code+{code}+already+exists", status_code=303)
        conn.execute(
            "INSERT INTO chart_of_accounts (code, name, type, is_active) VALUES (?, ?, ?, 1)",
            (code, name, acct_type),
        )
        log_action(conn, "create", "chart_of_accounts", None, {"code": code, "name": name, "type": acct_type},
                   user_id=getattr(request.state, "user", {}).get("username", "admin"))
    return RedirectResponse("/account-titles?message=Account+created", status_code=303)


@router.post("/account-titles/{account_id}/update", response_class=HTMLResponse)
def account_titles_update(request: Request,
                           account_id: int,
                           code: str = Form(...),
                           name: str = Form(...),
                           acct_type: str = Form("expense"),
                           is_active: str = Form("1")) -> Response:
    require_admin(request)
    code = code.strip()
    name = name.strip()
    valid_types = {"asset", "liability", "equity", "income", "expense"}
    if not code or not name:
        return RedirectResponse("/account-titles?error=Code+and+name+are+required", status_code=303)
    if acct_type not in valid_types:
        return RedirectResponse("/account-titles?error=Invalid+account+type", status_code=303)
    with db() as conn:
        conn.execute(
            "UPDATE chart_of_accounts SET code = ?, name = ?, type = ?, is_active = ? WHERE id = ?",
            (code, name, acct_type, 1 if is_active == "1" else 0, account_id),
        )
        log_action(conn, "update", "chart_of_accounts", account_id, {"code": code, "name": name},
                   user_id=getattr(request.state, "user", {}).get("username", "admin"))
    return RedirectResponse("/account-titles?message=Account+updated", status_code=303)
