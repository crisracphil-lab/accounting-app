"""Temporary debug test."""
import importlib, os
from fastapi.testclient import TestClient


def load_app(tmp_path):
    os.environ["ACCOUNTING_DB"] = str(tmp_path / "feature.db")
    import app.db as db_mod
    import app.auth as auth_mod
    import app.main as main_mod
    importlib.reload(db_mod)
    importlib.reload(auth_mod)
    importlib.reload(main_mod)
    return main_mod.app


def login_admin(client):
    r = client.post("/setup", data={
        "username": "admin", "full_name": "Admin",
        "password": "Password123!", "confirm_password": "Password123!",
    }, follow_redirects=False)
    assert r.status_code == 303


def _get_admin_id():
    import app.db as db_mod
    with db_mod.db() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    return row["id"]


def _csrf(user_id):
    from app.csrf import generate_csrf_token
    return generate_csrf_token(user_id)


# Copy-exact of the failing test
def test_supplier_master_is_editable_by_logged_in_user(tmp_path):
    client = TestClient(load_app(tmp_path))
    login_admin(client)
    uid = _get_admin_id()
    page = client.get("/suppliers")
    assert page.status_code == 200
    assert "Create supplier" in page.text

    created = client.post("/suppliers/create", data={
        "name": "Sample Vendor Inc",
        "tin": "123-456-789-00000",
        "default_expense_account_id": "0",
        "aliases": "Sample Vendor, SVI",
        "is_active": "1",
        "csrf_token": _csrf(uid),
    }, follow_redirects=False)
    assert created.status_code == 303, f"Expected 303, got {created.status_code}"
    page = client.get("/suppliers")
    assert "Sample Vendor Inc" in page.text
    assert "Sample Vendor" in page.text and "SVI" in page.text
