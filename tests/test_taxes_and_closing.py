"""Tests for BIR tax monitoring + closing-of-books features."""
import sys
import importlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


_LEDGER = Path("/dev/null/FOR RECONCILIATION MARCH GGR.xls")


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCOUNTING_DB", str(tmp_path / "test.db"))
    from app import db as db_module
    importlib.reload(db_module)
    db_module.init_db()
    yield db_module


# ---- Tax catalog -----------------------------------------------------------

class TestTaxCatalog:
    def test_form_codes(self):
        from app.services.tax_summary import BIR_FORMS
        codes = {f.code for f in BIR_FORMS}
        for c in ("1601-C","0619-E","1601-EQ","0619-F","1601-FQ","2550Q","1702Q","1702","2553Q"):
            assert c in codes, f"Missing form {c}"

    def test_due_dates(self):
        from app.services.tax_summary import get_form, due_date
        # 1601-C for January 2026 due Feb 10, 2026
        assert due_date(get_form("1601-C"), 2026, 1) == date(2026, 2, 10)
        # 2550Q for Q1 2026 due Apr 25, 2026 (period_end Mar 31 + 1 month, day 25)
        assert due_date(get_form("2550Q"), 2026, 1) == date(2026, 4, 25)
        # 1702 for 2025 due Apr 15, 2026
        assert due_date(get_form("1702"), 2025, 1) == date(2026, 4, 15)


# ---- Tax summary against seeded JEs ---------------------------------------

class TestTaxSummary:
    def test_runs_with_empty_db(self, fresh_db):
        from app.services.tax_summary import summarize_form, get_form
        with fresh_db.db() as conn:
            s = summarize_form(conn, get_form("1601-C"), 2026, 4)
        assert s.form.code == "1601-C"
        assert len(s.accounts) == 1
        assert s.accounts[0].account_code == "2130"
        # No JEs => zero balance
        assert s.accounts[0].closing_balance == Decimal("0")
        assert s.estimated_filing_amount == Decimal("0")

    def test_2550q_vat_payable_calculation(self, fresh_db):
        from app.services.tax_summary import summarize_form, get_form
        # Post a fake VAT scenario: Output VAT 5000 (Cr 2120), Input VAT 2000 (Dr 1210)
        with fresh_db.db() as conn:
            in_vat_id = conn.execute("SELECT id FROM chart_of_accounts WHERE code='1210'").fetchone()["id"]
            out_vat_id = conn.execute("SELECT id FROM chart_of_accounts WHERE code='2120'").fetchone()["id"]
            cash_id = conn.execute("SELECT id FROM chart_of_accounts WHERE code='1010'").fetchone()["id"]
            cur = conn.execute(
                "INSERT INTO journal_entries (entry_date, reference, description, status) "
                "VALUES ('2026-04-15', 'TEST', 'output vat', 'approved')")
            je1 = cur.lastrowid
            conn.execute("INSERT INTO journal_entry_lines (journal_entry_id, account_id, debit, credit, line_order) VALUES (?, ?, '5000', '0', 1)", (je1, cash_id))
            conn.execute("INSERT INTO journal_entry_lines (journal_entry_id, account_id, debit, credit, line_order) VALUES (?, ?, '0', '5000', 2)", (je1, out_vat_id))
            cur = conn.execute(
                "INSERT INTO journal_entries (entry_date, reference, description, status) "
                "VALUES ('2026-04-15', 'TEST', 'input vat', 'approved')")
            je2 = cur.lastrowid
            conn.execute("INSERT INTO journal_entry_lines (journal_entry_id, account_id, debit, credit, line_order) VALUES (?, ?, '2000', '0', 1)", (je2, in_vat_id))
            conn.execute("INSERT INTO journal_entry_lines (journal_entry_id, account_id, debit, credit, line_order) VALUES (?, ?, '0', '2000', 2)", (je2, cash_id))
            s = summarize_form(conn, get_form("2550Q"), 2026, 2)  # Q2 2026
        # VAT Payable = Output (5000 Cr) - Input (2000 Dr) = 3000
        assert s.estimated_filing_amount == Decimal("3000")


# ---- Subsidiary ledger parser + closing engine ----------------------------

@pytest.mark.skipif(not _LEDGER.exists(), reason="March ledger file not present")
class TestSubsidiaryLedger:
    def test_parses_real_march_ledger(self):
        from app.parsers.subsidiary_ledger import parse_subsidiary_ledger
        led = parse_subsidiary_ledger(_LEDGER)
        assert len(led.accounts) >= 1
        # Account 4111 should be there
        a = next((x for x in led.accounts if x.code == "4111"), None)
        assert a is not None
        assert a.opening_balance > 0
        assert a.closing_balance > 0
        assert a.period_debit > 0
        assert a.period_credit > 0


@pytest.mark.skipif(not _LEDGER.exists(), reason="March ledger file not present")
class TestClosingEngine:
    def test_evaluate_flags_above_threshold(self):
        from app.parsers.subsidiary_ledger import parse_subsidiary_ledger
        from app.services.closing_books import evaluate_closing
        led = parse_subsidiary_ledger(_LEDGER)
        # Threshold 100k - Acct 4111 should be flagged (changed by ~4.6M)
        flagged = evaluate_closing(led, Decimal("100000"))
        assert any(f.flagged and f.account_code == "4111" for f in flagged)

    def test_below_threshold_not_flagged(self):
        from app.parsers.subsidiary_ledger import parse_subsidiary_ledger
        from app.services.closing_books import evaluate_closing
        led = parse_subsidiary_ledger(_LEDGER)
        # With a sky-high threshold, nothing flags
        flagged = evaluate_closing(led, Decimal("1000000000"))
        assert all(not f.flagged for f in flagged)

    def test_save_run_persists(self, fresh_db):
        from app.services.closing_books import save_closing_run
        run_id = save_closing_run(_LEDGER, period_label="March 2026 close",
                                  threshold=Decimal("100000"))
        assert run_id > 0
        with fresh_db.db() as conn:
            r = conn.execute("SELECT * FROM closing_runs WHERE id = ?", (run_id,)).fetchone()
            assert r["period_label"] == "March 2026 close"
            assert r["status"] == "in_progress"
            changes = conn.execute(
                "SELECT * FROM closing_account_changes WHERE run_id = ?", (run_id,)
            ).fetchall()
            assert len(changes) >= 1
            assert any(c["flagged"] == 1 for c in changes)

    def test_explanation_workflow(self, fresh_db):
        from app.services.closing_books import save_closing_run, update_explanation, finalize_run
        run_id = save_closing_run(_LEDGER, period_label="March", threshold=Decimal("100000"))
        update_explanation(run_id, "4111", "Increased revenue from added outlets")
        with fresh_db.db() as conn:
            row = conn.execute(
                "SELECT explanation, reviewed_at FROM closing_account_changes "
                "WHERE run_id = ? AND account_code = '4111'", (run_id,)
            ).fetchone()
            assert "outlets" in row["explanation"]
            assert row["reviewed_at"] is not None
        finalize_run(run_id)
        with fresh_db.db() as conn:
            r = conn.execute("SELECT status FROM closing_runs WHERE id = ?", (run_id,)).fetchone()
            assert r["status"] == "completed"


@pytest.mark.skipif(not _LEDGER.exists(), reason="March ledger file not present")
class TestClosingPhase4:
    def test_new_columns_persist(self, fresh_db):
        from app.services.closing_books import (
            save_closing_run, update_basis, update_reviewer_notes,
        )
        run_id = save_closing_run(_LEDGER, period_label="March", threshold=Decimal("100000"))
        update_basis(run_id, "4111", "SL voucher TR1-20260301010-0020 (Mar Wk1 GGR)")
        update_reviewer_notes(run_id, "4111", "Reviewed by senior accountant")
        with fresh_db.db() as conn:
            row = conn.execute(
                "SELECT basis, reviewer_notes FROM closing_account_changes "
                "WHERE run_id = ? AND account_code = '4111'", (run_id,)
            ).fetchone()
            assert "TR1-20260301010-0020" in row["basis"]
            assert "senior accountant" in row["reviewer_notes"]


class Test1702QDueDates:
    """1702Q is due 60 calendar days after each quarter-end, adjusted for weekends.
    Q4 is not filed with 1702Q (the annual 1702RT covers the full year).
    """
    def test_q1_2026(self):
        # March 31, 2026 + 60 days = May 30, 2026 (Saturday) -> Monday June 1, 2026
        from app.services.tax_summary import due_date, get_form
        assert due_date(get_form("1702Q"), 2026, 1) == date(2026, 6, 1)

    def test_q2_2026(self):
        # June 30, 2026 + 60 days = August 29, 2026 (Saturday) -> Monday August 31, 2026
        from app.services.tax_summary import due_date, get_form
        assert due_date(get_form("1702Q"), 2026, 2) == date(2026, 8, 31)

    def test_q3_2026(self):
        # September 30, 2026 + 60 days = November 29, 2026 (Sunday) -> Monday November 30, 2026
        from app.services.tax_summary import due_date, get_form
        assert due_date(get_form("1702Q"), 2026, 3) == date(2026, 11, 30)

    def test_q4_not_valid(self):
        # 1702Q must not have a Q4 - annual 1702RT covers the full year
        from app.services.tax_summary import valid_quarters, get_form
        quarters = valid_quarters(get_form("1702Q"))
        assert 4 not in quarters, "1702Q must not have a Q4 due date"

    def test_no_weekend_adjustment_needed(self):
        # Q1 2025: March 31 + 60 = May 30, 2025 (Friday) -> no adjustment needed
        from app.services.tax_summary import due_date, get_form
        import datetime
        raw = datetime.date(2025, 3, 31) + datetime.timedelta(days=60)
        assert raw.weekday() == 4, "May 30 2025 should be Friday (weekday 4)"
        assert due_date(get_form("1702Q"), 2025, 1) == date(2025, 5, 30)


class TestNewSeedAccounts:
    def test_advances_to_suppliers_and_accrued_present(self, fresh_db):
        with fresh_db.db() as conn:
            for code, expected_name in (("1203", "Advances to Suppliers"),
                                        ("2142", "Accrued Expenses")):
                row = conn.execute("SELECT name, type FROM chart_of_accounts WHERE code=?",
                                   (code,)).fetchone()
                assert row is not None, f"Account {code} not seeded"
                assert expected_name in row["name"]
