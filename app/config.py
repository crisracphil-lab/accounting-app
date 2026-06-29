"""app/config.py — Centralized application configuration.

All environment variable reads live here.  Import constants from this module
instead of calling os.environ.get() directly in application code.

Override any value at runtime by setting the corresponding environment variable
before starting the server.  See .env.example at the project root for the full
list with descriptions.
"""
from __future__ import annotations

import os


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

#: Absolute path to the SQLite database file.
#: On Fly.io this must point to a persistent volume (e.g. /data/accounting.db).
DB_PATH: str = os.environ.get("ACCOUNTING_DB", "/data/accounting.db")


# ---------------------------------------------------------------------------
# HTTPS / TLS
# ---------------------------------------------------------------------------

#: Set to "1" in production to enable HSTS and Secure cookies.
#: Leave unset for local HTTP development.
HTTPS_ENABLED: bool = os.environ.get("ACCOUNTING_HTTPS", "").strip() == "1"


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

#: Session cookie lifetime in seconds.  Default: 12 hours.
SESSION_SECONDS: int = int(os.environ.get("ACCOUNTING_SESSION_SECONDS", "43200"))


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

#: PBKDF2-HMAC-SHA256 iteration count.  NIST recommends ≥ 210 000 for SHA-256.
#: Increasing this value after deployment invalidates no existing hashes —
#: existing passwords continue to verify against their stored iteration count.
PBKDF2_ITERATIONS: int = int(os.environ.get("ACCOUNTING_PBKDF2_ITERATIONS", "260000"))


# ---------------------------------------------------------------------------
# Brute-force / account lockout
# ---------------------------------------------------------------------------

#: Number of consecutive failed login attempts before an account is locked.
MAX_LOGIN_ATTEMPTS: int = int(os.environ.get("ACCOUNTING_MAX_LOGIN_ATTEMPTS", "5"))

#: How long (minutes) a locked account stays locked.
LOCKOUT_MINUTES: int = int(os.environ.get("ACCOUNTING_LOCKOUT_MINUTES", "15"))


# ---------------------------------------------------------------------------
# IP-level rate limiting (login endpoint)
# ---------------------------------------------------------------------------

#: Maximum login attempts per IP within the rate-limit window.
IP_RATE_LIMIT: int = int(os.environ.get("ACCOUNTING_IP_RATE_LIMIT", "20"))

#: Sliding window length (minutes) for IP rate limiting.
IP_RATE_WINDOW_MINUTES: int = int(os.environ.get("ACCOUNTING_IP_RATE_WINDOW_MINUTES", "15"))


# ---------------------------------------------------------------------------
# File uploads
# ---------------------------------------------------------------------------

#: Base directory for uploaded request attachments.
#: Defaults to <project_root>/uploads when unset.
UPLOAD_DIR: str = os.environ.get("ACCOUNTING_UPLOAD_DIR", "")


# ---------------------------------------------------------------------------
# Email / SMTP
# ---------------------------------------------------------------------------

SMTP_HOST: str = os.environ.get("SMTP_HOST", "")
SMTP_PORT: int = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER: str = os.environ.get("SMTP_USER", "")
SMTP_FROM: str = os.environ.get("SMTP_FROM", "")
SMTP_PASSWORD: str = os.environ.get("SMTP_PASSWORD", "")
SMTP_TLS: bool = os.environ.get("SMTP_TLS", "1").strip().lower() not in {
    "0", "false", "no", "off"
}


# ---------------------------------------------------------------------------
# Application base URL (used to build absolute links in email notifications)
# ---------------------------------------------------------------------------

#: Full base URL without a trailing slash, e.g. "https://bookpoint.fly.dev".
#: Leave unset for local development; email links will be relative.
APP_BASE_URL: str = os.environ.get("APP_BASE_URL", "").rstrip("/")
