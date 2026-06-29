from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook

from app.db import db, init_db
from app.services.invoice_matching import ingest_invoice_upload, match_invoices_and_payments


def _write_invoice(path: Path, supplier="ABC Supplies", amount="1000.00"):
    wb = Workbook()
    ws = wb.active
    ws.append(["Invoice No", "Invoice Date", "Supplier", "Description", "Gross Amount", "VAT", "Net Amount"])
    ws.append(["INV-1", "2025-05-01", supplier, "Office supplies", amount, "0", amount])
    wb.save(path)


def test_invoice_without_payment_becomes_accrued_expense(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("ACCOUNTING_DB", str(db_path))
    import app.db as dbmod
    dbmod.DB_PATH = db_path
    init_db()
    invoice_file = tmp_path / "invoices.xlsx"
    _write_invoice(invoice_file)
    result = ingest_invoice_upload(invoice_file)
    assert result["parsed_count"] == 1
    match = match_invoices_and_payments()
    assert match["accrued_expense"] == 1
    with db() as conn:
        inv = conn.execute("SELECT * FROM invoices").fetchone()
        assert inv["status"] == "accrued_expense"
        lines = conn.execute("""SELECT a.code, l.debit, l.credit FROM journal_entry_lines l
                                JOIN chart_of_accounts a ON a.id = l.account_id
                                WHERE l.journal_entry_id = ? ORDER BY l.line_order""", (inv["matched_journal_entry_id"],)).fetchall()
        assert lines[-1]["code"] == "2142"
        assert Decimal(lines[-1]["credit"]) == Decimal("1000.00")


def test_invoice_matches_existing_bank_payment(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("ACCOUNTING_DB", str(db_path))
    import app.db as dbmod
    dbmod.DB_PATH = db_path
    init_db()
    invoice_file = tmp_path / "invoices.xlsx"
    _write_invoice(invoice_file, supplier="ABC Supplies", amount="1500.00")
    ingest_invoice_upload(invoice_file)
    with db() as conn:
        cur = conn.execute("INSERT INTO uploaded_files (filename, file_type, file_size, sha256) VALUES ('bank.xlsx','bank',1,'bankhash')")
        upload_id = cur.lastrowid
        conn.execute("""INSERT INTO bank_transactions
                       (uploaded_file_id, transaction_id, transaction_date, description, debit_amount, credit_amount, net_amount, counterparty_name)
                       VALUES (?, 'T1', '2025-05-03', 'Payment ABC Supplies', '1500.00', '0', '-1500.00', 'ABC Supplies')""", (upload_id,))
    result = match_invoices_and_payments()
    assert result["matched_payment"] == 1
    with db() as conn:
        inv = conn.execute("SELECT * FROM invoices").fetchone()
        assert inv["status"] == "matched_payment"
        assert inv["matched_bank_transaction_id"] is not None
