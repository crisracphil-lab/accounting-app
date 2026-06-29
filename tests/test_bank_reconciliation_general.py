import csv
from pathlib import Path

from app.db import db, init_db
from app.services.bank_reconciliation import reconcile_files


def _csv(path: Path, rows):
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)


def test_general_bank_reconciliation_matches_different_column_names(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("ACCOUNTING_DB", str(db_path))
    import app.db as dbmod
    dbmod.DB_PATH = db_path
    init_db()
    bank = tmp_path / "bank.csv"
    system = tmp_path / "system.csv"
    _csv(bank, [["Transaction Date", "Particulars", "Ref No", "Withdrawal", "Deposit"], ["2025-05-01", "ABC payment", "R1", "100.00", ""]])
    _csv(system, [["Date", "Description", "Reference", "Amount"], ["2025-05-01", "ABC payment", "R1", "-100.00"]])
    run_id = reconcile_files(bank, system)
    with db() as conn:
        item = conn.execute("SELECT * FROM bank_reconciliation_items WHERE run_id=?", (run_id,)).fetchone()
        assert item["status"] == "matched"


def test_general_bank_reconciliation_reports_system_only(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("ACCOUNTING_DB", str(db_path))
    import app.db as dbmod
    dbmod.DB_PATH = db_path
    init_db()
    bank = tmp_path / "bank.csv"
    system = tmp_path / "system.csv"
    _csv(bank, [["Date", "Description", "Reference", "Amount"]])
    _csv(system, [["Date", "Description", "Reference", "Amount"], ["2025-05-01", "System only", "S1", "250.00"]])
    # bank file with no transactions should throw instead of returning empty success
    try:
        reconcile_files(bank, system)
    except Exception as exc:
        assert "No transaction rows" in str(exc)
    else:
        raise AssertionError("empty bank file must throw")

from openpyxl import Workbook


def _xlsx(path: Path, rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_system_vs_raw_excel_reconciliation_uses_flexible_headers(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("ACCOUNTING_DB", str(db_path))
    import app.db as dbmod
    dbmod.DB_PATH = db_path
    init_db()
    system = tmp_path / "per_system.xlsx"
    raw = tmp_path / "raw_supplier.xlsx"
    _xlsx(system, [["JE Date", "Journal Entry No", "Account Title", "System Amount"], ["2025-06-15", "JE-101", "Office Supplies", "1250.00"]])
    _xlsx(raw, [["Document Date", "Voucher No", "Vendor Name", "Payment Amount"], ["2025-06-15", "JE-101", "Office Supplies", "1250.00"]])
    run_id = reconcile_files(system, raw, reconciliation_type="system_vs_raw")
    with db() as conn:
        run = conn.execute("SELECT * FROM bank_reconciliation_runs WHERE id=?", (run_id,)).fetchone()
        item = conn.execute("SELECT * FROM bank_reconciliation_items WHERE run_id=?", (run_id,)).fetchone()
        assert run["reconciliation_type"] == "system_vs_raw"
        assert item["status"] == "matched"

from app.parsers.bank_statement_generic import parse_generic_statement


def test_parse_multiple_bank_statement_formats(tmp_path):
    samples = {
        "bdo.csv": [
            ["Transaction Date", "Particulars", "Ref No", "Withdrawal", "Deposit", "Running Balance"],
            ["2026-02-01", "Payment to Manila Broadcasting", "BDO-001", "12000.00", "", "88000.00"],
        ],
        "bpi.csv": [
            ["Posting Date", "Transaction Details", "Reference Number", "Debit Amount", "Credit Amount", "Balance"],
            ["02/02/2026", "Supplier payment - office rent", "BPI-001", "25000.00", "", "63000.00"],
        ],
        "metrobank.csv": [
            ["Date", "Transaction Description", "Reference", "Amount", "DR/CR", "Balance"],
            ["03-Feb-2026", "Deposit from customer", "MB-001", "15000.00", "CR", "78000.00"],
        ],
        "security_bank.csv": [
            ["Value Date", "Description", "Transaction Reference", "Debit", "Credit", "Available Balance"],
            ["04/02/2026", "Bank charge", "SB-001", "300.00", "", "77700.00"],
        ],
        "unionbank.csv": [
            ["Posted Date", "Remarks", "Transaction ID", "Amount", "Type", "Running Balance"],
            ["2026/02/05", "Payment to supplier", "UB-001", "5400.00", "Debit", "72300.00"],
        ],
        "landbank.csv": [
            ["Transaction Date", "Particulars", "Check No", "Debit", "Credit", "Balance"],
            ["Feb 06, 2026", "Check payment", "LBP-001", "900.00", "", "71400.00"],
        ],
        "rcbc.csv": [
            ["Txn Date", "Details", "Ref No", "Withdrawals", "Deposits", "Running Balance"],
            ["06 Feb 2026", "Client deposit", "RCBC-001", "", "2000.00", "73400.00"],
        ],
    }
    expected_amounts = {
        "bdo.csv": "-12000.00",
        "bpi.csv": "-25000.00",
        "metrobank.csv": "15000.00",
        "security_bank.csv": "-300.00",
        "unionbank.csv": "-5400.00",
        "landbank.csv": "-900.00",
        "rcbc.csv": "2000.00",
    }
    for name, rows in samples.items():
        path = tmp_path / name
        _csv(path, rows)
        parsed = parse_generic_statement(path)
        assert len(parsed) == 1
        assert str(parsed[0].amount) == expected_amounts[name]
        assert parsed[0].reference is not None


def test_parse_bank_statement_with_header_after_bank_metadata(tmp_path):
    path = tmp_path / "metadata_then_transactions.csv"
    _csv(path, [
        ["Account Name", "Operating Account"],
        ["Account Number", "123-456"],
        ["Statement Period", "February 2026"],
        [],
        ["Value Date", "Narrative", "Serial No", "Debit", "Credit", "Running Balance"],
        ["2026-02-10", "Payment to Manila Broadcasting", "S-100", "12000.00", "", "50000.00"],
    ])
    parsed = parse_generic_statement(path)
    assert len(parsed) == 1
    assert parsed[0].amount < 0
    assert parsed[0].description == "Payment to Manila Broadcasting"
