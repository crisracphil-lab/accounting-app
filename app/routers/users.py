"""app/routers/users.py — User management and email-settings routes."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import (
    admin_create_user,
    get_user_admin,
    invalidate_user_sessions,
    list_users,
    require_admin,
    reset_user_password,
    unlock_user,
    update_user_profile,
)
from app.db import db, log_action
from app.services.ip_ratelimit import check_keyed_rate_limit, get_client_ip, record_keyed_attempt
from app.services.log_service import log_security_event
from app.deps import (
    _department_belongs_to_company,
    _get_app_settings,
    _list_companies,
    _list_departments,
    _save_app_settings,
    templates,
)

router = APIRouter()

_VALID_ROLES = {"admin", "accountant", "viewer", "department_user"}


# ── Shared helper – renders users.html with current data ─────────────────────

def _users_page(
    request: Request,
    message: str = "",
    error: str = "",
    tmp_password: str = "",
    tmp_username: str = "",
):
    require_admin(request)
    return templates.TemplateResponse(request, "users.html", {
        "users": list_users(),
        "message": message or None,
        "error": error or None,
        "tmp_password": tmp_password or None,
        "tmp_username": tmp_username or None,
        "roles": ["admin", "accountant", "viewer", "department_user"],
        "companies": _list_companies(),
        "departments": _list_departments(),
        "email_settings": _get_app_settings(),
    })


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    message: str = "",
    error: str = "",
    tmp_password: str = "",
    tmp_username: str = "",
) -> Response:
    return _users_page(
        request,
        message=message,
        error=error,
        tmp_password=tmp_password,
        tmp_username=tmp_username,
    )


@router.post("/users/create", response_class=HTMLResponse)
def users_create(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(""),
    role: str = Form("accountant"),
    email: str = Form(""),
    company_id: int = Form(0),
    department_id: int = Form(0),
    is_operations_manager: str = Form("0"),
    notify_operations_approvals: str = Form("0"),
    password: str = Form(...),
    confirm_password: str = Form(...),
) -> Response:
    admin = require_admin(request)
    if role not in _VALID_ROLES:
        return _users_page(request, error=f"Invalid role '{role}'. Must be one of: {', '.join(sorted(_VALID_ROLES))}.")
    if password != confirm_password:
        return _users_page(request, error="Passwords do not match.")
    if not _department_belongs_to_company(department_id or None, company_id):
        return _users_page(request, error="Selected department does not belong to the selected company.")
    try:
        new_user_id = admin_create_user(
            username, password, full_name, role,
            actor=admin["username"], email=email,
            company_id=company_id or None, department_id=department_id or None,
            is_operations_manager=is_operations_manager == "1",
            notify_operations_approvals=notify_operations_approvals == "1",
        )
        if is_operations_manager == "1":
            with db() as conn:
                conn.execute("DELETE FROM operations_manager_company_access WHERE user_id = ?", (new_user_id,))
                conn.execute(
                    "INSERT OR IGNORE INTO operations_manager_company_access (user_id, company_id) "
                    "SELECT ?, id FROM companies WHERE is_active = 1",
                    (new_user_id,),
                )
    except ValueError as exc:
        return _users_page(request, error=str(exc))
    return RedirectResponse("/users?message=User%20created", status_code=303)


@router.post("/users/{user_id}/update", response_class=HTMLResponse)
def users_update(
    request: Request,
    user_id: int,
    full_name: str = Form(""),
    role: str = Form("accountant"),
    email: str = Form(""),
    company_id: int = Form(0),
    department_id: int = Form(0),
    is_operations_manager: str = Form("0"),
    notify_operations_approvals: str = Form("0"),
    is_active: str = Form("0"),
) -> Response:
    admin = require_admin(request)
    target = get_user_admin(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if role not in _VALID_ROLES:
        return _users_page(request, error=f"Invalid role '{role}'. Must be one of: {', '.join(sorted(_VALID_ROLES))}.")
    active = is_active == "1"
    if user_id == admin["id"] and not active:
        return _users_page(request, error="You cannot deactivate your own admin account.")
    if not _department_belongs_to_company(department_id or None, company_id):
        return _users_page(request, error="Selected department does not belong to the selected company.")
    update_user_profile(
        user_id, full_name, role, active,
        actor=admin["username"], email=email,
        company_id=company_id or None, department_id=department_id or None,
        is_operations_manager=is_operations_manager == "1",
        notify_operations_approvals=notify_operations_approvals == "1",
    )
    with db() as conn:
        conn.execute("DELETE FROM operations_manager_company_access WHERE user_id = ?", (user_id,))
        if is_operations_manager == "1":
            conn.execute(
                "INSERT OR IGNORE INTO operations_manager_company_access (user_id, company_id) "
                "SELECT ?, id FROM companies WHERE is_active = 1",
                (user_id,),
            )
    return RedirectResponse("/users?message=User%20updated", status_code=303)


@router.post("/users/email-settings", response_class=HTMLResponse)
def users_email_settings(
    request: Request,
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    smtp_tls: str = Form("0"),
) -> Response:
    require_admin(request)
    current = _get_app_settings()
    settings = {
        "smtp_host": smtp_host.strip(),
        "smtp_port": smtp_port.strip() or "587",
        "smtp_user": smtp_user.strip(),
        "smtp_from": smtp_from.strip(),
        "smtp_tls": "1" if smtp_tls == "1" else "0",
    }
    if smtp_password.strip():
        settings["smtp_password"] = smtp_password.strip()
    elif "smtp_password" in current:
        settings["smtp_password"] = current.get("smtp_password", "")
    _save_app_settings(settings)
    return RedirectResponse("/users?message=Email%20notification%20settings%20saved", status_code=303)


@router.post("/users/{user_id}/delete", response_class=HTMLResponse)
def users_delete(request: Request, user_id: int) -> Response:
    admin = require_admin(request)
    target = get_user_admin(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user_id == admin["id"]:
        return _users_page(request, error="You cannot delete your own active admin account.")
    with db() as conn:
        conn.execute(
            "UPDATE users SET is_active = 0, username = username || '__deleted__' || id WHERE id = ?",
            (user_id,),
        )
        log_action(conn, "delete_user", "user", user_id, {"deleted_by": admin["username"]})
    return RedirectResponse("/users?message=User%20deleted", status_code=303)


@router.post("/users/{user_id}/reset-password", response_class=HTMLResponse)
def users_reset_password(request: Request, user_id: int) -> Response:
    admin = require_admin(request)
    target = get_user_admin(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Rate-limit to prevent bulk enumeration / automated resets.
    client_ip = get_client_ip(request)
    with db() as conn:
        is_limited, retry_after = check_keyed_rate_limit(
            conn, f"{client_ip}:reset-password"
        )
        if is_limited:
            minutes_left = (retry_after + 59) // 60
            log_security_event(
                "rate_limit_hit",
                ip=client_ip,
                action="reset-password",
                admin_id=admin["id"],
                retry_after=retry_after,
            )
            return _users_page(
                request,
                error=(
                    f"Too many password reset attempts. "
                    f"Please try again in {minutes_left} minute"
                    f"{'s' if minutes_left != 1 else ''}."
                ),
            )
        record_keyed_attempt(conn, f"{client_ip}:reset-password")

    tmp_pwd = reset_user_password(user_id, actor=admin["username"])
    # Return the page directly so the temporary password is never exposed in a URL,
    # server logs, browser history, or Referer headers.
    return _users_page(request, tmp_password=tmp_pwd, tmp_username=target["username"])


@router.post("/users/{user_id}/unlock", response_class=HTMLResponse)
def users_unlock(request: Request, user_id: int) -> Response:
    admin = require_admin(request)
    target = get_user_admin(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    unlock_user(user_id, actor=admin["username"])
    return RedirectResponse("/users?message=Account+unlocked+successfully", status_code=303)


@router.post("/users/{user_id}/force-logout", response_class=HTMLResponse)
def users_force_logout(request: Request, user_id: int) -> Response:
    """Invalidate every active session for a user without changing their password."""
    admin = require_admin(request)
    target = get_user_admin(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    invalidate_user_sessions(user_id, actor=admin["username"])
    return RedirectResponse(
        f"/users?message={target['username']}%27s+active+sessions+have+been+invalidated",
        status_code=303,
    )
