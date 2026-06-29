"""Tests for the two-tier password policy.

Full strength  → validate_password_strength()  (8+ chars, uppercase, digit, special)
Relaxed        → validate_temporary_password()  (6+ chars only)

admin_create_user()  uses the relaxed policy (user forced to change on first login)
create_user()        is a raw insert; callers own validation
/setup route         uses the full policy     (first admin sets their own permanent password)
change_own_password() uses the full policy    (user choosing their own new password)
"""
import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNTING_DB", str(tmp_path / "test.db"))
    import app.db as db_module
    importlib.reload(db_module)
    db_module.init_db()
    yield db_module


# ── validate_password_strength ────────────────────────────────────────────────

class TestFullStrengthPolicy:
    def test_accepts_strong_password(self):
        from app.auth import validate_password_strength
        validate_password_strength("Admin123!")   # should not raise

    def test_rejects_too_short(self):
        from app.auth import validate_password_strength
        with pytest.raises(ValueError, match="8 characters"):
            validate_password_strength("Sh0rt!")

    def test_rejects_no_uppercase(self):
        from app.auth import validate_password_strength
        with pytest.raises(ValueError, match="uppercase"):
            validate_password_strength("nouppercase1!")

    def test_rejects_no_digit(self):
        from app.auth import validate_password_strength
        with pytest.raises(ValueError, match="number"):
            validate_password_strength("NoDigits!")

    def test_rejects_no_special_char(self):
        from app.auth import validate_password_strength
        with pytest.raises(ValueError, match="special"):
            validate_password_strength("NoSpecial1")

    def test_rejects_simple_temp_style_password(self):
        from app.auth import validate_password_strength
        # These would be fine as temp passwords but must be rejected for full policy
        with pytest.raises(ValueError):
            validate_password_strength("temp123")
        with pytest.raises(ValueError):
            validate_password_strength("Welcome1")


# ── validate_temporary_password ───────────────────────────────────────────────

class TestRelaxedPolicy:
    def test_accepts_simple_6_char_password(self):
        from app.auth import validate_temporary_password
        validate_temporary_password("temp12")      # exactly 6 chars
        validate_temporary_password("temp123")     # 7 chars
        validate_temporary_password("Welcome1")    # no special char — fine for temp

    def test_accepts_strong_password_too(self):
        from app.auth import validate_temporary_password
        validate_temporary_password("Admin123!")   # full-strength passes relaxed check too

    def test_rejects_too_short(self):
        from app.auth import validate_temporary_password
        with pytest.raises(ValueError, match="6 characters"):
            validate_temporary_password("abc")
        with pytest.raises(ValueError, match="6 characters"):
            validate_temporary_password("12345")   # 5 chars

    def test_rejects_empty(self):
        from app.auth import validate_temporary_password
        with pytest.raises(ValueError):
            validate_temporary_password("")
        with pytest.raises(ValueError):
            validate_temporary_password(None)


# ── admin_create_user uses relaxed policy ────────────────────────────────────

class TestAdminCreateUserRelaxedPolicy:
    def test_accepts_simple_temp_password(self, fresh_db):
        from app.auth import admin_create_user
        uid = admin_create_user(username="staff1", password="temp123",
                                full_name="Staff One", role="accountant")
        assert uid > 0

    def test_accepts_welcome_style_password(self, fresh_db):
        from app.auth import admin_create_user
        uid = admin_create_user(username="staff2", password="Welcome1",
                                full_name="Staff Two", role="accountant")
        assert uid > 0

    def test_accepts_6_char_minimum(self, fresh_db):
        from app.auth import admin_create_user
        uid = admin_create_user(username="staff3", password="abc123",
                                full_name="Staff Three", role="accountant")
        assert uid > 0

    def test_rejects_too_short_temp_password(self, fresh_db):
        from app.auth import admin_create_user
        with pytest.raises(ValueError, match="6 characters"):
            admin_create_user(username="staff4", password="abc",
                              full_name="Staff Four", role="accountant")

    def test_new_user_has_must_change_flag(self, fresh_db):
        from app.auth import admin_create_user
        uid = admin_create_user(username="staff5", password="temp123",
                                full_name="Staff Five", role="accountant")
        with fresh_db.db() as conn:
            row = conn.execute(
                "SELECT must_change_password FROM users WHERE id = ?", (uid,)
            ).fetchone()
        assert row["must_change_password"] == 1


# ── /setup route uses full policy ────────────────────────────────────────────

class TestSetupRouteFullPolicy:
    def test_setup_rejects_weak_password(self, fresh_db, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/setup", data={
            "username": "admin",
            "password": "temp123",        # passes relaxed but not full policy
            "confirm_password": "temp123",
            "full_name": "Admin",
        })
        # Should re-render the setup form with an error, not redirect to login
        assert resp.status_code == 200
        assert b"special" in resp.content.lower() or b"uppercase" in resp.content.lower() or b"password" in resp.content.lower()

    def test_setup_accepts_strong_password(self, fresh_db, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app, follow_redirects=False)
        resp = client.post("/setup", data={
            "username": "admin",
            "password": "Admin123!",
            "confirm_password": "Admin123!",
            "full_name": "Admin",
        })
        # Should redirect to / after successful setup
        assert resp.status_code == 303


# ── change_own_password uses full policy ─────────────────────────────────────

class TestChangeOwnPasswordFullPolicy:
    def test_rejects_weak_new_password(self, fresh_db):
        from app.auth import admin_create_user, change_own_password
        uid = admin_create_user(username="chgpw", password="temp123",
                                full_name="Change Me", role="accountant")
        with pytest.raises(ValueError):
            change_own_password(uid, "temp123", "simplepw")   # weak new password

    def test_accepts_strong_new_password(self, fresh_db):
        from app.auth import admin_create_user, change_own_password
        uid = admin_create_user(username="chgpw2", password="temp123",
                                full_name="Change Me Too", role="accountant")
        change_own_password(uid, "temp123", "NewStrong1!")   # should not raise
        with fresh_db.db() as conn:
            row = conn.execute(
                "SELECT must_change_password FROM users WHERE id = ?", (uid,)
            ).fetchone()
        assert row["must_change_password"] == 0
