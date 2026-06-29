"""Tests for payment-driven JE generation against the real RA260 file."""
import sys
from decimal import Decimal
from pathlib import Path
import importlib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FILE = Path("/dev/null/RA260_20260430135455895.xlsx")


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNTING_DB", str(tmp_path / "test.db"))
    from app import db as db_module
    importlib.reload(db_module)
    db_module.init_db()
    from app.services import ra260_ingest, payment_je_generator
    importlib.reload(ra260_ingest)
    importlib.reload(payment_je_generator)
    yield db_module, ra260_ingest, payment_je_generator


@pytest.mark.skipif(not _FILE.exists(), reason="RA260 file not present")
class TestPaymentJEGeneration:
    def test_generates_only_for_eligible_status(self, fresh_db):
        db_module, ra260_ingest, payment_je_generator = fresh_db
        ra260_ingest.ingest_ra260(_FILE)
        result = payment_je_generator.generate_jes_for_payments()
        # 84 successful + 4 released = 88 eligible
        assert result.je_generated == 88
        # 1 scheduled + 4 cancelled + 2 failed + 1 rejected = 8 ineligible
        assert result.skipped_ineligible_status == 8

    def test_jes_are_balanced(self, fresh_db):
        db_module, ra260_ingest, payment_je_generator = fresh_db
        ra260_ingest.ingest_ra260(_FILE)
        payment_je_generator.generate_jes_for_payments()
        with db_module.db() as conn:
            unbalanced = conn.execute(
                "SELECT je.id, "
                "SUM(CAST(l.debit AS REAL)) AS dr, "
                "SUM(CAST(l.credit AS REAL)) AS cr "
                "FROM journal_entries je "
                "JOIN journal_entry_lines l ON l.journal_entry_id = je.id "
                "WHERE je.classification_method = 'payment_register' "
                "GROUP BY je.id "
                "HAVING ABS(dr - cr) > 0.005"
            ).fetchall()
            assert len(unbalanced) == 0

    def test_running_twice_skips_already_generated(self, fresh_db):
        db_module, ra260_ingest, payment_je_generator = fresh_db
        ra260_ingest.ingest_ra260(_FILE)
        first = payment_je_generator.generate_jes_for_payments()
        second = payment_je_generator.generate_jes_for_payments()
        assert first.je_generated == 88
        assert second.je_generated == 0
        assert second.skipped_already_has_je == 88

    def test_payment_links_to_je(self, fresh_db):
        db_module, ra260_ingest, payment_je_generator = fresh_db
        ra260_ingest.ingest_ra260(_FILE)
        payment_je_generator.generate_jes_for_payments()
        with db_module.db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM payment_instructions "
                "WHERE journal_entry_id IS NOT NULL"
            ).fetchone()["n"]
            assert n == 88

    def test_cella_payment_classified_to_rent_expense(self, fresh_db):
        db_module, ra260_ingest, payment_je_generator = fresh_db
        ra260_ingest.ingest_ra260(_FILE)
        payment_je_generator.generate_jes_for_payments()
        with db_module.db() as conn:
            cella_payments = conn.execute(
                "SELECT p.*, je.id AS je_id FROM payment_instructions p "
                "LEFT JOIN journal_entries je ON je.id = p.journal_entry_id "
                "WHERE p.beneficiary_name LIKE '%CELLA%' "
                "AND p.transaction_status IN ('Transaction Successful','Transaction Released')"
            ).fetchall()
            assert len(cella_payments) >= 1
            for cp in cella_payments:
                assert cp["je_id"] is not None
                # JE should debit Rent Expense (5010)
                lines = conn.execute(
                    "SELECT l.*, a.code FROM journal_entry_lines l "
                    "JOIN chart_of_accounts a ON a.id = l.account_id "
                    "WHERE l.journal_entry_id = ?",
                    (cp["je_id"],),
                ).fetchall()
                # 2 lines, one Dr and one Cr, with code 5010 on Dr and 1010 on Cr
                accts = {l["code"] for l in lines}
                assert "1010" in accts  # Unionbank CIB credit
                assert "5010" in accts  # Rent Expense debit

    def test_per_payment_generation(self, fresh_db):
        db_module, ra260_ingest, payment_je_generator = fresh_db
        ra260_ingest.ingest_ra260(_FILE)
        with db_module.db() as conn:
            row = conn.execute(
                "SELECT id FROM payment_instructions "
                "WHERE transaction_status = 'Transaction Successful' LIMIT 1"
            ).fetchone()
            pid = row["id"]
        result = payment_je_generator.generate_jes_for_payments(payment_ids=[pid])
        assert result.je_generated == 1


class TestEligibilityRules:
    def test_eligible_statuses(self):
        from app.services.payment_je_generator import ELIGIBLE_STATUSES
        assert "Transaction Successful" in ELIGIBLE_STATUSES
        assert "Transaction Released" in ELIGIBLE_STATUSES
        assert "Cancelled" not in ELIGIBLE_STATUSES
        assert "Scheduled" not in ELIGIBLE_STATUSES
        assert "Transaction Failed" not in ELIGIBLE_STATUSES
