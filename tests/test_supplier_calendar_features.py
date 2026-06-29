import importlib
import os

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
        "username": "admin",
        "full_name": "Admin",
        "password": "Password123!",
        "confirm_password": "Password123!",
    }, follow_redirects=False)
    assert r.status_code == 303


def _get_admin_id() -> int:
    """Return the id of the seeded admin user."""
    import app.db as db_mod
    with db_mod.db() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    return row["id"]


def _csrf(user_id: int) -> str:
    """Generate a valid CSRF token for *user_id*."""
    from app.csrf import generate_csrf_token
    return generate_csrf_token(user_id)


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
    assert created.status_code == 303
    page = client.get("/suppliers")
    assert "Sample Vendor Inc" in page.text
    assert "Sample Vendor" in page.text and "SVI" in page.text


def test_calendar_shared_and_private_visibility(tmp_path):
    client = TestClient(load_app(tmp_path))
    login_admin(client)
    uid = _get_admin_id()
    r = client.post("/calendar/create", data={
        "title": "Shared filing deadline",
        "event_date": "2026-04-15",
        "start_time": "09:00",
        "end_time": "10:00",
        "description": "Quarterly filing",
        "location": "Accounting office",
        "visibility": "shared",
        "reminder_minutes": "60",
        "csrf_token": _csrf(uid),
    }, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/calendar/create", data={
        "title": "Private review",
        "event_date": "2026-04-16",
        "visibility": "private",
        "reminder_minutes": "30",
        "csrf_token": _csrf(uid),
    }, follow_redirects=False)
    assert r.status_code == 303

    events = client.get("/api/calendar/events").json()
    titles = {e["title"] for e in events}
    assert {"Shared filing deadline", "Private review"}.issubset(titles)

    shared = client.get("/api/calendar/events?visibility=shared").json()
    assert [e["title"] for e in shared] == ["Shared filing deadline"]


# ── Supplier management tests ─────────────────────────────────────────────────

def test_create_supplier_via_api(tmp_path):
    client = TestClient(load_app(tmp_path))
    login_admin(client)
    uid = _get_admin_id()

    r = client.post("/suppliers/create", data={
        "name": "Philippine Airlines",
        "tin": "001-002-003-00001",
        "default_expense_account_id": "0",
        "aliases": "",
        "is_active": "1",
        "csrf_token": _csrf(uid),
    }, follow_redirects=False)
    assert r.status_code == 303, f"Expected 303 redirect, got {r.status_code}"

    page = client.get("/suppliers")
    assert page.status_code == 200
    assert "Philippine Airlines" in page.text, "Supplier name not found on suppliers page"

    import app.db as db_mod
    with db_mod.db() as conn:
        row = conn.execute(
            "SELECT * FROM audit_logs WHERE entity_type = 'supplier' AND action = 'create'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "Expected a 'create supplier' audit log entry"
    assert "Philippine Airlines" in row["details_json"], \
        f"Expected supplier name in audit log details: {row['details_json']}"


def test_add_and_delete_alias(tmp_path):
    client = TestClient(load_app(tmp_path))
    login_admin(client)
    uid = _get_admin_id()

    # Create a fresh supplier
    client.post("/suppliers/create", data={
        "name": "Globe Telecom",
        "tin": "",
        "default_expense_account_id": "0",
        "aliases": "",
        "is_active": "1",
        "csrf_token": _csrf(uid),
    }, follow_redirects=False)

    import app.db as db_mod
    with db_mod.db() as conn:
        sup = conn.execute("SELECT id FROM suppliers WHERE name = 'Globe Telecom'").fetchone()
    assert sup is not None, "Supplier 'Globe Telecom' was not created"
    sup_id = sup["id"]

    # Add an alias via the new route (should be uppercased)
    r = client.post(f"/suppliers/{sup_id}/aliases/add",
                    data={"alias": "globe", "csrf_token": _csrf(uid)},
                    follow_redirects=False)
    assert r.status_code == 303, f"Expected 303 after alias add, got {r.status_code}"

    with db_mod.db() as conn:
        alias_row = conn.execute(
            "SELECT id, alias FROM supplier_aliases WHERE supplier_id = ? AND alias = 'GLOBE'",
            (sup_id,),
        ).fetchone()
    assert alias_row is not None, "Alias 'GLOBE' was not stored (expected uppercase)"
    alias_id = alias_row["id"]

    # Delete the alias via the new route
    r = client.post(f"/suppliers/{sup_id}/aliases/{alias_id}/delete",
                    data={"csrf_token": _csrf(uid)},
                    follow_redirects=False)
    assert r.status_code == 303, f"Expected 303 after alias delete, got {r.status_code}"

    with db_mod.db() as conn:
        gone = conn.execute(
            "SELECT id FROM supplier_aliases WHERE id = ?", (alias_id,)
        ).fetchone()
    assert gone is None, f"Alias with id={alias_id} should have been deleted"


def test_deactivate_supplier_hides_it_from_matching(tmp_path):
    client = TestClient(load_app(tmp_path))
    login_admin(client)
    uid = _get_admin_id()

    # Create supplier with an alias
    r = client.post("/suppliers/create", data={
        "name": "Meralco",
        "tin": "",
        "default_expense_account_id": "0",
        "aliases": "MERALCO",
        "is_active": "1",
        "csrf_token": _csrf(uid),
    }, follow_redirects=False)
    assert r.status_code == 303, f"Expected 303 after supplier create, got {r.status_code}"

    import app.db as db_mod
    with db_mod.db() as conn:
        sup = conn.execute("SELECT id FROM suppliers WHERE name = 'Meralco'").fetchone()
    assert sup is not None, "Supplier 'Meralco' was not created"
    sup_id = sup["id"]

    # Supplier should match before deactivation
    from app.services.supplier_matcher import match_supplier
    with db_mod.db() as conn:
        m = match_supplier(conn, description="PAYMENT TO MERALCO CORP")
    assert m is not None, "Expected a match before deactivation"
    assert m.supplier_id == sup_id, \
        f"Expected match for supplier {sup_id}, got {m.supplier_id}"

    # Deactivate via the route — must always soft-delete
    r = client.post(f"/suppliers/{sup_id}/delete",
                    data={"csrf_token": _csrf(uid)},
                    follow_redirects=False)
    assert r.status_code == 303, f"Expected 303 after deactivate, got {r.status_code}"

    # Supplier should now be inactive in DB
    with db_mod.db() as conn:
        row = conn.execute("SELECT is_active FROM suppliers WHERE id = ?", (sup_id,)).fetchone()
    assert row is not None, "Supplier row was hard-deleted (it should only be deactivated)"
    assert row["is_active"] == 0, f"Expected is_active=0 after deactivation, got {row['is_active']}"

    # Match should return None now
    with db_mod.db() as conn:
        m2 = match_supplier(conn, description="PAYMENT TO MERALCO CORP")
    assert m2 is None, "Deactivated supplier should not match any description"
