"""app/services/log_service.py — Structured JSON logging for BookPoint.

Two rotating log files are maintained:
  data/error_logs/errors.log    — unhandled application exceptions
  data/security_logs/security.log — auth failures, CSRF rejections, rate-limit hits

Every line is a self-contained JSON object, making logs trivially
machine-parseable by log aggregators (Loki, CloudWatch, Datadog, etc.).

Public API
----------
log_error_event(error_id, path, traceback_str)
log_security_event(event, **kwargs)
"""
from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_json_logger(
    name: str,
    log_dir: Path,
    filename: str,
    level: int,
) -> logging.Logger:
    """Create a RotatingFileHandler logger that writes one JSON line per record."""
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        filename=str(log_dir / filename),
        maxBytes=200 * 1024,   # 200 KB per file
        backupCount=5,
        encoding="utf-8",
        delay=True,
    )
    # Emit only the pre-formatted message; callers build the full JSON string.
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


_DATA_DIR = Path(__file__).parent.parent.parent / "data"

_error_logger = _make_json_logger(
    "bookpoint.errors",
    _DATA_DIR / "error_logs",
    "errors.log",
    logging.ERROR,
)

_security_logger = _make_json_logger(
    "bookpoint.security",
    _DATA_DIR / "security_logs",
    "security.log",
    logging.WARNING,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_error_event(error_id: str, path: str, traceback_str: str) -> None:
    """Emit a structured JSON error record.

    Parameters
    ----------
    error_id:      Short hex digest identifying this error class.
    path:          ``METHOD /url/path`` — no query strings.
    traceback_str: Full Python traceback as a string.
    """
    _error_logger.error(
        json.dumps(
            {
                "ts": _now(),
                "level": "ERROR",
                "event": "unhandled_exception",
                "error_id": error_id,
                "path": path,
                "traceback": traceback_str,
            },
            default=str,
        )
    )


def log_security_event(event: str, **kwargs: Any) -> None:
    """Emit a structured JSON security event record.

    Parameters
    ----------
    event:   Short snake_case identifier, e.g. ``"login_failed"``.
    **kwargs: Arbitrary key/value context — ip, username, action, path, etc.

    Examples
    --------
    >>> log_security_event("login_failed", ip="1.2.3.4", username="admin")
    >>> log_security_event("csrf_rejected", ip="1.2.3.4", path="/users/1/update")
    >>> log_security_event("rate_limit_hit", ip="1.2.3.4", action="change-password",
    ...                    retry_after=720)
    """
    _security_logger.warning(
        json.dumps(
            {"ts": _now(), "level": "SECURITY", "event": event, **kwargs},
            default=str,
        )
    )
