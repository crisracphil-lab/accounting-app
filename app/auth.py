"""Small local-auth helper for the FastAPI accounting app.

Uses SQLite users, PBKDF2 password hashing, and signed cookies. No default
password is hardcoded or generated; create the first admin through /setup.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from app.db import db, log_action
from app.config import (
    HTTPS_ENABLED,
    LOCKOUT_MINUTES,
    MAX_LOGIN_ATTEMPTS,
    PBKDF2_ITERATIONS,
    SESSION_SECONDS,
)

COOKIE_NAME = "accounting_session"


def _load_secret_key() -> str:
    """Load a real local signing key without using a hardcoded placeholder secret.

    Priority:
    1. ACCOUNTING_SECRET_KEY environment variable.
    2. A generated local .bookpoint_secret file beside the working app.

    The generated file is local-only and is not a placeholder value.
    """
    env_secret = os.environ.get("ACCOUNTING_SECRET_KEY")
    if env_secret:
        if len(env_secret) < 32:
            raise RuntimeError("ACCOUNTING_SECRET_KEY must be at least 32 characters.")
        return env_secret

    secret_path = Path(os.environ.get("ACCOUNTING_SECRET_FILE", ".bookpoint_secret"))
    if secret_path.exists():
        secret = secret_path.read_text(encoding="utf-8").strip()
        if len(secret) < 32:
            raise RuntimeError(f"Secret file {secret_path} is too short; delete it to regenerate.")
        return secret

    secret = secrets.token_urlsafe(48)
    secret_path.write_text(secret, encoding="utf-8")
    try:
        secret_path.chmod(0o600)
    except OSError:
        # Windows may not support POSIX chmod; the secret is still random and local.
        pass
    return secret


SECRET = _load_secret_key()


VALID_ROLES = {"admin", "accountant", "viewer", "department_user"}


# ---------------------------------------------------------------------------
# Typed user object
# ---------------------------------------------------------------------------

@dataclass
class User:
    """Typed user returned by require_user() / _current_user().

    Supports both attribute access (user.role) and legacy dict-style access
    (user["role"]) so all existing router code continues to work unchanged.
    The .get() method means future code can safely call user.get("field")
    without crashing — eliminating the sqlite3.Row footgun entirely.
    """
    id: int
    username: str
    full_name: str
    email: str
    role: str
    is_active: bool
    department_id: Optional[int]
    company_id: int
    is_operations_manager: bool
    notify_operations_approvals: bool
    must_change_password: bool

    @classmethod
    def from_row(cls, row) -> "User":
        return cls(
            id=int(row["id"]),
            username=str(row["username"] or ""),
            full_name=str(row["full_name"] or ""),
            email=str(row["email"] or ""),
            role=str(row["role"] or "accountant"),
            is_active=bool(row["is_active"]),
            department_id=row["department_id"],
            company_id=int(row["company_id"] or 1),
            is_operations_manager=bool(int(row["is_operations_manager"] or 0)),
            notify_operations_approvals=bool(int(row["notify_operations_approvals"] or 1)),
            must_change_password=bool(int(row["must_change_password"] or 0)),
        )

    # ---- dict-style compatibility so user["key"] still works in all routers

    def __getitem__(self, key: str):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def get(self, key: str, default=None):
        try:
            return getattr(self, key)
        except AttributeError:
            return default

    def keys(self):
        return vars(self).keys()


def normalize_role(role: str) -> str:
    role = (role or "accountant").strip().lower()
    return role if role in VALID_ROLES else "accountant"


_SPECIAL_CHARS = set("!@#$%^&*()_+-=")


def validate_password_strength(password: str) -> None:
    """Raise ValueError with a descriptive message if the password is too weak."""
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if not any(c.isupper() for c in password):
        raise ValueError("Password must contain at least one uppercase letter.")
    if not any(c.isdigit() for c in password):
        raise ValueError("Password must contain at least one number.")
    if not any(c in _SPECIAL_CHARS for c in password):
        raise ValueError("Password must contain at least one special character (!@#$%^&*()_+-=).")


def validate_temporary_password(password: str) -> None:
    """Relaxed policy for admin-set temporary passwords.

    The user is forced to change this on first login, so only a minimum
    length is enforced — no uppercase / digit / special-character rules.
    """
    if not password or len(password) < 6:
        raise ValueError("Temporary password must be at least 6 characters.")


def _generate_temp_password() -> str:
    """Generate a random 12-character password that passes validate_password_strength."""
    import string
    pool = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        chars = [secrets.choice(pool) for _ in range(12)]
        pwd = "".join(chars)
        if (any(c.isupper() for c in pwd)
                and any(c.isdigit() for c in pwd)
                and any(c in _SPECIAL_CHARS for c in pwd)):
            return pwd


def hash_password(password: str) -> str:
    # Validation is the caller's responsibility — call validate_password_strength()
    # or validate_temporary_password() before this.
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iter_s, salt_b64, digest_b64 = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iter_s))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _sign(payload: str) -> str:
    return hmac.new(SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _get_session_version(user_id: int) -> int:
    """Return the current session_version for *user_id* (0 if column absent).

    Used only by make_session_token() when minting a new token.
    Per-request validation goes through get_user_by_id_sv() instead to avoid
    a second round-trip to the database.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT session_version FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if row is None:
        return 0
    return int(row["session_version"] or 0)


def make_session_token(user_id: int) -> str:
    """Create a signed session token embedding the current session_version.

    Token format (5 colon-separated parts):
        {user_id}:{issued}:{session_version}:{nonce}:{hmac_sha256}

    The nonce is produced by secrets.token_urlsafe(12) which uses URL-safe
    base64 characters and never contains a colon, so split(":", 4) is safe.
    """
    issued = int(datetime.now(timezone.utc).timestamp())
    sv = _get_session_version(user_id)
    nonce = secrets.token_urlsafe(12)
    payload = f"{user_id}:{issued}:{sv}:{nonce}"
    return f"{payload}:{_sign(payload)}"


def parse_session_token(token: str | None) -> Optional[tuple[int, Optional[int]]]:
    """Cryptographically verify the token with no database access.

    Returns ``(user_id, token_sv)`` on success, or ``None`` if the token is
    missing, malformed, tampered with, or expired.

    ``token_sv`` is ``None`` for legacy 4-part tokens — the caller is
    responsible for skipping the session-version check in that case (backward
    compatibility window until all old cookies have expired naturally).

    This function is the hot path called on every request.  Keep it DB-free so
    the single database round-trip is consolidated in ``get_user_by_id_sv()``.
    """
    if not token:
        return None
    try:
        parts = token.split(":", 4)
        if len(parts) == 5:
            # New format: user_id:issued:sv:nonce:sig
            user_id_s, issued_s, sv_s, nonce, sig = parts
            payload = f"{user_id_s}:{issued_s}:{sv_s}:{nonce}"
            if not hmac.compare_digest(sig, _sign(payload)):
                return None
            if int(datetime.now(timezone.utc).timestamp()) - int(issued_s) >= SESSION_SECONDS:
                return None
            return int(user_id_s), int(sv_s)
        elif len(parts) == 4:
            # Legacy format: user_id:issued:nonce:sig (no version embedded)
            user_id_s, issued_s, nonce, sig = parts
            payload = f"{user_id_s}:{issued_s}:{nonce}"
            if not hmac.compare_digest(sig, _sign(payload)):
                return None
            if int(datetime.now(timezone.utc).timestamp()) - int(issued_s) >= SESSION_SECONDS:
                return None
            return int(user_id_s), None  # None = skip version check
        else:
            return None
    except Exception:
        return None


def read_session_token(token: str | None) -> Optional[int]:
    """Validate *token* and return the user_id, or None if invalid/expired.

    Convenience wrapper used by tests and any code that only needs the user_id.
    The request middleware uses the more efficient ``parse_session_token`` +
    ``get_user_by_id_sv`` path instead (one DB query instead of two).
    """
    parsed = parse_session_token(token)
    if parsed is None:
        return None
    user_id, token_sv = parsed
    if token_sv is None:
        # Legacy token: no version embedded, skip check
        return user_id
    current_sv = _get_session_version(user_id)
    if token_sv != current_sv:
        return None
    return user_id


def user_count() -> int:
    with db() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM users WHERE is_active = 1").fetchone()["n"]


def get_user_by_id(user_id: int) -> Optional[User]:
    with db() as conn:
        row = conn.execute(
            """SELECT id, username, full_name, email, role, is_active,
                      department_id, company_id, is_operations_manager,
                      notify_operations_approvals,
                      COALESCE(must_change_password, 0) AS must_change_password
               FROM users WHERE id = ? AND is_active = 1""",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return User.from_row(row)


def get_user_by_id_sv(user_id: int, token_sv: Optional[int]) -> Optional[User]:
    """Fetch the active user and validate session_version in a single query.

    This is the optimised path used by the request middleware.  It combines
    what would otherwise be two separate DB calls (``_get_session_version`` +
    ``get_user_by_id``) into one, cutting per-request database overhead in half
    for authenticated requests.

    Args:
        user_id:  The user ID extracted from the signed session token.
        token_sv: The session_version embedded in the token, or ``None`` for
                  legacy 4-part tokens (version check is skipped).

    Returns:
        The ``User`` if active and version matches, ``None`` otherwise.
    """
    with db() as conn:
        row = conn.execute(
            """SELECT id, username, full_name, email, role, is_active,
                      department_id, company_id, is_operations_manager,
                      notify_operations_approvals,
                      COALESCE(must_change_password, 0) AS must_change_password,
                      COALESCE(session_version, 0) AS session_version
               FROM users WHERE id = ? AND is_active = 1""",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    # Skip version check for legacy tokens (token_sv is None)
    if token_sv is not None and int(row["session_version"]) != token_sv:
        return None
    return User.from_row(row)


def _is_locked(user) -> bool:
    """Return True if the account is currently locked out."""
    locked_until = user["locked_until"] if "locked_until" in user.keys() else None
    if not locked_until:
        return False
    try:
        lock_dt = datetime.fromisoformat(locked_until).replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < lock_dt
    except Exception:
        return False


def authenticate(username: str, password: str):
    """Authenticate user with brute-force lockout protection.

    Returns the user row on success, or a dict with an 'error' key explaining
    why login failed (wrong password vs account locked).
    """
    with db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE lower(username) = lower(?) AND is_active = 1",
            (username.strip(),),
        ).fetchone()
        if user is None:
            return None  # Unknown user — no info leak

        # Check lockout
        if _is_locked(user):
            return {"error": "locked", "locked_until": user["locked_until"]}

        if not verify_password(password, user["password_hash"]):
            # Increment failure counter
            attempts = (user["failed_login_attempts"] or 0) + 1
            if attempts >= MAX_LOGIN_ATTEMPTS:
                from datetime import timedelta
                lock_dt = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
                lock_until = lock_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    "UPDATE users SET failed_login_attempts = ?, locked_until = ? WHERE id = ?",
                    (attempts, lock_until, user["id"]),
                )
                log_action(conn, "account_locked", "user", user["id"],
                           {"username": user["username"], "attempts": attempts},
                           user_id="system")
                return {"error": "locked", "locked_until": lock_until}
            else:
                conn.execute(
                    "UPDATE users SET failed_login_attempts = ? WHERE id = ?",
                    (attempts, user["id"]),
                )
            return None

        # Success — reset counter and record login
        conn.execute(
            "UPDATE users SET last_login_at = datetime('now'), failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
            (user["id"],),
        )
        log_action(conn, "login", "user", user["id"], {"username": user["username"]}, user_id=user["username"])
        return user


def unlock_user(user_id: int, actor: str = "admin") -> None:
    """Admin action to clear a lockout."""
    with db() as conn:
        conn.execute(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
            (user_id,),
        )
        log_action(conn, "account_unlocked", "user", user_id, {}, user_id=actor)


def create_user(username: str, password: str, full_name: str = "", role: str = "admin") -> int:
    username = username.strip()
    if not username:
        raise ValueError("Username is required.")
    role = normalize_role(role)
    pw_hash = hash_password(password)
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO users (username, password_hash, full_name, role)
               VALUES (?, ?, ?, ?)""",
            (username, pw_hash, full_name.strip() or username, role),
        )
        user_id = cur.lastrowid
        log_action(conn, "create", "user", user_id, {"username": username, "role": role})
        return user_id


def require_user(request: Request):
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Login required")
    return user


def login_response(user_id: int, redirect_to: str = "/") -> RedirectResponse:
    response = RedirectResponse(redirect_to or "/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        make_session_token(user_id),
        httponly=True,
        samesite="lax",
        max_age=SESSION_SECONDS,
        secure=HTTPS_ENABLED,  # True in production (Fly.io HTTPS); False in local dev
    )
    return response


def logout_response() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# OTP-pending token — short-lived, issued after password check passes but
# before the TOTP code is verified.
# ---------------------------------------------------------------------------

OTP_PENDING_COOKIE = "otp_pending"
OTP_PENDING_SECONDS = 300  # 5 minutes to enter the OTP code


def make_otp_pending_token(user_id: int, next_url: str = "/") -> str:
    """Create a signed short-lived token that represents "password OK, OTP needed".

    Format: {user_id}:{issued}:{next_b64}:{hmac}
    """
    import base64 as _b64
    issued = int(datetime.now(timezone.utc).timestamp())
    # Encode next_url so it can't contain a colon and break splitting
    next_b64 = _b64.urlsafe_b64encode(next_url.encode()).decode()
    payload = f"otp:{user_id}:{issued}:{next_b64}"
    return f"{payload}:{_sign(payload)}"


def read_otp_pending_token(token: str | None) -> Optional[tuple[int, str]]:
    """Verify the OTP-pending token. Returns (user_id, next_url) or None."""
    import base64 as _b64
    if not token:
        return None
    try:
        parts = token.split(":", 4)
        if len(parts) != 5:
            return None
        prefix, user_id_s, issued_s, next_b64, sig = parts
        if prefix != "otp":
            return None
        payload = f"otp:{user_id_s}:{issued_s}:{next_b64}"
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        if int(datetime.now(timezone.utc).timestamp()) - int(issued_s) > OTP_PENDING_SECONDS:
            return None
        next_url = _b64.urlsafe_b64decode(next_b64.encode()).decode()
        return int(user_id_s), next_url
    except Exception:
        return None


def otp_pending_response(user_id: int, next_url: str = "/") -> RedirectResponse:
    """Redirect to /login/verify-otp after issuing the OTP-pending cookie."""
    response = RedirectResponse("/login/verify-otp", status_code=303)
    response.set_cookie(
        OTP_PENDING_COOKIE,
        make_otp_pending_token(user_id, next_url),
        httponly=True,
        samesite="lax",
        max_age=OTP_PENDING_SECONDS,
        secure=HTTPS_ENABLED,
    )
    return response


def maybe_bootstrap_admin_from_env() -> None:
    """Deprecated compatibility hook.

    Admin accounts must be created by the app owner through /setup.
    Environment-based admin password bootstrapping is intentionally disabled
    so packaged copies never contain assumed or default credentials.
    """
    return None


def list_users():
    with db() as conn:
        return conn.execute(
            """SELECT id, username, full_name, email, role, is_active, created_at, last_login_at,
                      department_id, company_id, is_operations_manager, notify_operations_approvals,
                      COALESCE(must_change_password, 0) AS must_change_password,
                      locked_until, COALESCE(failed_login_attempts, 0) AS failed_login_attempts,
                      COALESCE(totp_enabled, 0) AS totp_enabled
               FROM users
               ORDER BY is_active DESC, lower(username)"""
        ).fetchall()


def get_user_admin(user_id: int):
    with db() as conn:
        return conn.execute(
            """SELECT id, username, full_name, email, role, is_active, created_at, last_login_at,
                      department_id, company_id, is_operations_manager, notify_operations_approvals,
                      COALESCE(must_change_password, 0) AS must_change_password
               FROM users WHERE id = ?""",
            (user_id,),
        ).fetchone()


def require_admin(request: Request):
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def admin_create_user(
    username: str,
    password: str,
    full_name: str = "",
    role: str = "accountant",
    actor: str = "system",
    email: str = "",
    company_id: int | None = None,
    department_id: int | None = None,
    is_operations_manager: bool = False,
    notify_operations_approvals: bool = True,
    must_change_password: bool = True,
) -> int:
    role = normalize_role(role)
    validate_temporary_password(password)
    user_id = create_user(username=username, password=password, full_name=full_name, role=role)
    with db() as conn:
        conn.execute(
            """UPDATE users
                  SET email = ?, company_id = ?, department_id = ?,
                      is_operations_manager = ?, notify_operations_approvals = ?,
                      must_change_password = ?
                WHERE id = ?""",
            (
                email.strip() or None, company_id, department_id,
                1 if is_operations_manager else 0,
                1 if notify_operations_approvals else 0,
                1 if must_change_password else 0,
                user_id,
            ),
        )
        log_action(
            conn, "admin_create", "user", user_id,
            {"username": username.strip(), "role": role,
             "is_operations_manager": bool(is_operations_manager)},
            user_id=actor,
        )
    return user_id


def update_user_profile(
    user_id: int,
    full_name: str,
    role: str,
    is_active: bool,
    actor: str = "system",
    email: str = "",
    company_id: int | None = None,
    department_id: int | None = None,
    is_operations_manager: bool = False,
    notify_operations_approvals: bool = True,
) -> None:
    role = normalize_role(role)
    with db() as conn:
        conn.execute(
            """UPDATE users
                  SET full_name = ?, email = ?, role = ?, is_active = ?,
                      company_id = ?, department_id = ?,
                      is_operations_manager = ?, notify_operations_approvals = ?
                WHERE id = ?""",
            (
                full_name.strip(), email.strip() or None, role,
                1 if is_active else 0,
                company_id, department_id,
                1 if is_operations_manager else 0,
                1 if notify_operations_approvals else 0,
                user_id,
            ),
        )
        log_action(
            conn, "admin_update", "user", user_id,
            {"role": role, "is_active": bool(is_active),
             "is_operations_manager": bool(is_operations_manager)},
            user_id=actor,
        )


def invalidate_user_sessions(user_id: int, actor: str = "system") -> None:
    """Force-logout a user by invalidating all their active sessions.

    Increments session_version so every existing token for this user
    is immediately rejected.  The user's password and account status
    are not changed — only the active sessions are terminated.
    """
    with db() as conn:
        conn.execute(
            """UPDATE users
                  SET session_version = COALESCE(session_version, 0) + 1
                WHERE id = ?""",
            (user_id,),
        )
        log_action(conn, "force_logout", "user", user_id, {}, user_id=actor)


def invalidate_all_sessions(actor: str = "system") -> int:
    """Force-logout every active user by rotating all session_version values.

    This is the emergency "rotate all tokens" operation — useful when a session
    signing key may have been compromised, or as a precautionary measure after
    a security incident.

    Returns the number of active user rows updated.
    """
    with db() as conn:
        cur = conn.execute(
            """UPDATE users
                  SET session_version = COALESCE(session_version, 0) + 1
                WHERE is_active = 1"""
        )
        affected = cur.rowcount
        log_action(
            conn,
            "global_session_rotate",
            "user",
            None,
            {"affected_users": affected},
            user_id=actor,
        )
    return affected


def reset_user_password(user_id: int, actor: str = "system") -> str:
    """Reset *user_id*'s password to a system-generated temporary value.

    Returns the plaintext temporary password so the caller can show it once
    to the admin.  The password is hashed before storage; the plaintext is
    never persisted.

    session_version is incremented so all existing sessions are invalidated
    immediately — the user must log in again with the new temporary password.
    """
    tmp_password = _generate_temp_password()
    pw_hash = hash_password(tmp_password)
    with db() as conn:
        conn.execute(
            """UPDATE users
                  SET password_hash = ?, must_change_password = 1,
                      session_version = COALESCE(session_version, 0) + 1
                WHERE id = ?""",
            (pw_hash, user_id),
        )
        log_action(conn, "password_reset", "user", user_id, {}, user_id=actor)
    return tmp_password


def change_own_password(user_id: int, current_password: str, new_password: str) -> None:
    """Change a user's own password after verifying the current one.

    session_version is incremented so all *other* active sessions (e.g. other
    browsers or stolen cookies) are invalidated.  The caller is responsible for
    issuing a fresh login_response() so the current session gets a new token
    that carries the updated version.
    """
    with db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)
        ).fetchone()
        if user is None or not verify_password(current_password, user["password_hash"]):
            raise ValueError("Current password is incorrect.")
        validate_password_strength(new_password)
        pw_hash = hash_password(new_password)
        conn.execute(
            """UPDATE users
                  SET password_hash = ?, must_change_password = 0,
                      session_version = COALESCE(session_version, 0) + 1
                WHERE id = ?""",
            (pw_hash, user_id),
        )
        log_action(conn, "password_change", "user", user_id, {}, user_id=user["username"])
