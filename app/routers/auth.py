"""app/routers/auth.py — Authentication routes."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import (
    LOCKOUT_MINUTES,
    OTP_PENDING_COOKIE,
    authenticate,
    change_own_password,
    create_user,
    hash_password,
    login_response,
    logout_response,
    make_session_token,
    otp_pending_response,
    read_otp_pending_token,
    require_user,
    user_count,
    validate_password_strength,
)
from app.db import db
from app.deps import templates
from app.services.ip_ratelimit import (
    check_keyed_rate_limit,
    check_rate_limit,
    get_client_ip,
    record_attempt,
    record_keyed_attempt,
)
from app.services.log_service import log_security_event

router = APIRouter()


@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request) -> Response:
    if user_count() > 0:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@router.post("/setup", response_class=HTMLResponse)
def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    full_name: str = Form(""),
) -> Response:
    if user_count() > 0:
        return RedirectResponse("/login", status_code=303)
    if password != confirm_password:
        return templates.TemplateResponse(request, "setup.html", {"error": "Passwords do not match."})
    try:
        validate_password_strength(password)
        user_id = create_user(username=username, password=password, full_name=full_name, role="admin")
    except ValueError as exc:
        return templates.TemplateResponse(request, "setup.html", {"error": str(exc)})
    return login_response(user_id)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/") -> Response:
    if user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None, "next": next or "/"})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
) -> Response:
    client_ip = get_client_ip(request)

    with db() as conn:
        is_limited, retry_after = check_rate_limit(conn, client_ip)
        if is_limited:
            minutes_left = (retry_after + 59) // 60
            log_security_event(
                "login_rate_limit_hit",
                ip=client_ip,
                retry_after=retry_after,
            )
            response = templates.TemplateResponse(
                request,
                "login.html",
                {
                    "error": (
                        f"Too many login attempts from your IP address. "
                        f"Please try again in {minutes_left} minute"
                        f"{'s' if minutes_left != 1 else ''}."
                    ),
                    "next": next or "/",
                },
                status_code=429,
            )
            response.headers["Retry-After"] = str(retry_after)
            return response
        record_attempt(conn, client_ip)

    result = authenticate(username, password)
    if result is None:
        log_security_event("login_failed", ip=client_ip, username=username)
        return templates.TemplateResponse(request, "login.html", {
            "error": "Invalid username or password.",
            "next": next or "/",
        })
    if isinstance(result, dict) and result.get("error") == "locked":
        log_security_event("login_account_locked", ip=client_ip, username=username)
        return templates.TemplateResponse(request, "login.html", {
            "error": (
                f"Account locked due to too many failed attempts. "
                f"Please try again after {LOCKOUT_MINUTES} minutes or contact your administrator."
            ),
            "next": next or "/",
        })
    user = result
    if not next or not next.startswith("/") or next.startswith("//"):
        next = "/"

    # If the user has TOTP enabled, redirect to the OTP verification step
    # instead of issuing a full session cookie.
    if user["totp_enabled"]:
        return otp_pending_response(user["id"], "/change-password" if user["must_change_password"] else next)

    return login_response(user["id"], "/change-password" if user["must_change_password"] else next)


# ---------------------------------------------------------------------------
# OTP verification step (second factor)
# ---------------------------------------------------------------------------

@router.get("/login/verify-otp", response_class=HTMLResponse)
def verify_otp_form(request: Request) -> Response:
    token = request.cookies.get(OTP_PENDING_COOKIE)
    parsed = read_otp_pending_token(token)
    if parsed is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "verify_otp.html", {"error": None})


@router.post("/login/verify-otp", response_class=HTMLResponse)
def verify_otp_submit(
    request: Request,
    otp_code: str = Form(""),
) -> Response:
    from app.services.totp_service import consume_backup_code, verify_totp

    token = request.cookies.get(OTP_PENDING_COOKIE)
    parsed = read_otp_pending_token(token)
    if parsed is None:
        return RedirectResponse("/login", status_code=303)
    user_id, next_url = parsed

    with db() as conn:
        row = conn.execute(
            "SELECT totp_secret, totp_backup_codes, must_change_password FROM users WHERE id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()

    if row is None:
        return RedirectResponse("/login", status_code=303)

    code = otp_code.strip()

    # Try TOTP first, then backup codes
    if verify_totp(row["totp_secret"], code):
        response = login_response(user_id, "/change-password" if row["must_change_password"] else next_url)
        response.delete_cookie(OTP_PENDING_COOKIE)
        return response

    # Try backup code
    success, updated_hashes = consume_backup_code(row["totp_backup_codes"], code)
    if success:
        with db() as conn:
            conn.execute(
                "UPDATE users SET totp_backup_codes = ? WHERE id = ?",
                (updated_hashes, user_id),
            )
        log_security_event("backup_code_used", user_id=user_id)
        response = login_response(user_id, "/change-password" if row["must_change_password"] else next_url)
        response.delete_cookie(OTP_PENDING_COOKIE)
        return response

    log_security_event("totp_failed", user_id=user_id)
    return templates.TemplateResponse(
        request, "verify_otp.html",
        {"error": "Invalid code. Please try again or use a backup code."},
        status_code=401,
    )


# ---------------------------------------------------------------------------
# 2FA setup / disable (under /profile)
# ---------------------------------------------------------------------------

@router.get("/profile/setup-2fa", response_class=HTMLResponse)
def setup_2fa_form(request: Request) -> Response:
    from app.services.totp_service import (
        generate_backup_codes,
        generate_totp_secret,
        get_qr_data_uri,
        get_totp_uri,
        hash_backup_codes,
    )

    user = require_user(request)

    # Generate a new secret and backup codes for this setup session.
    # We store the provisional secret in a signed cookie until the user
    # confirms with a valid code (so we don't commit unverified secrets).
    new_secret = generate_totp_secret()
    backup_codes = generate_backup_codes()
    totp_uri = get_totp_uri(new_secret, user["username"])
    qr_data_uri = get_qr_data_uri(totp_uri)

    # Store provisional data in the session via a signed cookie
    import base64 as _b64, json as _json
    from app.auth import _sign, HTTPS_ENABLED, OTP_PENDING_SECONDS
    provisional = _b64.urlsafe_b64encode(
        _json.dumps({"s": new_secret, "b": hash_backup_codes(backup_codes)}).encode()
    ).decode()
    from app.auth import _sign
    sig = _sign(f"2fa-setup:{user['id']}:{provisional}")
    setup_cookie = f"{provisional}:{sig}"

    response = templates.TemplateResponse(
        request, "setup_2fa.html",
        {
            "qr_data_uri": qr_data_uri,
            "totp_uri": totp_uri,
            "backup_codes": backup_codes,
            "error": None,
        },
    )
    response.set_cookie(
        "totp_setup_pending",
        setup_cookie,
        httponly=True,
        samesite="lax",
        max_age=600,  # 10 minutes to complete setup
        secure=HTTPS_ENABLED,
    )
    return response


@router.post("/profile/setup-2fa", response_class=HTMLResponse)
def setup_2fa_submit(
    request: Request,
    otp_code: str = Form(""),
) -> Response:
    import base64 as _b64, json as _json
    from app.auth import _sign, HTTPS_ENABLED
    from app.services.totp_service import verify_totp

    user = require_user(request)

    # Read and verify the provisional setup cookie
    setup_cookie = request.cookies.get("totp_setup_pending", "")
    parts = setup_cookie.rsplit(":", 1)
    if len(parts) != 2:
        return RedirectResponse("/profile/setup-2fa", status_code=303)
    provisional, sig = parts
    if not __import__("hmac").compare_digest(sig, _sign(f"2fa-setup:{user['id']}:{provisional}")):
        return RedirectResponse("/profile/setup-2fa", status_code=303)

    try:
        data = _json.loads(_b64.urlsafe_b64decode(provisional.encode()).decode())
        new_secret = data["s"]
        hashed_backup = data["b"]
    except Exception:
        return RedirectResponse("/profile/setup-2fa", status_code=303)

    if not verify_totp(new_secret, otp_code.strip()):
        # Re-render with error (user needs to try again; issue fresh setup)
        return RedirectResponse("/profile/setup-2fa?error=invalid", status_code=303)

    # Commit to DB
    with db() as conn:
        conn.execute(
            "UPDATE users SET totp_secret = ?, totp_enabled = 1, totp_backup_codes = ? WHERE id = ?",
            (new_secret, hashed_backup, user["id"]),
        )
    log_security_event("totp_enabled", user_id=user["id"])

    response = RedirectResponse("/profile?message=2FA+enabled+successfully.", status_code=303)
    response.delete_cookie("totp_setup_pending")
    return response


@router.post("/profile/disable-2fa", response_class=HTMLResponse)
def disable_2fa(request: Request, password: str = Form("")) -> Response:
    from app.auth import verify_password

    user = require_user(request)
    with db() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        if row is None or not verify_password(password, row["password_hash"]):
            # Re-render profile with error
            profile_row = conn.execute(
                "SELECT full_name, email, totp_enabled FROM users WHERE id = ?", (user["id"],)
            ).fetchone()
            return templates.TemplateResponse(
                request, "profile.html",
                {
                    "full_name": profile_row["full_name"] or "",
                    "email": profile_row["email"] or "",
                    "totp_enabled": bool(profile_row["totp_enabled"]),
                    "message": None,
                    "error": "Incorrect password. 2FA was not disabled.",
                },
            )
        conn.execute(
            "UPDATE users SET totp_secret = NULL, totp_enabled = 0, totp_backup_codes = NULL WHERE id = ?",
            (user["id"],),
        )
    log_security_event("totp_disabled", user_id=user["id"])
    return RedirectResponse("/profile?message=2FA+disabled.", status_code=303)


@router.post("/logout")
def logout() -> Response:
    if user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    return logout_response()


@router.get("/profile", response_class=HTMLResponse)
def profile_form(request: Request, message: str = "") -> Response:
    user = require_user(request)
    with db() as conn:
        row = conn.execute(
            "SELECT full_name, email, COALESCE(totp_enabled, 0) AS totp_enabled FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
    return templates.TemplateResponse(
        request, "profile.html",
        {
            "full_name": row["full_name"] or "",
            "email": row["email"] or "",
            "totp_enabled": bool(row["totp_enabled"]),
            "message": message or None,
            "error": None,
        },
    )


@router.post("/profile", response_class=HTMLResponse)
def profile_submit(
    request: Request,
    full_name: str = Form(""),
    email: str = Form(""),
) -> Response:
    user = require_user(request)
    full_name = full_name.strip()
    email = email.strip() or None
    with db() as conn:
        conn.execute(
            "UPDATE users SET full_name = ?, email = ? WHERE id = ?",
            (full_name or None, email, user["id"]),
        )
        totp_enabled = bool(conn.execute(
            "SELECT COALESCE(totp_enabled, 0) AS totp_enabled FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()["totp_enabled"])
    ctx = {
        "full_name": full_name,
        "email": email or "",
        "totp_enabled": totp_enabled,
        "message": "Profile updated successfully.",
        "error": None,
    }
    return templates.TemplateResponse(request, "profile.html", ctx)


@router.get("/change-password", response_class=HTMLResponse)
def change_password_form(request: Request) -> Response:
    user = require_user(request)
    return templates.TemplateResponse(
        request, "change_password.html",
        {"error": None, "forced": bool(user["must_change_password"])},
    )


@router.post("/change-password", response_class=HTMLResponse)
def change_password_submit(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> Response:
    user = require_user(request)
    forced = bool(user["must_change_password"])

    # Rate-limit by IP to prevent brute-force against the current-password check.
    client_ip = get_client_ip(request)
    with db() as conn:
        is_limited, retry_after = check_keyed_rate_limit(
            conn, f"{client_ip}:change-password"
        )
        if is_limited:
            minutes_left = (retry_after + 59) // 60
            log_security_event(
                "rate_limit_hit",
                ip=client_ip,
                action="change-password",
                retry_after=retry_after,
            )
            response = templates.TemplateResponse(
                request,
                "change_password.html",
                {
                    "error": (
                        f"Too many password change attempts. "
                        f"Please try again in {minutes_left} minute"
                        f"{'s' if minutes_left != 1 else ''}."
                    ),
                    "forced": forced,
                },
                status_code=429,
            )
            response.headers["Retry-After"] = str(retry_after)
            return response
        record_keyed_attempt(conn, f"{client_ip}:change-password")

    if new_password != confirm_password:
        return templates.TemplateResponse(
            request, "change_password.html",
            {"error": "New passwords do not match.", "forced": forced},
        )
    try:
        if forced:
            # Skip current password check — set new password directly
            validate_password_strength(new_password)
            pw_hash = hash_password(new_password)
            with db() as conn:
                conn.execute(
                    "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                    (pw_hash, user["id"]),
                )
        else:
            change_own_password(user["id"], current_password, new_password)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "change_password.html",
            {"error": str(exc), "forced": forced},
        )
    return RedirectResponse("/?message=Password%20changed", status_code=303)
