"""tests/conftest.py — session-wide pytest configuration.

Sets ACCOUNTING_DB to a writable temporary path before any test module is
imported.  This prevents the module-level init_db() call in app.main from
trying to create /data (which requires root and only exists inside Docker /
on Fly.io).  Individual test fixtures that need an isolated DB override
ACCOUNTING_DB themselves via importlib.reload(app.db).
"""
import os
import tempfile

if "ACCOUNTING_DB" not in os.environ:
    _tmp_dir = tempfile.mkdtemp(prefix="bookpoint_test_")
    os.environ["ACCOUNTING_DB"] = os.path.join(_tmp_dir, "test_default.db")
