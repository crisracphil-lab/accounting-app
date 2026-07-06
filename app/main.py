"""
FastAPI entry point for the local accounting web app.

Run:  uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import hashlib
import secrets
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.auth import (
    COOKIE_NAME,
    get_user_by_id_sv,
    parse_session_token,
    user_count,
)
from app.csrf import generate_csrf_token, validate_csrf_token
from app.db import db, init_db
from app.config import HTTPS_ENABLED
from app.deps import _is_accounting_user, templates
from app.services.log_service import log_error_event, log_security_event

# ── Router imports ────────────────────────────────────────────────────────────
from app.routers import (
    accounting,
    admin,
    analytics,
    api,
    auth,
    banking,
    closing,
    invoices,
    reconciliation,
    reporting,
    requests,
    requests_export,
    users,
)

# ── App instance ──────────────────────────────────────────────────────────────
app = FastAPI(title="BookPoint - Accounting and Financial Operations")

BASE_DIR = Path(__file__).parent

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ── Exception handlers ────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def friendly_exception_handler(request: Request, exc: Exception):
    error_id = hashlib.sha256(
        f"{request.method}:{request.url.path}:{repr(exc)}".encode()
    ).hexdigest()[:8].upper()
    # Log only method + path — never query strings, which may contain tokens
    safe_path = f"{request.method} {request.url.path}"
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log_error_event(error_id, safe_path, tb)
    print(f"[ERROR {error_id}] {request.method} {request.url.path}: {exc}")
    traceback.print_exc()
    # Build a human-readable reason from the exception chain
    reasons = []
    e: BaseException | None = exc
    seen: set = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        msg = str(e).strip()
        if msg:
            reasons.append(f"{type(e).__name__}: {msg}")
        e = e.__cause__ or e.__context__
    readable_reason = "\n".join(reasons) if reasons else f"{type(exc).__name__}"

    if request.headers.get("accept", "").lower().find("application/json") >= 0:
        return JSONResponse(
            status_code=500,
            content={"error": readable_reason, "error_id": error_id},
        )
    return templates.TemplateResponse(
        request,
        "error.html",
        {"error_id": error_id, "error": readable_reason},
        status_code=500,
    )


@app.exception_handler(HTTPException)
async def friendly_http_exception_handler(request: Request, exc: HTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "We could not process this request."
    if request.headers.get("accept", "").lower().find("application/json") >= 0:
        return JSONResponse(status_code=exc.status_code, content={"error": message})
    return templates.TemplateResponse(
        request,
        "error.html",
        {"error_id": "VALIDATION", "error": message},
        status_code=exc.status_code,
    )


# ── Database initialisation ───────────────────────────────────────────────────
init_db()


# ── Middleware ────────────────────────────────────────────────────────────────
PUBLIC_PATHS = {"/login", "/logout", "/setup", "/health"}


@app.middleware("http")
async def load_user_and_protect_pages(request: Request, call_next):
    """Attach request.state.user, generate CSRF token, require login for accounting pages.

    CSRF enforcement rules
    ──────────────────────
    * Only for mutating methods: POST, PUT, DELETE, PATCH.
    * Only when the user is authenticated (request.state.user is not None).
    * /logout is exempt: its only effect is deleting a cookie; CSRF cannot
      cause meaningful harm there and exempting it avoids a chicken-and-egg
      problem when a session expires mid-form.
    * /setup and /login are public paths served to unauthenticated users,
      so the "user is not None" guard already excludes them.
    """
    path = request.url.path
    user = None
    # parse_session_token: pure crypto, no DB hit.
    # get_user_by_id_sv: single DB query — fetches user AND validates session_version.
    parsed = parse_session_token(request.cookies.get(COOKIE_NAME))
    if parsed is not None:
        _uid, _sv = parsed
        user = get_user_by_id_sv(_uid, _sv)
    request.state.user = user
    request.state.can_use_calendar = _is_accounting_user(user)
    request.state.unread_notifications_count = 0

    # Mint a fresh CSRF token for every authenticated page load.
    if user is not None:
        request.state.csrf_token = generate_csrf_token(user["id"])
        try:
            with db() as conn:
                request.state.unread_notifications_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND is_read = 0",
                    (user["id"],),
                ).fetchone()["n"]
        except Exception:
            request.state.unread_notifications_count = 0
    else:
        # Unauthenticated requests get an empty token; the template meta tag
        # will be empty and the login/setup forms are not CSRF-validated.
        request.state.csrf_token = ""

    # CSRF validation for authenticated mutating requests.
    _MUTATING = {"POST", "PUT", "DELETE", "PATCH"}
    _CSRF_EXEMPT = {"/logout"}
    if (
        request.method in _MUTATING
        and user is not None
        and path not in _CSRF_EXEMPT
    ):
        try:
            await request.body()  # cache raw bytes in _body so stream() replays for form()
            form_data = await request.form()
            submitted_token = form_data.get("csrf_token", "")
        except Exception:
            submitted_token = ""

        if not validate_csrf_token(submitted_token, user["id"]):
            log_security_event(
                "csrf_rejected",
                path=f"{request.method} {request.url.path}",
                user_id=user["id"],
            )
            if request.headers.get("accept", "").lower().find("application/json") >= 0:
                return JSONResponse(
                    status_code=403,
                    content={"error": "Invalid or expired CSRF token. Please reload the page and try again."},
                )
            return templates.TemplateResponse(
                request,
                "error.html",
                {
                    "error_id": "CSRF",
                    "error": "Your form session has expired or the request was rejected for security reasons. "
                             "Please go back, reload the page, and try again.",
                },
                status_code=403,
            )

    # Auth / redirect guards.
    is_public = path in PUBLIC_PATHS or path.startswith("/static/")
    if user is None and not is_public:
        if user_count() == 0:
            return RedirectResponse("/setup", status_code=303)
        next_url = str(request.url.path)
        if request.url.query:
            next_url += "?" + request.url.query
        return RedirectResponse(f"/login?next={next_url}", status_code=303)

    if (
        user is not None
        and user["must_change_password"]
        and path not in {"/change-password", "/logout"}
        and not path.startswith("/static/")
    ):
        return RedirectResponse("/change-password", status_code=303)

    if user is not None and user["role"] == "department_user":
        allowed_prefixes = [
            "/requests", "/notifications", "/static/", "/api/health", "/change-password", "/profile",
        ]
        if request.state.can_use_calendar:
            allowed_prefixes.extend(["/calendar", "/api/calendar"])
        if user["is_operations_manager"]:
            allowed_prefixes.extend(["/ops-reconciliation", "/analytics"])
        allowed_exact = {"/logout", "/"}
        if path not in allowed_exact and not any(
            path.startswith(prefix) for prefix in allowed_prefixes
        ):
            return RedirectResponse("/requests", status_code=303)

    return await call_next(request)


# ── Security headers middleware ───────────────────────────────────────────────
# Registration order matters: FastAPI/Starlette builds the middleware stack
# with reversed(), so the LAST @app.middleware("http") decorator in source
# becomes the OUTERMOST wrapper.  Being outermost means this middleware
# receives every response — including early returns (redirects, CSRF 403s)
# produced by load_user_and_protect_pages above — and stamps security headers
# on all of them.
#
# Per-request CSP nonce: a fresh cryptographic nonce is generated before
# call_next so that the inner middleware and template renderer can read it
# from request.state.csp_nonce.  The nonce is embedded into the CSP header
# and into every inline <script nonce="…"> / <style nonce="…"> tag in the
# templates.  This eliminates 'unsafe-inline' from the policy.
#
# HTTPS-only header: set ACCOUNTING_HTTPS=1 in production to enable HSTS.
# Omitting it locally prevents browsers from pinning HTTP-only dev servers.


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Generate a per-request CSP nonce and inject HTTP security headers.

    Attachment/receipt preview routes (paths ending in "/preview") are
    rendered by request_detail.html inside a same-origin <iframe> so PDFs
    can be viewed inline. A blanket "frame-ancestors 'none'" / "X-Frame-
    Options: DENY" forbids ANY framing of the response — including framing
    by the very same app — so browsers refuse to display the iframe and show
    a broken-file placeholder instead of the PDF. Relax just these routes to
    same-origin framing; every other response keeps the strict default.
    """
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    response = await call_next(request)
    is_preview = request.url.path.endswith("/preview")
    frame_ancestors = "'self'" if is_preview else "'none'"
    csp = (
        "default-src 'self'; "
        f"script-src 'self' https://cdn.jsdelivr.net 'nonce-{nonce}'; "
        # script-src-attr has no nonce/hash source of its own, so 'unsafe-inline'
        # here is honored (it is only dropped when the SAME directive also
        # carries a nonce/hash). This allows the onclick="…"/onchange="…"/
        # oninput="…" attributes used throughout the templates (e.g. the
        # split-accounts row controls in request_detail.html) to run, while
        # <script> tags themselves remain locked down to 'self' + the nonce —
        # an injected <script> tag (the classic XSS vector) is still blocked.
        "script-src-attr 'unsafe-inline'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        # Same reasoning for inline style="…" attributes (e.g. the 85vh
        # attachment preview sizing) — <style> tags stay nonce-gated.
        "style-src-attr 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        f"frame-ancestors {frame_ancestors}"
    )
    response.headers["Content-Security-Policy"] = csp
    response.headers["X-Frame-Options"] = "SAMEORIGIN" if is_preview else "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if HTTPS_ENABLED:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health_check():
    """Public health check endpoint for load balancer / uptime monitoring."""
    return {"status": "ok", "service": "bookpoint"}


# ── Router registration ───────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(admin.router)
app.include_router(api.router)
app.include_router(reporting.router)
app.include_router(banking.router)
app.include_router(accounting.router)
app.include_router(analytics.router)
app.include_router(reconciliation.router)
app.include_router(requests_export.router)
app.include_router(requests.router)
app.include_router(invoices.router)
app.include_router(closing.router)
