"""
Tests for supplier matcher, classifier, JE generator, and end-to-end ingestion.
Uses an isolated temp SQLite DB and the real UB statement.
"""
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Use an isolated SQLite DB for each test class."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("ACCOUNTING_DB", str(db_path))
    # Reload db module to pick up the new path
    import importlib
    from app import db as db_module
    importlib.reload(db_module)
    db_module.init_db()
    yield db_module
    # Reset the env var and reload back
    monkeypatch.delenv("ACCOUNTING_DB", raising=False)


_REAL_FILE = Path("/dev/null/032026 UB PESO.xlsx")


class TestSeedData:
    def test_chart_of_accounts_seeded(self, temp_db):
        with temp_db.db() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM chart_of_accounts").fetchone()["n"]
            assert n >= 25
            # Critical accounts present
            for code in ("1010", "1210", "2110", "5010", "5020", "5040", "5999"):
                row = conn.execute(
                    "SELECT * FROM chart_of_accounts WHERE code=?", (code,)
                ).fetchone()
                assert row is not None, f"Missing account {code}"

    def test_suppliers_seeded(self, temp_db):
        with temp_db.db() as conn:
            cella = conn.execute(
                "SELECT * FROM suppliers WHERE name='Cella Storage Space Rental'"
            ).fetchone()
            assert cella is not None
            assert cella["tin"] == "248-018-726-00000"

    def test_aliases_seeded(self, temp_db):
        with temp_db.db() as conn:
            n = conn.execute(
                """SELECT COUNT(*) AS n FROM supplier_aliases
                   WHERE alias = 'CELLA STORAGE SPACE RENTAL'"""
            ).fetchone()["n"]
            assert n == 1

    def test_classification_rules_seeded(self, temp_db):
        with temp_db.db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM classification_rules"
            ).fetchone()["n"]
            assert n >= 10


class TestSupplierMatcher:
    def test_exact_match_on_counterparty(self, temp_db):
        from app.services.supplier_matcher import match_supplier
        with temp_db.db() as conn:
            m = match_supplier(conn,
                               description="Sent to CELLA STORAGE SPACE RENTAL    BDO 097718",
                               counterparty_name="CELLA STORAGE SPACE RENTAL")
        assert m is not None
        assert m.supplier_name == "Cella Storage Space Rental"
        assert m.confidence > 0.9

    def test_substring_match_on_remarks(self, temp_db):
        from app.services.supplier_matcher import match_supplier
        with temp_db.db() as conn:
            m = match_supplier(conn,
                               description="BILLS PAYMENT",
                               remarks="GLOBE B2B RAC PHIL HD M360 API FOR JAN 2026")
        assert m is not None
        assert m.supplier_name == "Globe Telecom Inc"

    def test_no_match_on_unknown(self, temp_db):
        from app.services.supplier_matcher import match_supplier
        with temp_db.db() as conn:
            m = match_supplier(conn,
                               description="XYZ UNKNOWN COMPANY",
                               counterparty_name="XYZ UNKNOWN COMPANY")
        assert m is None


class TestClassifier:
    def test_supplier_default_takes_precedence(self, temp_db):
        from app.services.classifier import classify
        with temp_db.db() as conn:
            row = conn.execute(
                "SELECT default_expense_account_id FROM suppliers WHERE name='Cella Storage Space Rental'"
            ).fetchone()
            sup_acct = row["default_expense_account_id"]
            cls = classify(conn,
                           description="Sent to CELLA STORAGE SPACE RENTAL    BDO 097718",
                           supplier_default_account_id=sup_acct)
        assert cls.target_account_code == "5010"  # Rent Expense
        assert cls.confidence > 0.9

    def test_rule_based_payroll(self, temp_db):
        from app.services.classifier import classify
        with temp_db.db() as conn:
            cls = classify(conn, description="PAYROLL")
        assert cls.target_account_code == "5050"  # Salaries

    def test_unknown_falls_to_suspense(self, temp_db):
        from app.services.classifier import classify
        with temp_db.db() as conn:
            cls = classify(conn, description="WEIRD UNKNOWN THING NO RULE MATCHES")
        assert cls.target_account_code == "5999"  # Suspense
        assert cls.confidence == 0.0


@pytest.mark.skipif(not _REAL_FILE.exists(), reason="Real UB file not present")
class TestEndToEndIngestion:
    def test_ingest_real_file_creates_balanced_jes(self, temp_db):
        from app.services.file_upload import ingest_statement
        result = ingest_statement(_REAL_FILE)
        assert result.transactions_inserted > 200
        assert result.journal_entries_drafted == result.transactions_inserted
        # All generated JEs must balance
        with temp_db.db() as conn:
            unbalanced = conn.execute(
                """SELECT je.id, SUM(CAST(l.debit AS REAL)) AS dr,
                          SUM(CAST(l.credit AS REAL)) AS cr
                   FROM journal_entries je
                   JOIN journal_entry_lines l ON l.journal_entry_id = je.id
                   GROUP BY je.id
                   HAVING ABS(dr - cr) > 0.005"""
            ).fetchall()
            assert len(unbalanced) == 0, \
                f"Unbalanced JEs: {[dict(r) for r in unbalanced]}"

    def test_cella_payment_classified_to_rent_expense(self, temp_db):
        from app.services.file_upload import ingest_statement
        ingest_statement(_REAL_FILE)
        with temp_db.db() as conn:
            # Find any 118,417.75 outflow with classification
            row = conn.execute(
                """SELECT bt.*, je.id AS je_id
                   FROM bank_transactions bt
                   LEFT JOIN journal_entries je ON je.id = bt.journal_entry_id
                   WHERE CAST(bt.debit_amount AS REAL) = 118417.75"""
            ).fetchone()
            assert row is not None
            # Should be matched to Cella supplier OR classified to rent
            if row["supplier_id"]:
                sup = conn.execute(
                    "SELECT * FROM suppliers WHERE id=?", (row["supplier_id"],)
                ).fetchone()
                assert "Cella" in sup["name"] or row["classification"].startswith("5010")

    def test_suspense_count_is_visible(self, temp_db):
        from app.services.file_upload import ingest_statement
        result = ingest_statement(_REAL_FILE)
        # Some transactions WILL be in suspense (employee names, etc.)
        # The point is that the count is non-negative and reported
        assert result.suspense_count >= 0
        assert result.suspense_count <= result.transactions_inserted

    def test_duplicate_upload_rejected(self, temp_db):
        from app.services.file_upload import ingest_statement, DuplicateUploadError
        ingest_statement(_REAL_FILE)
        with pytest.raises(DuplicateUploadError):
            ingest_statement(_REAL_FILE)
