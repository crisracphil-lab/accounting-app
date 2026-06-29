"""tests/test_new_features.py — Integration tests for Tasks 11-13.

Covers:
  * Keyed rate limiting (check_keyed_rate_limit / record_keyed_attempt)
  * Force-logout (invalidate_user_sessions) — per-user session invalidation
  * Global session rotation (invalidate_all_sessions) — all-user invalidation
  * Structured JSON log output (log_security_event / log_error_event)
"""
from __future__ import annotations

import importlib
import json
import os
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(monkeypatch, tmp_path):
    """Fresh SQLite DB + reloaded modules for each test."""
    db_path = str(tmp_path / "test_features.db")
    monkeypatch.setenv("ACCOUNTING_DB", db_path)

    import app.config as cfg
    importlib.reload(cfg)
    import app.db as _db
    importlib.reload(_db)
    _db.init_db()

    import app.auth as _auth
    importlib.reload(_auth)

    return db_path


@pytest.fixture()
def two_users(isolated_db):
    """Create two active users and return (user_id_a, user_id_b)."""
    import app.auth as auth
    uid_a = auth.create_user("alice", "Alice1234!", "Alice", "admin")
    uid_b = auth.create_user("bob", "Bob1234!!", "Bob", "accountant")
    return uid_a, uid_b


# ---------------------------------------------------------------------------
# Keyed rate-limit tests
# ---------------------------------------------------------------------------

class TestKeyedRateLimit:

    def _conn(self, isolated_db):
        import app.db as _db
        importlib.reload(_db)
        import sqlite3
        conn = sqlite3.connect(isolated_db)
        conn.row_factory = sqlite3.Row
        return conn

    def test_not_limited_initially(self, isolated_db):
        from app.services.ip_ratelimit import check_keyed_rate_limit
        import sqlite3
        conn = sqlite3.connect(isolated_db)
        conn.row_factory = sqlite3.Row
        limited, retry = check_keyed_rate_limit(conn, "1.2.3.4:change-password", max_attempts=5)
        conn.close()
        assert limited is False
        assert retry == 0

    def test_limited_after_max_attempts(self, isolated_db):
        from app.services.ip_ratelimit import check_keyed_rate_limit, record_keyed_attempt
        import sqlite3
        conn = sqlite3.connect(isolated_db)
        conn.row_factory = sqlite3.Row
        key = "1.2.3.4:change-password"
        for _ in range(5):
            record_keyed_attempt(conn, key)
            conn.commit()
        limited, retry = check_keyed_rate_limit(conn, key, max_attempts=5)
        conn.close()
        assert limited is True
        assert retry > 0

    def test_different_keys_are_independent(self, isolated_db):
        """Rate-limiting key A must not bleed into key B."""
        from app.services.ip_ratelimit import check_keyed_rate_limit, record_keyed_attempt
        import sqlite3
        conn = sqlite3.connect(isolated_db)
        conn.row_factory = sqlite3.Row
        key_a = "10.0.0.1:change-password"
        key_b = "10.0.0.1:reset-password"
        for _ in range(10):
            record_keyed_attempt(conn, key_a)
            conn.commit()
        limited_a, _ = check_keyed_rate_limit(conn, key_a, max_attempts=10)
        limited_b, _ = check_keyed_rate_limit(conn, key_b, max_attempts=10)
        conn.close()
        assert limited_a is True
        assert limited_b is False

    def test_retry_after_is_positive_seconds(self, isolated_db):
        from app.services.ip_ratelimit import check_keyed_rate_limit, record_keyed_attempt
        import sqlite3
        conn = sqlite3.connect(isolated_db)
        conn.row_factory = sqlite3.Row
        key = "9.9.9.9:change-password"
        for _ in range(3):
            record_keyed_attempt(conn, key)
            conn.commit()
        limited, retry = check_keyed_rate_limit(conn, key, max_attempts=3, window_minutes=15)
        conn.close()
        assert limited is True
        assert 1 <= retry <= 15 * 60


# ---------------------------------------------------------------------------
# Force-logout (per-user session invalidation) tests
# ---------------------------------------------------------------------------

class TestForceLogout:

    def test_invalidate_user_sessions_invalidates_token(self, two_users):
        from app.auth import (
            invalidate_user_sessions,
            make_session_token,
            read_session_token,
        )
        uid_a, uid_b = two_users
        token = make_session_token(uid_a)
        assert read_session_token(token) == uid_a

        invalidate_user_sessions(uid_a, actor="admin")
        assert read_session_token(token) is None

    def test_invalidate_one_user_does_not_affect_other(self, two_users):
        from app.auth import (
            invalidate_user_sessions,
            make_session_token,
            read_session_token,
        )
        uid_a, uid_b = two_users
        token_a = make_session_token(uid_a)
        token_b = make_session_token(uid_b)

        invalidate_user_sessions(uid_a, actor="admin")

        assert read_session_token(token_a) is None   # A's token gone
        assert read_session_token(token_b) == uid_b  # B's token intact

    def test_new_token_valid_after_force_logout(self, two_users):
        from app.auth import (
            invalidate_user_sessions,
            make_session_token,
            read_session_token,
        )
        uid_a, _ = two_users
        old_token = make_session_token(uid_a)
        invalidate_user_sessions(uid_a, actor="admin")

        new_token = make_session_token(uid_a)
        assert read_session_token(old_token) is None
        assert read_session_token(new_token) == uid_a


# ---------------------------------------------------------------------------
# Global session rotation tests
# ---------------------------------------------------------------------------

class TestGlobalSessionRotation:

    def test_invalidate_all_sessions_logs_out_all_users(self, two_users):
        from app.auth import (
            invalidate_all_sessions,
            make_session_token,
            read_session_token,
        )
        uid_a, uid_b = two_users
        token_a = make_session_token(uid_a)
        token_b = make_session_token(uid_b)
        assert read_session_token(token_a) == uid_a
        assert read_session_token(token_b) == uid_b

        affected = invalidate_all_sessions(actor="admin")

        assert read_session_token(token_a) is None
        assert read_session_token(token_b) is None
        assert affected == 2  # both users affected

    def test_new_tokens_work_after_global_rotation(self, two_users):
        from app.auth import (
            invalidate_all_sessions,
            make_session_token,
            read_session_token,
        )
        uid_a, uid_b = two_users
        invalidate_all_sessions(actor="admin")

        new_a = make_session_token(uid_a)
        new_b = make_session_token(uid_b)
        assert read_session_token(new_a) == uid_a
        assert read_session_token(new_b) == uid_b

    def test_invalidate_all_returns_affected_count(self, two_users):
        from app.auth import invalidate_all_sessions
        count = invalidate_all_sessions(actor="system")
        assert count == 2


# ---------------------------------------------------------------------------
# JSON logging tests
# ---------------------------------------------------------------------------

class TestJsonLogging:

    def test_log_security_event_emits_valid_json(self, tmp_path, monkeypatch):
        """log_security_event should write a single valid JSON line."""
        import logging
        import app.services.log_service as ls

        # Redirect the security logger to a test file
        test_log = tmp_path / "security_test.log"
        handler = logging.FileHandler(str(test_log), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        ls._security_logger.addHandler(handler)
        try:
            ls.log_security_event(
                "login_failed",
                ip="1.2.3.4",
                username="testuser",
            )
            handler.flush()
        finally:
            ls._security_logger.removeHandler(handler)
            handler.close()

        lines = test_log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1

        record = json.loads(lines[0])
        assert record["event"] == "login_failed"
        assert record["ip"] == "1.2.3.4"
        assert record["username"] == "testuser"
        assert record["level"] == "SECURITY"
        assert "ts" in record

    def test_log_error_event_emits_valid_json(self, tmp_path):
        """log_error_event should write a single valid JSON line."""
        import logging
        import app.services.log_service as ls

        test_log = tmp_path / "error_test.log"
        handler = logging.FileHandler(str(test_log), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        ls._error_logger.addHandler(handler)
        try:
            ls.log_error_event("ABCD1234", "POST /some/path", "Traceback...")
            handler.flush()
        finally:
            ls._error_logger.removeHandler(handler)
            handler.close()

        lines = test_log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1

        record = json.loads(lines[0])
        assert record["event"] == "unhandled_exception"
        assert record["error_id"] == "ABCD1234"
        assert record["path"] == "POST /some/path"
        assert record["traceback"] == "Traceback..."
        assert record["level"] == "ERROR"
        assert "ts" in record

    def test_security_event_ts_format(self, tmp_path):
        """Timestamp must be ISO-8601 UTC (ends with Z)."""
        import logging
        import app.services.log_service as ls

        test_log = tmp_path / "ts_test.log"
        handler = logging.FileHandler(str(test_log), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        ls._security_logger.addHandler(handler)
        try:
            ls.log_security_event("test_event")
            handler.flush()
        finally:
            ls._security_logger.removeHandler(handler)
            handler.close()

        record = json.loads(test_log.read_text(encoding="utf-8").strip())
        assert record["ts"].endswith("Z")
        # Basic ISO-8601 shape: YYYY-MM-DDTHH:MM:SSZ
        assert len(record["ts"]) == 20
