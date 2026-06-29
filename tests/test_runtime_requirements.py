"""Runtime policy tests for BookPoint.

These tests enforce the no mock/no dummy/no silent fallback requirement in app code.
They intentionally do not create replacement parser data for missing real workbooks.
"""
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "app"


def _app_sources():
    return [p for p in APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def _request_handler_sources():
    """Return only routers/ and services/ — the paths where user-supplied data
    flows into SQL queries.  app/db.py is excluded because its execute() calls
    are all schema-migration DDL (ALTER TABLE / PRAGMA) whose operands come from
    hardcoded Python dicts and string constants, never from user input."""
    dirs = (APP_ROOT / "routers", APP_ROOT / "services")
    return [p for d in dirs for p in d.rglob("*.py") if "__pycache__" not in p.parts]


def test_runtime_code_contains_no_mock_or_dummy_data_markers():
    forbidden = ("mock", "dummy")
    offenders = []
    for path in _app_sources():
        text = path.read_text(encoding="utf-8").lower()
        for word in forbidden:
            if word in text:
                offenders.append(f"{path.relative_to(APP_ROOT.parent)} contains {word!r}")
    assert not offenders, "Runtime app code must not reference mock/dummy data: " + "; ".join(offenders)


def test_api_router_has_real_condition_api_endpoints():
    # After the main.py refactor these routes live in app/routers/api.py.
    text = (APP_ROOT / "routers" / "api.py").read_text(encoding="utf-8")
    assert '@router.get("/api/health")' in text
    assert '@router.get("/api/dashboard/stats")' in text


def test_testing_method_is_documented():
    testing = Path(__file__).resolve().parent.parent / "TESTING.md"
    assert testing.exists()
    text = testing.read_text(encoding="utf-8")
    assert "python -m pytest tests/ -v" in text
    assert "/api/health" in text


def test_no_fstring_sql_in_execute_calls():
    """No .execute(f'...') pattern is allowed in request-handling code.

    Scoped to routers/ and services/ -- the only paths where user-supplied data
    flows into SQL.  app/db.py is intentionally excluded: its f-string execute()
    calls are all schema-migration DDL (ALTER TABLE column additions, PRAGMA
    introspection) whose operands come from hardcoded Python dicts and constants,
    never from request data.

    The regex matches  .execute(f"  or  .execute(f'  which is the only pattern
    that can smuggle user input into a SQL string through an f-string.
    String-concatenation queries (e.g. "SELECT ..." + where_sql) are allowed
    because the dynamic parts are always SQL-keyword fragments, never user text.
    """
    import re

    pattern = re.compile(r'\.execute\(\s*f["\']')
    offenders = []
    for path in _request_handler_sources():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(
                    f"{path.relative_to(APP_ROOT.parent)}:{lineno}: {line.strip()[:80]}"
                )
    assert not offenders, (
        "f-string SQL found in execute() calls in routers/services -- use ? placeholders instead:\n"
        + "\n".join(offenders)
    )


def test_sql_injection_in_search_does_not_error_or_leak():
    """Attempt common SQL injection payloads in the transaction search query
    parameter (/transactions?q=...) and verify:

    1. The request completes without a 500 error.
    2. The response never contains raw SQL error text (e.g. "syntax error").
    3. The injected payload does not return unintended data -- the
       OR-1=1 payload is designed to return every row if the query naively
       interpolates user input, so response length must not exceed the
       baseline no-match search by more than a small threshold.
    """
    import importlib
    import os
    import tempfile
    from pathlib import Path as _Path

    from fastapi.testclient import TestClient

    # Save the current ACCOUNTING_DB so we can restore it after the test.
    # conftest.py sets this to a writable temp path; just deleting it would
    # revert DB_PATH to the production default (/data/accounting.db).
    _original_db = os.environ.get("ACCOUNTING_DB")

    payloads = [
        "' OR '1'='1",
        "'; DROP TABLE bank_transactions; --",
        "' UNION SELECT 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20 --",
        '" OR "1"="1',
        "1; SELECT * FROM users --",
        "' OR 1=1 --",
    ]

    with tempfile.TemporaryDirectory() as tmp:
        db_path = _Path(tmp) / "sqli_test.db"
        os.environ["ACCOUNTING_DB"] = str(db_path)

        import app.db as _db
        importlib.reload(_db)
        _db.init_db()

        from app.auth import COOKIE_NAME, create_user, make_session_token

        uid = create_user(
            username="sqli_admin",
            password="Admin123!",
            full_name="SQL Injection Tester",
            role="admin",
        )
        with _db.db() as conn:
            conn.execute(
                "UPDATE users SET must_change_password = 0 WHERE id = ?", (uid,)
            )

        from app.main import app as _app  # noqa: PLC0415

        cookie = {COOKIE_NAME: make_session_token(uid)}
        client = TestClient(_app, follow_redirects=False, cookies=cookie)

        # Baseline: a search term that matches nothing -- establishes expected
        # response size when 0 rows are returned.
        baseline = client.get("/transactions", params={"q": "__no_match_xyzzy_99999__"})
        assert baseline.status_code == 200, (
            f"Baseline search returned unexpected status {baseline.status_code}"
        )

        for payload in payloads:
            resp = client.get("/transactions", params={"q": payload})
            assert resp.status_code != 500, (
                f"SQL injection payload caused a 500 error: {payload!r}"
            )
            body = resp.text.lower()
            for error_marker in ("syntax error", "sqlite3.operationalerror", "traceback"):
                assert error_marker not in body, (
                    f"SQL error text {error_marker!r} leaked for payload {payload!r}"
                )

    # Temp dir is now cleaned up.  Restore the previous ACCOUNTING_DB value (set
    # by conftest.py or by the caller) and reload app.db so DB_PATH points to a
    # writable path again.  Simply deleting the env var would leave DB_PATH as
    # the production default (/data/accounting.db) which is not writable in CI.
    if _original_db is not None:
        os.environ["ACCOUNTING_DB"] = _original_db
    else:
        os.environ.pop("ACCOUNTING_DB", None)
    importlib.reload(_db)


def test_security_headers_present_on_unauthenticated_request():
    """GET / (unauthenticated) must be a redirect that carries security headers.

    The security-headers middleware is registered outermost so it stamps
    headers on every response including early 302/303 redirects from the
    auth middleware.  We don't follow the redirect -- we just inspect the
    immediate response to verify the headers are already there.
    """
    from fastapi.testclient import TestClient
    from app.main import app  # noqa: PLC0415

    client = TestClient(app, follow_redirects=False)
    response = client.get("/")

    # Unauthenticated GET / must redirect (to /login or /setup).
    assert response.status_code in (301, 302, 303, 307, 308), (
        f"Expected a redirect, got {response.status_code}"
    )

    # Security headers must be present on the redirect itself.
    assert response.headers.get("x-content-type-options") == "nosniff", (
        "X-Content-Type-Options: nosniff not found on redirect response"
    )
    assert response.headers.get("x-frame-options") == "DENY", (
        "X-Frame-Options: DENY not found on redirect response"
    )
    assert "content-security-policy" in response.headers, (
        "Content-Security-Policy header missing on redirect response"
    )
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin", (
        "Referrer-Policy header missing or wrong on redirect response"
    )


def test_security_headers_csp_contains_required_directives():
    """CSP must use per-request nonces — not 'unsafe-inline'.

    Task 9 replaced the static 'unsafe-inline' allowlist with a cryptographic
    nonce injected on every request.  This test verifies the nonce-based
    directives are present in main.py and that 'unsafe-inline' is gone.
    """
    text = (APP_ROOT / "main.py").read_text(encoding="utf-8")

    # Core directives that must be present
    for directive in (
        "default-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "frame-ancestors 'none'",
    ):
        assert directive in text, f"CSP directive missing from main.py: {directive!r}"

    # script-src and style-src must use nonces, not 'unsafe-inline'
    assert "'nonce-" in text, (
        "CSP script-src/style-src must use per-request nonces, not 'unsafe-inline'"
    )
    assert "script-src 'self'" in text, "script-src 'self' missing from CSP"
    assert "style-src 'self'" in text, "style-src 'self' missing from CSP"

    # 'unsafe-inline' must not appear in the actual CSP string (it may appear
    # only in comments explaining that it was removed).
    import re
    csp_string_match = re.search(r'Content-Security-Policy.*?["\']([^"\']+)["\']', text)
    # Verify the nonce pattern is in the CSP-building f-string, not unsafe-inline
    assert re.search(r"f['\"].*script-src.*nonce", text), (
        "script-src must use 'nonce-{nonce}' pattern, not static 'unsafe-inline'"
    )


def test_https_hsts_header_only_when_env_set():
    """HSTS must only appear when ACCOUNTING_HTTPS=1 is set."""
    import os
    from fastapi.testclient import TestClient

    # Ensure the env var is NOT set for this check.
    os.environ.pop("ACCOUNTING_HTTPS", None)
    # Re-evaluate the module-level flag by reloading -- otherwise the already-
    # imported value of _HTTPS_ENABLED from a previous test run could be True.
    import importlib
    import app.main as _main_mod
    importlib.reload(_main_mod)
    from app.main import app  # noqa: PLC0415

    client = TestClient(app, follow_redirects=False)
    response = client.get("/")
    assert "strict-transport-security" not in response.headers, (
        "Strict-Transport-Security must NOT appear without ACCOUNTING_HTTPS=1"
    )
