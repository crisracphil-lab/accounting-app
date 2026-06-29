"""
Routes for Closing of Books (/closing) and Financial Statements (/fs).

Templates already exist; these routes wire them to the existing service layer:
  - app.services.closing_books
  - app.services.financial_statement_service
"""
from __future__ import annotations

import json
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse

from app.auth import require_user
from app.db import db
from app.deps import templates
from app.services.closing_books import (
    DEFAULT_THRESHOLD,
    export_closing_financial_statement_with_remarks,
    finalize_run,
    save_closing_run_from_files,
    update_basis,
    update_explanation,
    update_reviewer_notes,
)
from app.services.financial_statement_service import (
    export_fs_xlsx,
    ingest_fs,
    update_remarks,
)

router = APIRouter()


def _current_user(request: Request):
    return require_user(request)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _list_closing_runs(conn):
    return conn.execute(
        """
        SELECT cr.id, cr.period_label, cr.threshold, cr.status, cr.created_at,
               COUNT(ca.id)    AS account_count,
               SUM(ca.flagged) AS flagged_count
          FROM closing_runs cr
          LEFT JOIN closing_account_changes ca ON ca.run_id = cr.id
         GROUP BY cr.id
         ORDER BY cr.id DESC
        """
    ).fetchall()


# ── Closing of Books ───────────────────────────────────────────────────────────

@router.get("/closing", response_class=HTMLResponse)
def closing_list(request: Request) -> Response:
    _current_user(request)
    with db() as conn:
        runs = _list_closing_runs(conn)
    return templates.TemplateResponse(request, "closing_upload.html", {
        "error": None,
        "runs":  runs,
    })


@router.post("/closing/upload")
async def closing_upload(
    request: Request,
    financial_statement_file: UploadFile = File(...),
    subsidiary_ledger_file:   UploadFile = File(...),
    period_label: str = Form(...),
    threshold:    str = Form("100000"),
) -> Response:
    _current_user(request)
    try:
        thr = Decimal(threshold.replace(",", "").strip())
    except InvalidOperation:
        thr = DEFAULT_THRESHOLD

    with tempfile.TemporaryDirectory() as tmp:
        fs_path = Path(tmp) / (financial_statement_file.filename or "fs.xlsx")
        sl_path = Path(tmp) / (subsidiary_ledger_file.filename  or "sl.xlsx")
        fs_path.write_bytes(await financial_statement_file.read())
        sl_path.write_bytes(await subsidiary_ledger_file.read())
        try:
            run_id = save_closing_run_from_files(fs_path, sl_path, period_label, thr)
        except Exception as exc:
            with db() as conn:
                runs = _list_closing_runs(conn)
            return templates.TemplateResponse(request, "closing_upload.html", {
                "error": str(exc),
                "runs":  runs,
            })

    return RedirectResponse(f"/closing/{run_id}", status_code=303)


@router.get("/closing/{run_id}", response_class=HTMLResponse)
def closing_detail_view(request: Request, run_id: int, show_all: str = "0") -> Response:
    _current_user(request)
    with db() as conn:
        run = conn.execute(
            "SELECT * FROM closing_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise HTTPException(404, "Closing run not found")
        rows = conn.execute(
            "SELECT * FROM closing_account_changes "
            "WHERE run_id = ? ORDER BY flagged DESC, sheet, id",
            (run_id,),
        ).fetchall()

    flagged_count = sum(1 for r in rows if r["flagged"])
    show_all_bool = show_all == "1"
    visible_rows  = rows if show_all_bool else [r for r in rows if r["flagged"]]

    return templates.TemplateResponse(request, "closing_detail.html", {
        "run":           run,
        "rows":          rows,
        "flagged_count": flagged_count,
        "show_all":      show_all_bool,
        "visible_rows":  visible_rows,
    })


@router.post("/closing/{run_id}/explain/{account_code}")
def closing_explain(request: Request, run_id: int, account_code: str,
                    explanation: str = Form("")) -> Response:
    _current_user(request)
    update_explanation(run_id, account_code, explanation)
    return RedirectResponse(f"/closing/{run_id}", status_code=303)


@router.post("/closing/{run_id}/basis/{account_code}")
def closing_basis(request: Request, run_id: int, account_code: str,
                  basis: str = Form("")) -> Response:
    _current_user(request)
    update_basis(run_id, account_code, basis)
    return RedirectResponse(f"/closing/{run_id}", status_code=303)


@router.post("/closing/{run_id}/reviewer/{account_code}")
def closing_reviewer(request: Request, run_id: int, account_code: str,
                     notes: str = Form("")) -> Response:
    _current_user(request)
    update_reviewer_notes(run_id, account_code, notes)
    return RedirectResponse(f"/closing/{run_id}", status_code=303)


@router.post("/closing/{run_id}/finalize")
def closing_finalize(request: Request, run_id: int) -> Response:
    _current_user(request)
    finalize_run(run_id)
    return RedirectResponse(f"/closing/{run_id}", status_code=303)


@router.get("/closing/{run_id}/export-fs")
def closing_export_fs(request: Request, run_id: int) -> Response:
    _current_user(request)
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / f"closing_{run_id}_fs_with_remarks.xlsx"
        try:
            result_path = export_closing_financial_statement_with_remarks(run_id, out_path)
            data = result_path.read_bytes()
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
    fname = f"closing_{run_id}_financial_statement.xlsx"
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── Financial Statements ───────────────────────────────────────────────────────

@router.get("/fs", response_class=HTMLResponse)
def fs_list(request: Request) -> Response:
    _current_user(request)
    with db() as conn:
        uploads = conn.execute(
            "SELECT * FROM fs_uploads ORDER BY id DESC"
        ).fetchall()
    return templates.TemplateResponse(request, "fs_list.html", {
        "error":   None,
        "uploads": uploads,
    })


@router.post("/fs/upload")
async def fs_upload(
    request: Request,
    file:         UploadFile = File(...),
    period_label: str        = Form(...),
) -> Response:
    _current_user(request)
    with tempfile.TemporaryDirectory() as tmp:
        fs_path = Path(tmp) / (file.filename or "fs.xlsx")
        fs_path.write_bytes(await file.read())
        try:
            up_id = ingest_fs(fs_path, period_label)
        except Exception as exc:
            with db() as conn:
                uploads = conn.execute(
                    "SELECT * FROM fs_uploads ORDER BY id DESC"
                ).fetchall()
            return templates.TemplateResponse(request, "fs_list.html", {
                "error":   str(exc),
                "uploads": uploads,
            })
    return RedirectResponse(f"/fs/{up_id}", status_code=303)


class _FSRowObj:
    """Thin wrapper so Jinja2 can access row.columns as an attribute."""
    __slots__ = ("id", "sheet", "row_number", "account_code",
                 "account_title", "columns", "inc_dec", "remarks", "edited_at")

    def __getitem__(self, k):   # allow r["key"] access too
        return getattr(self, k)


def _wrap_fs_row(r) -> _FSRowObj:
    o = _FSRowObj()
    o.id            = r["id"]
    o.sheet         = r["sheet"]
    o.row_number    = r["row_number"]
    o.account_code  = r["account_code"]
    o.account_title = r["account_title"]
    o.columns       = json.loads(r["columns_json"] or "{}")
    o.inc_dec       = r["inc_dec"]
    o.remarks       = r["remarks"]
    o.edited_at     = r["edited_at"]
    return o


@router.get("/fs/{fs_id}", response_class=HTMLResponse)
def fs_detail(request: Request, fs_id: int) -> Response:
    _current_user(request)
    with db() as conn:
        upload = conn.execute(
            "SELECT * FROM fs_uploads WHERE id = ?", (fs_id,)
        ).fetchone()
        if upload is None:
            raise HTTPException(404, "FS upload not found")
        raw_rows = conn.execute(
            "SELECT * FROM fs_rows WHERE fs_upload_id = ? ORDER BY sheet, row_number",
            (fs_id,),
        ).fetchall()

    is_columns = json.loads(upload["is_columns_json"] or "[]")
    bs_columns = json.loads(upload["bs_columns_json"] or "[]")
    all_rows   = [_wrap_fs_row(r) for r in raw_rows]
    is_rows    = [r for r in all_rows if r.sheet == "IS"]
    bs_rows    = [r for r in all_rows if r.sheet == "BS"]

    return templates.TemplateResponse(request, "fs_detail.html", {
        "upload":     upload,
        "is_rows":    is_rows,
        "bs_rows":    bs_rows,
        "is_columns": is_columns,
        "bs_columns": bs_columns,
    })


@router.post("/fs/row/{row_id}/remarks")
def fs_row_remarks(request: Request, row_id: int, remarks: str = Form("")) -> Response:
    _current_user(request)
    with db() as conn:
        row = conn.execute(
            "SELECT fs_upload_id FROM fs_rows WHERE id = ?", (row_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "FS row not found")
        fs_id = row["fs_upload_id"]
    update_remarks(row_id, remarks)
    return RedirectResponse(f"/fs/{fs_id}", status_code=303)


@router.get("/fs/{fs_id}/export")
def fs_export(request: Request, fs_id: int) -> Response:
    _current_user(request)
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / f"fs_{fs_id}_export.xlsx"
        try:
            result_path = export_fs_xlsx(fs_id, out_path)
            data = result_path.read_bytes()
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
    fname = f"financial_statement_{fs_id}.xlsx"
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
