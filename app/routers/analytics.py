import json
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.db import db
from app.deps import (
    _current_user,
    _is_accounting_user,
    _is_operations_manager,
    _operations_access_companies,
    templates,
)
from app.routers.requests import _co_id

router = APIRouter()


def _last_12_months() -> list[str]:
    """Return 12 'YYYY-MM' strings, oldest first, ending with the current month."""
    today = date.today()
    y, m = today.year, today.month
    months = []
    for i in range(11, -1, -1):
        mm = m - i
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        months.append(f"{yy:04d}-{mm:02d}")
    return months


def _build_series(rows, name_key: str, months: list[str]) -> list[dict]:
    totals: dict[str, dict[str, float]] = {}
    for r in rows:
        totals.setdefault(r[name_key], {})[r["ym"]] = float(r["total"] or 0)
    series = []
    for name, by_month in sorted(totals.items()):
        series.append({
            "name": name,
            "data": [round(by_month.get(m, 0.0), 2) for m in months],
        })
    return series


@router.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, company_id: int = 0, department_id: int = 0) -> Response:
    user = _current_user(request)
    if not (_is_accounting_user(user) or _is_operations_manager(user)):
        raise HTTPException(status_code=404, detail="Not found")

    months = _last_12_months()
    since = months[0] + "-01"

    with db() as conn:
        ops_companies = []
        if _is_operations_manager(user):
            ops_companies = _operations_access_companies(conn, user)
            allowed_company_ids = [int(c["id"]) for c in ops_companies]
            if company_id and company_id in allowed_company_ids:
                selected_company_id = company_id
            elif allowed_company_ids:
                selected_company_id = allowed_company_ids[0]
            else:
                selected_company_id = 0
        else:
            selected_company_id = _co_id(conn, user["company_id"])

        dept_rows = conn.execute(
            """SELECT strftime('%Y-%m', pr.created_at) AS ym,
                      COALESCE(d.name, 'Unassigned') AS department_name,
                      SUM(CAST(REPLACE(pr.amount, ',', '') AS REAL)) AS total
                 FROM payment_requests pr
                 LEFT JOIN departments d ON d.id = pr.department_id
                WHERE pr.company_id = ?
                  AND pr.is_draft = 0
                  AND pr.status NOT IN ('cancelled', 'rejected')
                  AND pr.created_at >= ?
                GROUP BY ym, department_name
                ORDER BY ym ASC""",
            (selected_company_id, since),
        ).fetchall()

        departments = conn.execute(
            """SELECT DISTINCT d.id, d.name
                 FROM departments d
                 JOIN payment_requests pr ON pr.department_id = d.id
                WHERE pr.company_id = ? AND pr.is_draft = 0
                ORDER BY d.name""",
            (selected_company_id,),
        ).fetchall()

        selected_department_id = department_id or 0
        employee_rows = []
        if selected_department_id:
            employee_rows = conn.execute(
                """SELECT strftime('%Y-%m', pr.created_at) AS ym,
                          u.full_name AS employee_name,
                          SUM(CAST(REPLACE(pr.amount, ',', '') AS REAL)) AS total
                     FROM payment_requests pr
                     JOIN users u ON u.id = pr.requester_user_id
                    WHERE pr.company_id = ?
                      AND pr.department_id = ?
                      AND pr.is_draft = 0
                      AND pr.status NOT IN ('cancelled', 'rejected')
                      AND pr.created_at >= ?
                    GROUP BY ym, employee_name
                    ORDER BY ym ASC""",
                (selected_company_id, selected_department_id, since),
            ).fetchall()

    department_series = _build_series(dept_rows, "department_name", months)
    employee_series = _build_series(employee_rows, "employee_name", months) if selected_department_id else []

    return templates.TemplateResponse(request, "analytics.html", {
        "months_json": json.dumps(months),
        "department_series_json": json.dumps(department_series),
        "employee_series_json": json.dumps(employee_series),
        "departments": departments,
        "selected_department_id": selected_department_id,
        "ops_companies": ops_companies,
        "selected_company_id": selected_company_id,
        "is_operations_manager": _is_operations_manager(user),
    })
