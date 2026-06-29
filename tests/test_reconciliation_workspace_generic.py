from pathlib import Path
from openpyxl import Workbook

from app.parsers.bank_statement_generic import parse_generic_statement
from app.services.bank_reconciliation import reconcile_files
from app.db import db, init_db


def _xlsx(path: Path, headers, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_generic_parser_accepts_non_bank_raw_export(tmp_path):
    source = tmp_path / "subsidiary_ledger.xlsx"
    _xlsx(source, ["Doc Date", "Voucher No", "Account Title", "System Amount"], [["2026-04-01", "JV-1", "Office Supplies", 1250]])
    rows = parse_generic_statement(source)
    assert len(rows) == 1
    assert rows[0].date == "2026-04-01"
    assert rows[0].amount == 1250
    assert rows[0].description == "Office Supplies"


def test_reconciliation_workspace_file_a_vs_file_b(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("ACCOUNTING_DB", str(db_path))
    init_db()
    left = tmp_path / "collections.xlsx"
    right = tmp_path / "deposits.xlsx"
    _xlsx(left, ["Report Date", "Reference No", "Description", "Amount"], [["2026-04-02", "OR-99", "Customer deposit", 5000]])
    _xlsx(right, ["Posting Date", "Ref No", "Particulars", "Credit Amount"], [["2026-04-02", "OR-99", "Customer deposit", 5000]])
    run_id = reconcile_files(left, right, reconciliation_type="general_file_reconciliation")
    with db() as conn:
        item = conn.execute("SELECT status FROM bank_reconciliation_items WHERE run_id=?", (run_id,)).fetchone()
    assert item["status"] == "matched"
