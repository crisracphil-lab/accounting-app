"""IP-based login rate limiting.

Tracks all login attempts (successful or failed) per client IP in the
login_attempts table.  If a single IP makes >= IP_RATE_LIMIT attempts
within IP_RATE_WINDOW_MINUTES, further attempts are blocked with HTTP 429.

Real-IP extraction order:
  1. X-Forwarded-For (first value, for Fly.io and other reverse proxies)
  2. request.client.host (direct connection)

Both candidates are validated against ipaddress.ip_address() before use;
any non-parseable string is rejected and the other candidate is tried.
"""
from __future__ import annotations

import ipaddress
import sqlite3
from datetime import datetime, timedelta
from typing import Tuple

from fastapi import Request

from app.config import IP_RATE_LIMIT, IP_RATE_WINDOW_MINUTES


# ── IP extraction ─────────────────────────────────────────────────────────────

def _valid_ip(candidate: str | None) -> str | None:
    """Return *candidate* if it is a syntactically valid IP address, else None."""
    if not candidate:
        return None
    # X-Forwarded-For may be a comma-separated list; take only the first value.
    first = candidate.split(",")[0].strip()
    try:
        ipaddress.ip_address(first)
        return first
    except ValueError:
        return None


def get_client_ip(request: Request) -> str:
    """Return the best available client IP address.

    Prefers the first value in X-Forwarded-For (set by Fly.io and other
    proxies) over request.client.host.  Falls back to the literal string
    "unknown" if neither yields a valid IP so that the rate-limit table
    still gets a row rather than raising an exception.
    """
    xff = request.headers.get("x-forwarded-for")
    ip = _valid_ip(xff) or _valid_ip(
        request.client.host if request.client else None
    )
    return ip or "unknown"


# ── Rate-limit logic ──────────────────────────────────────────────────────────

def check_rate_limit(conn: sqlite3.Connection, ip: str) -> Tuple[bool, int]:
    """Check whether *ip* has exceeded the rate limit.

    Returns (is_rate_limited, retry_after_seconds).

    retry_after_seconds is the number of seconds until the oldest attempt in
    the current window expires and the count drops back below the limit.
    It is 0 when the IP is not rate-limited.
    """
    window_start = datetime.utcnow() - timedelta(minutes=IP_RATE_WINDOW_MINUTES)
    window_start_str = window_start.strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        "SELECT attempted_at FROM login_attempts "
        "WHERE ip_address = ? AND attempted_at >= ? "
        "ORDER BY attempted_at ASC",
        (ip, window_start_str),
    ).fetchall()

    if len(rows) < IP_RATE_LIMIT:
        return False, 0

    # The window is full.  The oldest attempt leaves the window at
    # oldest_attempted_at + IP_RATE_WINDOW_MINUTES, giving the earliest
    # moment when a new attempt would be allowed.
    oldest_str = rows[0]["attempted_at"]
    try:
        oldest_dt = datetime.strptime(oldest_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        # Fallback: if the timestamp format differs, unblock after the full window.
        oldest_dt = datetime.utcnow() - timedelta(minutes=IP_RATE_WINDOW_MINUTES)

    retry_at = oldest_dt + timedelta(minutes=IP_RATE_WINDOW_MINUTES)
    retry_after = max(1, int((retry_at - datetime.utcnow()).total_seconds()))
    return True, retry_after


def record_attempt(conn: sqlite3.Connection, ip: str) -> None:
    """Insert a login attempt row for *ip* into login_attempts."""
    conn.execute(
        "INSERT INTO login_attempts (ip_address) VALUES (?)",
        (ip,),
    )


# ── Generic keyed rate-limit (reuses login_attempts table) ───────────────────

def check_keyed_rate_limit(
    conn: sqlite3.Connection,
    key: str,
    max_attempts: int = 10,
    window_minutes: int = 15,
) -> Tuple[bool, int]:
    """Generic rate-limit check using an arbitrary *key* string.

    The *key* is stored in the ``ip_address`` column, so composite keys such
    as ``"{ip}:change-password"`` are fully supported.

    Returns (is_rate_limited, retry_after_seconds).
    """
    window_start = datetime.utcnow() - timedelta(minutes=window_minutes)
    window_start_str = window_start.strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        "SELECT attempted_at FROM login_attempts "
        "WHERE ip_address = ? AND attempted_at >= ? "
        "ORDER BY attempted_at ASC",
        (key, window_start_str),
    ).fetchall()

    if len(rows) < max_attempts:
        return False, 0

    oldest_str = rows[0]["attempted_at"]
    try:
        oldest_dt = datetime.strptime(oldest_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        oldest_dt = datetime.utcnow() - timedelta(minutes=window_minutes)

    retry_at = oldest_dt + timedelta(minutes=window_minutes)
    retry_after = max(1, int((retry_at - datetime.utcnow()).total_seconds()))
    return True, retry_after


def record_keyed_attempt(conn: sqlite3.Connection, key: str) -> None:
    """Insert a rate-limit row for the given composite *key*."""
    conn.execute(
        "INSERT INTO login_attempts (ip_address) VALUES (?)",
        (key,),
    )
