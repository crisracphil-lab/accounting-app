from fastapi.testclient import TestClient

from app.auth import create_user, COOKIE_NAME, make_session_token
from app.csrf import generate_csrf_token
from app.db import db
from app.main import app

STRONG_PW = "Admin@12345"   # meets uppercase + digit + special requirements


def test_department_user_can_submit_request_and_is_restricted(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNTING_DB", str(tmp_path / "req.db"))
    from app.db import init_db
    init_db()  # Initialise schema in the isolated temp DB before any request
    client = TestClient(app)
    client.post(
        "/setup",
        data={"username": "admin", "password": STRONG_PW, "confirm_password": STRONG_PW, "full_name": "Admin"},
        follow_redirects=False,
    )
    dept_id = create_user("dept", STRONG_PW, "Dept User", "department_user")

    # Build an authenticated client for the dept user (session cookie + CSRF)
    dept = TestClient(app, cookies={COOKIE_NAME: make_session_token(dept_id)})
    csrf = generate_csrf_token(dept_id)

    # Department users are redirected to /requests (not /) when they hit the root
    r = dept.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/requests"

    # Submit a reimbursement request — returns 200 + success template (not a redirect)
    r = dept.post(
        "/requests/create",
        data={
            "request_type": "reimbursement",
            "department_name": "Operations",
            "payee_name": "Staff Member",
            "description": "Travel reimbursement",
            "amount": "1500.00",
            "csrf_token": csrf,
        },
        files={"file": ("receipt.pdf", b"%PDF-1.4 test", "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:400]}"
    assert "submitted" in r.text.lower() or "success" in r.text.lower(), \
        "Success message not found in response"

    with db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM payment_requests").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM notifications").fetchone()["n"] >= 1
