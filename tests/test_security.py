"""tests/test_security.py — CSRF token and session token security tests.

Covers:
  * CSRF token generation, validation, expiry, and tamper detection
  * Session token creation, validation, and expiry
  * Session invalidation via session_version bump on password change
  * Backward-compatibility of legacy 4-part session tokens
"""
from __future__ import annotations

import importlib
import os
import tempfile
import time

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(monkeypatch, tmp_path):
    """Provide a fresh, initialised in-memory-like SQLite DB for each test.

    We use a per-test temp file (not ":memory:") because the connection is
    opened fresh for each db() context manager call, and :memory: databases
    are per-connection.
    """
    db_path = str(tmp_path / "test_security.db")
    monkeypatch.setenv("ACCOUNTING_DB", db_path)

    # Re-import config and db to pick up the new env var.
    import app.config as cfg
    importlib.reload(cfg)
    import app.db as _db
    importlib.reload(_db)
    _db.init_db()

    # Re-import auth so it uses the reloaded db module.
    import app.auth as _auth
    importlib.reload(_auth)
    import app.csrf as _csrf
    importlib.reload(_csrf)

    return db_path


@pytest.fixture()
def test_user(isolated_db):
    """Create a test admin user and return their user_id."""
    import app.auth as auth
    user_id = auth.create_user(
        username="testadmin",
        password="Admin1234!",
        full_name="Test Admin",
        role="admin",
    )
    return user_id


# ---------------------------------------------------------------------------
# CSRF token tests
# ---------------------------------------------------------------------------

class TestCsrfToken:

    def test_valid_token_passes(self, test_user):
        from app.csrf import generate_csrf_token, validate_csrf_token
        token = generate_csrf_token(test_user)
        assert validate_csrf_token(token, test_user) is True

    def test_wrong_user_rejected(self, test_user):
        from app.csrf import generate_csrf_token, validate_csrf_token
        token = generate_csrf_token(test_user)
        # Token was issued for test_user; validating against a different id must fail.
        assert validate_csrf_token(token, test_user + 999) is False

    def test_tampered_hmac_rejected(self, test_user):
        from app.csrf import generate_csrf_token, validate_csrf_token
        token = generate_csrf_token(test_user)
        # Flip the last character to corrupt the HMAC.
        corrupted = token[:-1] + ("a" if token[-1] != "a" else "b")
        assert validate_csrf_token(corrupted, test_user) is False

    def test_expired_token_rejected(self, test_user, monkeypatch):
        """Freeze time forward past the 1-hour CSRF expiry."""
        import app.csrf as csrf_module
        token = csrf_module.generate_csrf_token(test_user)

        # Patch time so the token looks 2 hours old.
        original_time = time.time
        monkeypatch.setattr(time, "time", lambda: original_time() + 7201)

        assert csrf_module.validate_csrf_token(token, test_user) is False

    def test_empty_token_rejected(self, test_user):
        from app.csrf import validate_csrf_token
        assert validate_csrf_token("", test_user) is False

    def test_malformed_token_rejected(self, test_user):
        from app.csrf import validate_csrf_token
        assert validate_csrf_token("not:a:valid:token:at:all:extra", test_user) is False


# ---------------------------------------------------------------------------
# Session token tests
# ---------------------------------------------------------------------------

class TestSessionToken:

    def test_valid_token_returns_user_id(self, test_user):
        from app.auth import make_session_token, read_session_token
        token = make_session_token(test_user)
        assert read_session_token(token) == test_user

    def test_empty_token_returns_none(self, isolated_db):
        from app.auth import read_session_token
        assert read_session_token(None) is None
        assert read_session_token("") is None

    def test_tampered_sig_rejected(self, test_user):
        from app.auth import make_session_token, read_session_token
        token = make_session_token(test_user)
        corrupted = token[:-1] + ("a" if token[-1] != "a" else "b")
        assert read_session_token(corrupted) is None

    def test_expired_token_rejected(self, test_user, monkeypatch):
        import app.auth as auth_module
        token = auth_module.make_session_token(test_user)

        # Patch SESSION_SECONDS to 1 second, then let the token age out.
        monkeypatch.setattr(auth_module, "SESSION_SECONDS", 1)
        time.sleep(1.1)
        assert auth_module.read_session_token(token) is None

    def test_legacy_4part_token_accepted(self, test_user, monkeypatch):
        """4-part tokens (pre-session_version) must still validate."""
        import hmac as _hmac
        import hashlib
        import secrets
        from datetime import datetime, timezone
        from app.auth import SESSION_SECONDS, SECRET, read_session_token

        issued = int(datetime.now(timezone.utc).timestamp())
        nonce = secrets.token_urlsafe(12)
        payload = f"{test_user}:{issued}:{nonce}"
        sig = _hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        legacy_token = f"{payload}:{sig}"

        assert read_session_token(legacy_token) == test_user


# ---------------------------------------------------------------------------
# Session invalidation on password change
# ---------------------------------------------------------------------------

class TestSessionInvalidation:

    def test_token_invalid_after_own_password_change(self, test_user):
        from app.auth import (
            change_own_password,
            make_session_token,
            read_session_token,
        )
        token = make_session_token(test_user)
        assert read_session_token(token) == test_user  # valid before change

        change_own_password(test_user, "Admin1234!", "NewPass99!")
        # Old token must now be rejected because session_version was bumped.
        assert read_session_token(token) is None

    def test_token_invalid_after_admin_password_reset(self, test_user):
        from app.auth import (
            make_session_token,
            read_session_token,
            reset_user_password,
        )
        token = make_session_token(test_user)
        assert read_session_token(token) == test_user

        reset_user_password(test_user, actor="admin")
        # Admin reset must also invalidate existing tokens.
        assert read_session_token(token) is None

    def test_new_token_valid_after_password_change(self, test_user):
        """After a password change, a freshly minted token must work."""
        from app.auth import (
            change_own_password,
            make_session_token,
            read_session_token,
        )
        change_own_password(test_user, "Admin1234!", "NewPass99!")
        new_token = make_session_token(test_user)
        assert read_session_token(new_token) == test_user

    def test_multiple_resets_each_increment_version(self, test_user):
        """Two consecutive resets must each produce a different, working token."""
        from app.auth import (
            make_session_token,
            read_session_token,
            reset_user_password,
        )
        token1 = make_session_token(test_user)
        reset_user_password(test_user, actor="admin")
        token2 = make_session_token(test_user)  # new token after first reset

        assert read_session_token(token1) is None   # first token invalidated
        assert read_session_token(token2) == test_user  # second token valid

        reset_user_password(test_user, actor="admin")
        assert read_session_token(token2) is None   # second token now invalidated
