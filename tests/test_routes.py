"""HTTP-level route tests for BookPoint.

Uses FastAPI's TestClient against the live application with an isolated
per-class SQLite database.  No mocks, no dummy data — every assertion is
against real app behaviour.

DB isolation technique
──────────────────────
importlib.reload(app.db) re-executes db.py with ACCOUNTING_DB pointing at a
temp file.  All Python functions look up module-level globals (like DB_PATH)
at call time via __globals__, which is a reference to the *same* module dict
that reload() updates in place.  So every piece of app code that calls db()
— including code already imported by routers — transparently uses the
isolated temp database for the duration of each test.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from app.main import app  # imported once; DB_PATH swapped per fixture via reload
from app.auth import COOKIE_NAME, make_session_token


# ── helpers ───────────────────────────────────────────────────────────────────

def _csrf(user_id: int) -> str:
    """Generate a fresh valid CSRF token for *user_id*."""
    from app.csrf import generate_csrf_token
    return generate_csrf_token(user_id)


def _cookie(user_id: int) -> dict[str, str]:
    """Return a cookies dict with a valid session for *user_id*."""
    return {COOKIE_NAME: make_session_token(user_id)}


def _create_user(role: str = "admin", username: str | None = None,
                 password: str = "Admin123!", must_change: bool = False) -> int:
    """Insert a user directly into the active DB; return its id."""
    from app.auth import create_user
    import app.db as _db
    uname = username or f"user_{role}"
    uid = create_user(username=uname, password=password,
                      full_name=uname, role=role)
    if not must_change:
        with _db.db() as conn:
            conn.execute("UPDATE users SET must_change_password = 0 WHERE id = ?", (uid,))
    return uid


def _minimal_xlsx() -> bytes:
    """Return bytes of a tiny xlsx with valid bank statement columns."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Date", "Description", "Amount"])
    ws.append(["2024-01-15", "Test payment", "100.00"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_setup(tmp_path, monkeypatch):
    """Isolated temp SQLite DB; reload app.db so every route uses it."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("ACCOUNTING_DB", str(db_path))
    import app.db as db_module
    importlib.reload(db_module)
    db_module.init_db()
    yield db_module
    monkeypatch.delenv("ACCOUNTING_DB", raising=False)


@pytest.fixture()
def client(db_setup):
    """TestClient that does NOT follow redirects (so we can assert 3xx codes)."""
    return TestClient(app, follow_redirects=False)


@pytest.fixture()
def admin_client(db_setup):
    """TestClient pre-authenticated as a fresh admin user (no forced pw change)."""
    uid = _create_user(role="admin", username="admin", must_change=False)
    c = TestClient(app, follow_redirects=False, cookies=_cookie(uid))
    c._admin_id = uid  # type: ignore[attr-defined]
    return c


# ── Authentication flow ───────────────────────────────────────────────────────

class TestAuthFlow:
    def test_setup_form_shows_when_no_users(self, client):
        r = client.get("/setup")
        assert r.status_code == 200, r.text

    def test_setup_creates_admin_and_redirects_to_login(self, client):
        r = client.post("/setup", data={
            "username": "boss",
            "password": "Boss1234!",
            "confirm_password": "Boss1234!",
            "full_name": "Boss User",
        })
        # Successful setup redirects (302/303) to /login or /
        assert r.status_code in (302, 303), f"Expected redirect, got {r.status_code}"

    def test_login_correct_credentials_sets_session_cookie(self, db_setup):
        _create_user(role="admin", username="alice", password="Alice123!")
        c = TestClient(app, follow_redirects=False)
        r = c.post("/login", data={"username": "alice", "password": "Alice123!", "next": "/"})
        assert r.status_code in (302, 303), f"Expected redirect, got {r.status_code}"
        assert COOKIE_NAME in r.cookies, "Session cookie not set after successful login"

    def test_login_wrong_password_returns_200_with_error(self, db_setup):
        _create_user(role="admin", username="bob", password="Bob12345!")
        c = TestClient(app, follow_redirects=False)
        r = c.post("/login", data={"username": "bob", "password": "wrong", "next": "/"})
        assert r.status_code == 200
        assert "invalid" in r.text.lower() or "password" in r.text.lower(), \
            "Expected error message in login response"

    def test_login_lockout_after_five_wrong_attempts(self, db_setup):
        _create_user(role="admin", username="charlie", password="Charlie1!")
        c = TestClient(app, follow_redirects=False)
        for _ in range(5):
            c.post("/login", data={"username": "charlie", "password": "bad", "next": "/"})
        # 6th attempt — account should now be locked
        r = c.post("/login", data={"username": "charlie", "password": "bad", "next": "/"})
        assert r.status_code == 200
        assert "lock" in r.text.lower() or "attempt" in r.text.lower(), \
            "Expected lockout message after 5 failed logins"

    def test_unauthenticated_get_root_redirects_to_login(self, db_setup):
        # Ensure at least one user exists so middleware redirects to /login not /setup
        _create_user(role="admin", username="dave", password="Dave1234!")
        c = TestClient(app, follow_redirects=False)
        r = c.get("/")
        assert r.status_code in (302, 303), f"Expected redirect, got {r.status_code}"
        assert "login" in r.headers.get("location", ""), \
            f"Expected redirect to /login, got: {r.headers.get('location')}"

    def test_authenticated_get_root_returns_200(self, admin_client):
        r = admin_client.get("/")
        assert r.status_code == 200, \
            f"Expected dashboard 200, got {r.status_code}: {r.text[:200]}"


# ── Role enforcement ──────────────────────────────────────────────────────────

class TestRoleEnforcement:
    def test_viewer_cannot_post_to_suppliers_create(self, db_setup):
        uid = _create_user(role="viewer", username="viewer1", must_change=False)
        c = TestClient(app, follow_redirects=False, cookies=_cookie(uid))
        r = c.post("/suppliers/create", data={
            "name": "Evil Supplier",
            "csrf_token": _csrf(uid),
        })
        # require_admin raises HTTPException(403); friendly handler returns 403
        assert r.status_code == 403, \
            f"Viewer should be blocked from creating suppliers, got {r.status_code}"

    def test_department_user_redirected_away_from_accounting_pages(self, db_setup):
        uid = _create_user(role="department_user", username="dept1", must_change=False)
        c = TestClient(app, follow_redirects=False, cookies=_cookie(uid))
        # /transactions is an accounting-only page
        r = c.get("/transactions")
        assert r.status_code in (302, 303), \
            f"department_user should be redirected from /transactions, got {r.status_code}"
        assert r.headers.get("location", "").startswith("/requests"), \
            f"Expected redirect to /requests, got: {r.headers.get('location')}"

    def test_department_user_can_access_requests(self, db_setup):
        uid = _create_user(role="department_user", username="dept2", must_change=False)
        c = TestClient(app, follow_redirects=False, cookies=_cookie(uid))
        r = c.get("/requests")
        assert r.status_code == 200, \
            f"department_user should be able to access /requests, got {r.status_code}"

    def test_department_user_can_access_notifications(self, db_setup):
        uid = _create_user(role="department_user", username="dept3", must_change=False)
        c = TestClient(app, follow_redirects=False, cookies=_cookie(uid))
        r = c.get("/notifications")
        assert r.status_code == 200, \
            f"department_user should be able to access /notifications, got {r.status_code}"


# ── Core accounting routes (admin) ────────────────────────────────────────────

class TestAccountingRoutes:
    def test_transactions_returns_200(self, admin_client):
        r = admin_client.get("/transactions")
        assert r.status_code == 200, r.text[:200]

    def test_journal_entries_returns_200(self, admin_client):
        r = admin_client.get("/journal-entries")
        assert r.status_code == 200, r.text[:200]

    def test_suppliers_returns_200(self, admin_client):
        r = admin_client.get("/suppliers")
        assert r.status_code == 200, r.text[:200]

    def test_audit_log_returns_200(self, admin_client):
        r = admin_client.get("/audit-log")
        assert r.status_code == 200, r.text[:200]

    def test_api_health_returns_ok(self, admin_client):
        r = admin_client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["database"] == "reachable"

    def test_api_dashboard_stats_returns_expected_keys(self, admin_client):
        r = admin_client.get("/api/dashboard/stats")
        assert r.status_code == 200
        body = r.json()
        expected_keys = {
            "uploads", "transactions", "draft_journal_entries",
            "approved_journal_entries", "payment_instructions", "closing_runs",
        }
        missing = expected_keys - set(body.keys())
        assert not missing, f"Missing keys in /api/dashboard/stats: {missing}"

    def test_api_dashboard_stats_values_are_non_negative_ints(self, admin_client):
        r = admin_client.get("/api/dashboard/stats")
        body = r.json()
        for key, val in body.items():
            assert isinstance(val, int) and val >= 0, \
                f"/api/dashboard/stats[{key!r}] = {val!r}, expected non-negative int"


# ── Upload protection ─────────────────────────────────────────────────────────

class TestUploadProtection:
    def test_non_xlsx_upload_returns_error_not_500(self, admin_client):
        uid = admin_client._admin_id  # type: ignore[attr-defined]
        r = admin_client.post(
            "/upload",
            data={"company_id": "1", "csrf_token": _csrf(uid)},
            files={"file": ("report.pdf", b"%PDF-1.4 fake content", "application/pdf")},
        )
        # Must NOT be 500; the route should return the upload form with an error
        assert r.status_code != 500, "Upload of unsupported file type caused a 500"
        assert r.status_code == 200, f"Expected 200 with error, got {r.status_code}"
        assert "error" in r.text.lower() or "supported" in r.text.lower(), \
            "Expected an error message for unsupported file type"

    def test_duplicate_upload_returns_error_not_500(self, admin_client, db_setup):
        """Upload a file once successfully, then upload the same bytes again.

        The duplicate-hash check now runs before parsing, so the second POST
        returns a 'Duplicate upload' error regardless of whether the file
        contents are a real bank statement.
        """
        uid = admin_client._admin_id  # type: ignore[attr-defined]
        xlsx_bytes = _minimal_xlsx()
        sha = hashlib.sha256(xlsx_bytes).hexdigest()

        # Pre-seed the uploaded_files table with this file's SHA-256 to simulate
        # a prior successful upload — avoids needing a fully parseable statement.
        with db_setup.db() as conn:
            conn.execute(
                "INSERT INTO uploaded_files "
                "(filename, file_type, file_size, sha256, bank_account, period_covered, company_id) "
                "VALUES (?, ?, ?, ?, NULL, NULL, 1)",
                ("prior_upload.xlsx", "xlsx", len(xlsx_bytes), sha),
            )

        r = admin_client.post(
            "/upload",
            data={"company_id": "1", "csrf_token": _csrf(uid)},
            files={"file": ("prior_upload.xlsx", xlsx_bytes,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert r.status_code != 500, "Duplicate upload caused a 500"
        assert r.status_code == 200, f"Expected 200 with error, got {r.status_code}"
        assert "duplicate" in r.text.lower(), \
            f"Expected 'duplicate' in response for re-upload. Body excerpt: {r.text[:400]}"


# ── IP-based rate limiting ────────────────────────────────────────────────────

class TestIPRateLimit:
    """POST /login must return 429 with Retry-After once an IP hits 20 attempts."""

    _TEST_IP = "203.0.113.42"  # TEST-NET-3, RFC 5737 — never a real address

    def test_21st_attempt_from_same_ip_returns_429(self, db_setup):
        """Make 20 login attempts via X-Forwarded-For, then assert the 21st is 429."""
        _create_user(role="admin", username="rl_admin", password="Admin123!",
                     must_change=False)
        c = TestClient(app, follow_redirects=False,
                       headers={"X-Forwarded-For": self._TEST_IP})

        # 20 attempts with a non-existent username so per-account lockout
        # (5 failures on the same account) is never triggered.
        for i in range(20):
            r = c.post("/login", data={
                "username": f"ghost_user_{i}",
                "password": "wrong",
                "next": "/",
            })
            assert r.status_code == 200, (
                f"Attempt {i + 1} unexpectedly returned {r.status_code} "
                f"(expected 200 with login error)"
            )

        # 21st attempt must be rate-limited.
        r21 = c.post("/login", data={
            "username": "ghost_user_x",
            "password": "wrong",
            "next": "/",
        })
        assert r21.status_code == 429, (
            f"Expected HTTP 429 on 21st attempt, got {r21.status_code}"
        )
        assert "retry-after" in r21.headers, (
            "HTTP 429 response must include a Retry-After header"
        )
        retry_after = int(r21.headers["retry-after"])
        assert 1 <= retry_after <= 15 * 60, (
            f"Retry-After {retry_after}s is outside the expected 1–900 second range"
        )
        assert "too many" in r21.text.lower(), (
            "429 response body should mention 'too many' login attempts"
        )

    def test_different_ips_have_independent_limits(self, db_setup):
        """Rate limit for IP A must not affect IP B."""
        _create_user(role="admin", username="rl_admin2", password="Admin123!",
                     must_change=False)

        ip_a = "203.0.113.1"
        ip_b = "203.0.113.2"

        # Exhaust the limit for IP A.
        c_a = TestClient(app, follow_redirects=False,
                         headers={"X-Forwarded-For": ip_a})
        for i in range(20):
            c_a.post("/login", data={
                "username": f"ghost_{i}",
                "password": "wrong",
                "next": "/",
            })

        # IP A is now blocked.
        r_a = c_a.post("/login", data={
            "username": "ghost_x", "password": "wrong", "next": "/"
        })
        assert r_a.status_code == 429, "IP A should be rate-limited after 20 attempts"

        # IP B has made zero attempts — must still get the login form (200).
        c_b = TestClient(app, follow_redirects=False,
                         headers={"X-Forwarded-For": ip_b})
        r_b = c_b.post("/login", data={
            "username": "ghost_x", "password": "wrong", "next": "/"
        })
        assert r_b.status_code == 200, (
            f"IP B should not be rate-limited, but got {r_b.status_code}"
        )
