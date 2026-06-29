"""app/routers/reporting.py — Tax monitoring routes."""
from __future__ import annotations

from datetime import date as _date

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.db import db
from app.deps import _current_user, templates
from app.services.tax_filing_status import (
    get_status as get_tax_filing,
)
from app.services.tax_filing_status import (
    set_status as set_tax_filing_status,
)
from app.services.tax_summary import (
    BIR_FORMS,
    due_date,
    get_form,
    summarize_form,
    valid_quarters,
)

router = APIRouter()


@router.get("/taxes", response_class=HTMLResponse)
def taxes_page(request: Request, year: int = 0, due_month: int = 0, period_index: int = 0) -> Response:
    """Show BIR forms by payment/due month, not by accounting period month.

    Example: 2550Q for Quarter 1 is included in April because Q1 ends March 31
    and the 2550Q due date is April 25.
    """
    user = _current_user(request)
    company_id = int(user["company_id"] or 1)
    today = _date.today()
    if year == 0:
        year = today.year
    if due_month == 0:
        due_month = today.month
    if due_month < 1 or due_month > 12:
        raise HTTPException(status_code=400, detail="due_month must be 1 to 12")

    summaries = []
    filings = {}
    candidate_period_years = {year, year - 1}
    with db() as conn:
        for form in BIR_FORMS:
            if form.frequency == "monthly":
                periods = range(1, 13)
            elif form.frequency == "quarterly":
                periods = valid_quarters(form)
            else:
                periods = range(1, 2)
            for py in candidate_period_years:
                for pi in periods:
                    d = due_date(form, py, pi)
                    if d.year == year and d.month == due_month:
                        summary = summarize_form(conn, form, py, pi)
                        summaries.append(summary)
                        fr = get_tax_filing(conn, form.code, py, pi, company_id=company_id)
                        if fr is not None:
                            filings[(form.code, py, pi)] = fr
    summaries.sort(key=lambda x: (x.due, x.form.code))
    return templates.TemplateResponse(request, "taxes.html",
                                       {"year": year, "due_month": due_month,
                                        "period_index": period_index,
                                        "summaries": summaries, "filings": filings,
                                        "today": today})


@router.get("/taxes/{form_code}", response_class=HTMLResponse)
def tax_form_detail(request: Request, form_code: str, year: int = 0, period_index: int = 0) -> Response:
    user = _current_user(request)
    company_id = int(user["company_id"] or 1)
    today = _date.today()
    if year == 0:
        year = today.year
    if period_index == 0:
        period_index = today.month
    form = get_form(form_code)
    if form is None:
        raise HTTPException(404, f"Form {form_code} not in catalog")
    if form.frequency == "monthly":
        pi = period_index if period_index <= 12 else 1
    elif form.frequency == "annual":
        pi = 1
    else:
        pi = period_index if period_index <= 4 else 1
    with db() as conn:
        summary = summarize_form(conn, form, year, pi)
        filing = get_tax_filing(conn, form.code, year, pi, company_id=company_id)
    return templates.TemplateResponse(request, "tax_form_detail.html",
                                       {"summary": summary, "filing": filing})


@router.post("/taxes/{form_code}/status")
def tax_form_set_status(
    request: Request,
    form_code: str,
    year: int = Form(...),
    period_index: int = Form(...),
    status: str = Form(...),
    reference_number: str = Form(""),
    notes: str = Form(""),
) -> Response:
    """Update the BIR filing status for a given form/year/period."""
    user = _current_user(request)
    company_id = int(user["company_id"] or 1)
    if get_form(form_code) is None:
        raise HTTPException(404, f"Form {form_code} not in catalog")
    try:
        set_tax_filing_status(
            form_code, year, period_index, status,
            reference_number=reference_number.strip() or None,
            notes=notes.strip() or None,
            company_id=company_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(
        f"/taxes/{form_code}?year={year}&period_index={period_index}",
        status_code=303,
    )
