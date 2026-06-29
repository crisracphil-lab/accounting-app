"""app/csrf.py — CSRF protection via HMAC-SHA256 double-submit tokens.

Token format (colon-delimited, no colons inside any segment):
    {user_id}:{unix_timestamp}:{url-safe nonce}:{hmac_hex}

Tokens are tied to the authenticated user's ID and expire after 1 hour.
Constant-time comparison prevents timing attacks.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

# Imported lazily to avoid a circular import at module load time if csrf.py
# is ever imported before auth.py has finished initialising.
def _secret() -> str:
    from app.auth import SECRET  # noqa: PLC0415
    return SECRET


# Tokens are valid for 1 hour; a fresh token is minted on every page load.
CSRF_MAX_AGE = 3600


def generate_csrf_token(session_user_id: int) -> str:
    """Return a signed CSRF token bound to *session_user_id*.

    Format: ``{user_id}:{timestamp}:{nonce}:{hmac_hex}``
    """
    nonce = secrets.token_urlsafe(16)   # 22-char base64url — no colons
    ts = int(time.time())
    payload = f"{session_user_id}:{ts}:{nonce}"
    sig = hmac.new(
        _secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}:{sig}"


def validate_csrf_token(token: str, session_user_id: int) -> bool:
    """Return ``True`` iff *token* is valid, unexpired, and matches *session_user_id*.

    Uses constant-time comparison throughout to prevent timing side-channels.
    """
    if not token:
        return False
    try:
        user_id_s, ts_s, nonce, sig = token.split(":", 3)
        # User-ID binding — compare as integers to avoid string-padding tricks.
        if int(user_id_s) != int(session_user_id):
            return False
        # Expiry check — accept up to CSRF_MAX_AGE seconds of clock drift.
        if int(time.time()) - int(ts_s) > CSRF_MAX_AGE:
            return False
        payload = f"{user_id_s}:{ts_s}:{nonce}"
        expected = hmac.new(
            _secret().encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        # Constant-time comparison — both operands are hex strings of equal length.
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False
