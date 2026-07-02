"""
SQLite database layer.

Single-file local database (accounting.db), no server required.
Schema is defined in SCHEMA below; init_db() creates all tables and
seeds the chart of accounts, classification rules, and a few known
suppliers on first run (idempotent: re-running does nothing).

All money is stored as TEXT in the form 'NNNN.NN' to preserve precision.
The Decimal type from Python is used everywhere in code.
"""
from __future__ import annotations

import sqlite3
import json
import os
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Iterator, Optional

from app.config import DB_PATH as _DB_PATH_STR

DB_PATH = Path(_DB_PATH_STR)


# -----------------------------------------------------------------------------
# Schema
# -----------------------------------------------------------------------------
SCHEMA = r"""
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS chart_of_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('asset','liability','equity','income','expense')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    tin TEXT,
    default_expense_account_id INTEGER REFERENCES chart_of_accounts(id),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS supplier_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    UNIQUE (supplier_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_supplier_aliases_alias ON supplier_aliases(alias);

CREATE TABLE IF NOT EXISTS uploaded_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    file_type TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    bank_account TEXT,
    period_covered TEXT,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    parsed_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bank_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_file_id INTEGER NOT NULL REFERENCES uploaded_files(id),
    transaction_id TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    posted_date TEXT,
    description TEXT NOT NULL,
    check_number TEXT,
    debit_amount TEXT NOT NULL DEFAULT '0',
    credit_amount TEXT NOT NULL DEFAULT '0',
    net_amount TEXT NOT NULL,
    ending_balance TEXT,
    reference_number TEXT,
    remarks TEXT,
    branch TEXT,
    biller_name TEXT,
    counterparty_name TEXT,
    supplier_id INTEGER REFERENCES suppliers(id),
    supplier_match_confidence REAL,
    supplier_match_reason TEXT,
    journal_entry_id INTEGER,
    classification TEXT,
    status TEXT NOT NULL DEFAULT 'unmatched'
        CHECK (status IN ('unmatched','matched_supplier','journal_generated','reviewed','approved')),
    UNIQUE (uploaded_file_id, transaction_id)
);
CREATE INDEX IF NOT EXISTS idx_bank_tx_status ON bank_transactions(status);
CREATE INDEX IF NOT EXISTS idx_bank_tx_supplier ON bank_transactions(supplier_id);

CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_transaction_id INTEGER REFERENCES bank_transactions(id),
    uploaded_file_id INTEGER REFERENCES uploaded_files(id),
    entry_date TEXT NOT NULL,
    reference TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','submitted','reviewed','approved','posted','rejected')),
    confidence_score REAL,
    classification_method TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_je_status ON journal_entries(status);

CREATE TABLE IF NOT EXISTS journal_entry_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journal_entry_id INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES chart_of_accounts(id),
    debit TEXT NOT NULL DEFAULT '0',
    credit TEXT NOT NULL DEFAULT '0',
    description TEXT,
    line_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS classification_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    pattern_type TEXT NOT NULL CHECK (pattern_type IN ('keyword','regex')),
    target_account_id INTEGER NOT NULL REFERENCES chart_of_accounts(id),
    direction TEXT NOT NULL CHECK (direction IN ('debit','credit','auto')) DEFAULT 'auto',
    priority INTEGER NOT NULL DEFAULT 100,
    description TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_rules_priority ON classification_rules(priority, is_active);


CREATE TABLE IF NOT EXISTS payment_instructions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_file_id INTEGER NOT NULL REFERENCES uploaded_files(id),
    tran_id TEXT NOT NULL,
    batch_id TEXT,
    company TEXT,
    source_account TEXT,
    remarks TEXT,
    remittance_type TEXT,
    transaction_date TEXT,
    transaction_status TEXT,
    amount TEXT NOT NULL,
    transaction_count INTEGER,
    beneficiary_code TEXT,
    beneficiary_name TEXT,
    beneficiary_account TEXT,
    beneficiary_address TEXT,
    journal_entry_id INTEGER,
    je_status TEXT,
    UNIQUE (uploaded_file_id, tran_id)
);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payment_instructions(transaction_status);
CREATE INDEX IF NOT EXISTS idx_payments_date ON payment_instructions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_payments_beneficiary ON payment_instructions(beneficiary_name);


CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT,
    email TEXT,
    role TEXT NOT NULL DEFAULT 'accountant'
        CHECK (role IN ('admin','accountant','viewer','department_user')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT,
    department_id INTEGER REFERENCES departments(id),
    company_id INTEGER REFERENCES companies(id),
    is_operations_manager INTEGER NOT NULL DEFAULT 0,
    notify_operations_approvals INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    failed_login_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

CREATE TABLE IF NOT EXISTS operations_manager_company_access (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, company_id)
);


CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    event_date TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    description TEXT,
    location TEXT,
    visibility TEXT NOT NULL DEFAULT 'shared'
        CHECK (visibility IN ('shared','private')),
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    reminder_minutes INTEGER,
    is_done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_calendar_events_date ON calendar_events(event_date, visibility, owner_user_id);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    user_id TEXT NOT NULL DEFAULT 'system',
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_logs(timestamp);

CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    company_id INTEGER REFERENCES companies(id),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name, company_id)
);

CREATE TABLE IF NOT EXISTS payment_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_type TEXT NOT NULL DEFAULT 'supplier_payment'
        CHECK (request_type IN ('supplier_payment','reimbursement')),
    requester_user_id INTEGER NOT NULL REFERENCES users(id),
    department_id INTEGER REFERENCES departments(id),
    supplier_name TEXT,
    payee_name TEXT NOT NULL,
    description TEXT NOT NULL,
    amount TEXT NOT NULL,
    due_date TEXT,
    status TEXT NOT NULL DEFAULT 'submitted'
        CHECK (status IN ('submitted','for_review','for_process','approved','paid','rejected','cancelled')),
    invoice_id INTEGER REFERENCES invoices(id),
    accounting_notes TEXT,
    paid_at TEXT,
    operations_approved_by_user_id INTEGER REFERENCES users(id),
    operations_approved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_payment_requests_status ON payment_requests(status);
CREATE INDEX IF NOT EXISTS idx_payment_requests_requester ON payment_requests(requester_user_id);
CREATE TABLE IF NOT EXISTS request_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES payment_requests(id) ON DELETE CASCADE,
    author_user_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_request_comments_request ON request_comments(request_id, created_at);

CREATE TABLE IF NOT EXISTS request_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES payment_requests(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    content_type TEXT,
    file_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    invoice_id INTEGER REFERENCES invoices(id),
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_request_attachments_request ON request_attachments(request_id);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    link_url TEXT,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read, created_at);

CREATE TABLE IF NOT EXISTS petty_cash_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date TEXT NOT NULL,
    description TEXT NOT NULL,
    amount TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'miscellaneous'
        CHECK (category IN ('ps_af','miscellaneous')),
    account_title TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','for_approval','approved','reimbursed')),
    created_by_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_petty_cash_entries_date ON petty_cash_entries(entry_date);
CREATE INDEX IF NOT EXISTS idx_petty_cash_entries_status ON petty_cash_entries(status);
CREATE INDEX IF NOT EXISTS idx_petty_cash_entries_category ON petty_cash_entries(category);

CREATE TABLE IF NOT EXISTS petty_cash_access (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    can_encode INTEGER NOT NULL DEFAULT 1,
    can_approve INTEGER NOT NULL DEFAULT 0,
    granted_by_user_id INTEGER REFERENCES users(id),
    granted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS closing_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_label TEXT NOT NULL,
    threshold TEXT NOT NULL,
    source_filename TEXT,
    financial_statement_filename TEXT,
    subsidiary_ledger_filename TEXT,
    financial_statement_stored_path TEXT,
    status TEXT NOT NULL DEFAULT 'in_progress'
        CHECK (status IN ('in_progress','completed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS closing_account_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES closing_runs(id) ON DELETE CASCADE,
    sheet TEXT,
    fs_row_number INTEGER,
    account_code TEXT NOT NULL,
    account_title TEXT,
    opening_balance TEXT NOT NULL,
    closing_balance TEXT NOT NULL,
    net_change TEXT NOT NULL,
    fs_inc_dec TEXT,
    ledger_debit TEXT,
    ledger_credit TEXT,
    ledger_rows INTEGER,
    flagged INTEGER NOT NULL DEFAULT 0,
    explanation TEXT,
    basis TEXT,
    reviewer_notes TEXT,
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_closing_changes_run ON closing_account_changes(run_id);



CREATE TABLE IF NOT EXISTS invoice_uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    parsed_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_file_id INTEGER NOT NULL REFERENCES invoice_uploads(id) ON DELETE CASCADE,
    invoice_number TEXT,
    invoice_date TEXT,
    supplier_name TEXT NOT NULL,
    supplier_id INTEGER REFERENCES suppliers(id),
    description TEXT,
    gross_amount TEXT NOT NULL,
    vat_amount TEXT,
    ewt_amount TEXT,
    net_amount TEXT NOT NULL,
    due_date TEXT,
    source_filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unmatched'
        CHECK (status IN ('unmatched','matched_payment','advance_payment','accrued_expense','reviewed','posted')),
    matched_bank_transaction_id INTEGER REFERENCES bank_transactions(id),
    matched_payment_instruction_id INTEGER REFERENCES payment_instructions(id),
    matched_journal_entry_id INTEGER REFERENCES journal_entries(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoices_supplier ON invoices(supplier_name);
CREATE INDEX IF NOT EXISTS idx_invoices_number ON invoices(invoice_number);

CREATE TABLE IF NOT EXISTS invoice_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER REFERENCES invoices(id) ON DELETE CASCADE,
    bank_transaction_id INTEGER REFERENCES bank_transactions(id),
    payment_instruction_id INTEGER REFERENCES payment_instructions(id),
    match_type TEXT NOT NULL CHECK (match_type IN ('matched_payment','advance_payment','accrued_expense')),
    amount_difference TEXT NOT NULL DEFAULT '0',
    score REAL NOT NULL DEFAULT 0,
    reason TEXT,
    journal_entry_id INTEGER REFERENCES journal_entries(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_invoice_matches_invoice ON invoice_matches(invoice_id);

CREATE TABLE IF NOT EXISTS bank_reconciliation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_filename TEXT NOT NULL,
    system_filename TEXT NOT NULL,
    reconciliation_type TEXT NOT NULL DEFAULT 'general_file_reconciliation'
        CHECK (reconciliation_type IN ('general_file_reconciliation','bank_vs_system','system_vs_raw','bank_vs_raw','left_vs_right')),
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bank_reconciliation_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES bank_reconciliation_runs(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('matched','bank_only','system_only','amount_difference','date_difference','possible_duplicate')),
    bank_date TEXT,
    bank_reference TEXT,
    bank_description TEXT,
    bank_amount TEXT,
    system_date TEXT,
    system_reference TEXT,
    system_description TEXT,
    system_amount TEXT,
    amount_difference TEXT,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_bank_recon_items_run ON bank_reconciliation_items(run_id);


CREATE TABLE IF NOT EXISTS open_item_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL DEFAULT 1,
    filename TEXT NOT NULL,
    account_filter TEXT,
    open_side TEXT NOT NULL DEFAULT 'debit'
        CHECK (open_side IN ('debit','credit')),
    row_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS open_item_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES open_item_runs(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('open','partial','closed')),
    open_date TEXT NOT NULL,
    account_code TEXT,
    account_title TEXT,
    reference TEXT,
    description TEXT,
    original_amount TEXT NOT NULL,
    closed_amount TEXT NOT NULL DEFAULT '0',
    open_balance TEXT NOT NULL DEFAULT '0',
    aging_days INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_open_item_details_run ON open_item_details(run_id, status);

CREATE TABLE IF NOT EXISTS open_item_closures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES open_item_runs(id) ON DELETE CASCADE,
    open_reference TEXT,
    close_reference TEXT,
    open_date TEXT,
    close_date TEXT,
    amount TEXT NOT NULL,
    close_description TEXT
);
CREATE INDEX IF NOT EXISTS idx_open_item_closures_run ON open_item_closures(run_id);

CREATE TABLE IF NOT EXISTS tax_filings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL DEFAULT 1,
    form_code TEXT NOT NULL,
    year INTEGER NOT NULL,
    period_index INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'not_started'
        CHECK (status IN ('not_started','in_process','filed')),
    filed_date TEXT,
    reference_number TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, form_code, year, period_index)
);
CREATE INDEX IF NOT EXISTS idx_tax_filings_form_year ON tax_filings(company_id, form_code, year);

CREATE TABLE IF NOT EXISTS fs_uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_label TEXT NOT NULL,
    company_name TEXT,
    source_filename TEXT,
    is_columns_json TEXT,
    bs_columns_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fs_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fs_upload_id INTEGER NOT NULL REFERENCES fs_uploads(id) ON DELETE CASCADE,
    sheet TEXT NOT NULL CHECK (sheet IN ('IS','BS')),
    row_number INTEGER NOT NULL,
    account_code TEXT,
    account_title TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    inc_dec TEXT,
    remarks TEXT,
    edited_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_fs_rows_upload ON fs_rows(fs_upload_id, sheet);

CREATE TABLE IF NOT EXISTS login_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address  TEXT NOT NULL,
    attempted_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time
    ON login_attempts(ip_address, attempted_at);

"""


def _current_db_path() -> Path:
    """Return the active DB path, re-reading the env var each call for test isolation."""
    return Path(os.environ.get("ACCOUNTING_DB", str(DB_PATH)))


def get_connection() -> sqlite3.Connection:
    """Open a connection with foreign keys enforced and Row factory."""
    conn = sqlite3.connect(_current_db_path(), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Convenience context manager."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if missing and run idempotent seeds. Also runs in-place migrations."""
    _db_path = _current_db_path()
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    # Ensure all upload subdirectories exist (start.sh may lack permission on the volume)
    _data = _db_path.parent
    for subdir in (
        "uploads/request_attachments",
        "uploads/payment_receipts",
        "uploads/invoices",
        "uploads/journal_learning",
        "commission_carry",
        "error_logs",
    ):
        (_data / subdir).mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _seed_if_empty(conn)
        # Prune IP rate-limit history older than 24 h to prevent unbounded growth.
        conn.execute(
            "DELETE FROM login_attempts WHERE attempted_at < datetime('now', '-24 hours')"
        )


def _migrate(conn) -> None:
    """Idempotent column additions for existing databases."""
    cols = {row["name"] for row in conn.execute(
        "PRAGMA table_info(payment_instructions)").fetchall()}
    if "journal_entry_id" not in cols:
        conn.execute("ALTER TABLE payment_instructions ADD COLUMN journal_entry_id INTEGER")
    if "je_status" not in cols:
        conn.execute("ALTER TABLE payment_instructions ADD COLUMN je_status TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_payments_je ON payment_instructions(journal_entry_id)")
    # closing_runs / closing_account_changes incremental columns
    cr_cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(closing_runs)").fetchall()}
    for name in ("financial_statement_filename", "subsidiary_ledger_filename", "financial_statement_stored_path"):
        if name not in cr_cols:
            conn.execute(f"ALTER TABLE closing_runs ADD COLUMN {name} TEXT")

    cac_cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(closing_account_changes)").fetchall()}
    closing_extra_cols = {
        "basis": "TEXT",
        "reviewer_notes": "TEXT",
        "sheet": "TEXT",
        "fs_row_number": "INTEGER",
        "fs_inc_dec": "TEXT",
        "ledger_debit": "TEXT",
        "ledger_credit": "TEXT",
        "ledger_rows": "INTEGER",
    }
    for name, ddl in closing_extra_cols.items():
        if name not in cac_cols:
            conn.execute(f"ALTER TABLE closing_account_changes ADD COLUMN {name} {ddl}")

    inv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(invoices)").fetchall()}
    if inv_cols and "matched_payment_instruction_id" not in inv_cols:
        conn.execute("ALTER TABLE invoices ADD COLUMN matched_payment_instruction_id INTEGER")

    recon_cols = {r["name"] for r in conn.execute("PRAGMA table_info(bank_reconciliation_runs)").fetchall()}
    if recon_cols and "reconciliation_type" not in recon_cols:
        conn.execute("ALTER TABLE bank_reconciliation_runs ADD COLUMN reconciliation_type TEXT NOT NULL DEFAULT 'bank_vs_system'")

    # Company-scoped accounting data: each company shares calendar/admin but
    # has separate uploads, bank statements, JE learning, and accounting tabs.
    for table_name in ("uploaded_files", "bank_transactions", "journal_entries"):
        table_cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
        if table_cols and "company_id" not in table_cols:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN company_id INTEGER NOT NULL DEFAULT 1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uploaded_files_company ON uploaded_files(company_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_tx_company ON bank_transactions(company_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_je_company ON journal_entries(company_id)")


    conn.executescript("""
    CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS operations_manager_company_access (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        PRIMARY KEY (user_id, company_id)
    );
    """)

    user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    user_extra_cols = {
        "department_id": "INTEGER REFERENCES departments(id)",
        "email": "TEXT",
        "company_id": "INTEGER REFERENCES companies(id)",
        "is_operations_manager": "INTEGER NOT NULL DEFAULT 0",
        "notify_operations_approvals": "INTEGER NOT NULL DEFAULT 1",
        "must_change_password": "INTEGER NOT NULL DEFAULT 0",
        "failed_login_attempts": "INTEGER NOT NULL DEFAULT 0",
        "locked_until": "TEXT",
    }
    for name, ddl in user_extra_cols.items():
        if user_cols and name not in user_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
    if user_cols:
        # Remove users pointing to deleted companies before FK validation
        conn.execute("DELETE FROM users WHERE company_id IS NOT NULL AND company_id NOT IN (SELECT id FROM companies)")
        # Backfill NULL company_ids: prefer company 1 if it exists, else use first active company
        if conn.execute("SELECT 1 FROM companies WHERE id = 1").fetchone():
            conn.execute("UPDATE users SET company_id = COALESCE(company_id, 1)")
        else:
            _first_co = conn.execute("SELECT id FROM companies WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()
            if _first_co:
                conn.execute("UPDATE users SET company_id = ? WHERE company_id IS NULL", (_first_co["id"],))
        conn.execute("""INSERT OR IGNORE INTO operations_manager_company_access (user_id, company_id)
                      SELECT id, company_id FROM users WHERE is_operations_manager = 1 AND company_id IS NOT NULL""")

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS departments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        company_id INTEGER REFERENCES companies(id),
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (name, company_id)
    );
    CREATE TABLE IF NOT EXISTS payment_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_type TEXT NOT NULL DEFAULT 'supplier_payment'
            CHECK (request_type IN ('supplier_payment','reimbursement')),
        requester_user_id INTEGER NOT NULL REFERENCES users(id),
        department_id INTEGER REFERENCES departments(id),
        supplier_name TEXT,
        payee_name TEXT NOT NULL,
        description TEXT NOT NULL,
        amount TEXT NOT NULL,
        due_date TEXT,
        status TEXT NOT NULL DEFAULT 'submitted'
            CHECK (status IN ('submitted','for_review','for_process','approved','paid','rejected','cancelled')),
        invoice_id INTEGER REFERENCES invoices(id),
        accounting_notes TEXT,
        paid_at TEXT,
        operations_approved_by_user_id INTEGER REFERENCES users(id),
        operations_approved_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_payment_requests_status ON payment_requests(status);
    CREATE INDEX IF NOT EXISTS idx_payment_requests_requester ON payment_requests(requester_user_id);
    CREATE TABLE IF NOT EXISTS request_attachments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL REFERENCES payment_requests(id) ON DELETE CASCADE,
        filename TEXT NOT NULL,
        stored_path TEXT NOT NULL,
        content_type TEXT,
        file_size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        invoice_id INTEGER REFERENCES invoices(id),
        uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_request_attachments_request ON request_attachments(request_id);
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        link_url TEXT,
        is_read INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read, created_at);
    """)
    dept_cols = {r["name"] for r in conn.execute("PRAGMA table_info(departments)").fetchall()}
    if dept_cols and "company_id" not in dept_cols:
        conn.execute("ALTER TABLE departments ADD COLUMN company_id INTEGER REFERENCES companies(id)")
    if dept_cols:
        # Remove departments pointing to deleted companies before FK validation
        conn.execute("DELETE FROM departments WHERE company_id IS NOT NULL AND company_id NOT IN (SELECT id FROM companies)")
        # Backfill NULL company_ids: prefer company 1 if it exists, else use first active company
        if conn.execute("SELECT 1 FROM companies WHERE id = 1").fetchone():
            conn.execute("UPDATE departments SET company_id = COALESCE(company_id, 1)")
        else:
            _first_co = conn.execute("SELECT id FROM companies WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()
            if _first_co:
                conn.execute("UPDATE departments SET company_id = ? WHERE company_id IS NULL", (_first_co["id"],))

    # Normalize department data before applying/using uniqueness.  Some upgraded
    # databases may already contain duplicate department rows for the same company
    # because older forms allowed free-text entry.  Keep the oldest row, repoint
    # users and requests to it, and remove the duplicates so department dropdowns
    # and submissions are stable.
    duplicate_groups = conn.execute(
        """SELECT lower(trim(name)) AS key_name, COALESCE(company_id, 1) AS company_id, MIN(id) AS keep_id, GROUP_CONCAT(id) AS ids, COUNT(*) AS n
           FROM departments
           GROUP BY lower(trim(name)), COALESCE(company_id, 1)
           HAVING COUNT(*) > 1"""
    ).fetchall()
    for group in duplicate_groups:
        keep_id = group["keep_id"]
        duplicate_ids = [int(x) for x in (group["ids"] or "").split(",") if int(x) != keep_id]
        for duplicate_id in duplicate_ids:
            conn.execute("UPDATE users SET department_id = ? WHERE department_id = ?", (keep_id, duplicate_id))
            conn.execute("UPDATE payment_requests SET department_id = ? WHERE department_id = ?", (keep_id, duplicate_id))
            conn.execute("DELETE FROM departments WHERE id = ?", (duplicate_id,))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_departments_company_name_ci ON departments (COALESCE(company_id, 1), lower(trim(name)))")

    # Keep the database-level duplicate protection active after cleanup.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_departments_company_name_ci ON departments (COALESCE(company_id, 1), lower(trim(name)))")

    standard_departments = ["HR and Admin", "Sales", "Accounting", "Marketing", "Technical"]
    company_rows = conn.execute("SELECT id FROM companies WHERE is_active = 1 ORDER BY id").fetchall()
    for company in company_rows:
        for dept_name in standard_departments:
            conn.execute("INSERT OR IGNORE INTO departments (name, company_id) VALUES (?, ?)", (dept_name, company["id"]))

    # Existing operations-manager users should be approvers for both companies.
    # The approvals page still filters to high-value reimbursements only.
    conn.execute("""INSERT OR IGNORE INTO operations_manager_company_access (user_id, company_id)
                  SELECT u.id, c.id
                  FROM users u
                  CROSS JOIN companies c
                  WHERE u.is_active = 1 AND u.is_operations_manager = 1 AND c.is_active = 1""")

    # Compatibility repair for databases touched by the earlier v2 migration where
    # SQLite rewrote existing foreign keys to departments_legacy during a table
    # rebuild.  Keeping this mirror table present prevents department-related
    # inserts from failing, while new installs continue to use departments.
    fk_targets = {r["table"] for table_name in ("users", "payment_requests") for r in conn.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()}
    if "departments_legacy" in fk_targets:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS departments_legacy (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            company_id INTEGER REFERENCES companies(id),
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (name, company_id)
        );
        """)
        conn.execute("DELETE FROM departments_legacy")
        conn.execute("INSERT OR IGNORE INTO departments_legacy (id, name, company_id, is_active, created_at) SELECT id, name, company_id, is_active, created_at FROM departments")

    pr_cols = {r["name"] for r in conn.execute("PRAGMA table_info(payment_requests)").fetchall()}
    pr_extra_cols = {
        "company_id": "INTEGER REFERENCES companies(id)",
        "operations_approved_by_user_id": "INTEGER REFERENCES users(id)",
        "operations_approved_at": "TEXT",
        "account_id": "INTEGER REFERENCES chart_of_accounts(id)",
        "operations_notes": "TEXT",
        "updated_at": "TEXT",
        "requester_email": "TEXT",
        "journal_basis_filename": "TEXT",
        "journal_basis_path": "TEXT",
        "is_draft": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, ddl in pr_extra_cols.items():
        if pr_cols and name not in pr_cols:
            conn.execute(f"ALTER TABLE payment_requests ADD COLUMN {name} {ddl}")
    if pr_cols:
        # Remove payment_requests pointing to deleted companies before FK validation
        conn.execute("DELETE FROM payment_requests WHERE company_id IS NOT NULL AND company_id NOT IN (SELECT id FROM companies)")
        # Backfill NULL company_ids: prefer company 1 if it exists, else use first active company
        if conn.execute("SELECT 1 FROM companies WHERE id = 1").fetchone():
            conn.execute("UPDATE payment_requests SET company_id = COALESCE(company_id, 1)")
        else:
            _first_co = conn.execute("SELECT id FROM companies WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()
            if _first_co:
                conn.execute("UPDATE payment_requests SET company_id = ? WHERE company_id IS NULL", (_first_co["id"],))

    # Line items for multi-category reimbursements
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS request_line_items (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id  INTEGER NOT NULL REFERENCES payment_requests(id) ON DELETE CASCADE,
        account_id  INTEGER REFERENCES chart_of_accounts(id),
        description TEXT,
        amount      REAL    NOT NULL DEFAULT 0,
        sort_order  INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_rli_request ON request_line_items(request_id);

    CREATE TABLE IF NOT EXISTS payment_receipts (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id          INTEGER NOT NULL REFERENCES payment_requests(id) ON DELETE CASCADE,
        filename            TEXT    NOT NULL,
        stored_path         TEXT    NOT NULL,
        content_type        TEXT,
        file_size           INTEGER NOT NULL DEFAULT 0,
        uploaded_by_user_id INTEGER REFERENCES users(id),
        uploaded_at         TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_receipts_request ON payment_receipts(request_id);
    """)# Two-way comment thread on a payment request (requester <-> accounting/operations)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS request_comments (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id      INTEGER NOT NULL REFERENCES payment_requests(id) ON DELETE CASCADE,
        author_user_id  INTEGER NOT NULL REFERENCES users(id),
        body            TEXT    NOT NULL,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_request_comments_request ON request_comments(request_id, created_at);
    """)

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS commission_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        previous_filename TEXT,
        rows_count INTEGER NOT NULL DEFAULT 0,
        total_payable TEXT NOT NULL DEFAULT '0',
        next_negative_carry TEXT NOT NULL DEFAULT '0',
        created_by_user_id INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS petty_cash_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_date TEXT NOT NULL,
        description TEXT NOT NULL,
        amount TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'miscellaneous'
            CHECK (category IN ('ps_af','miscellaneous')),
        account_title TEXT,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','for_approval','approved','reimbursed')),
        created_by_user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_petty_cash_entries_date ON petty_cash_entries(entry_date);
    CREATE INDEX IF NOT EXISTS idx_petty_cash_entries_status ON petty_cash_entries(status);
    CREATE INDEX IF NOT EXISTS idx_petty_cash_entries_category ON petty_cash_entries(category);
    CREATE TABLE IF NOT EXISTS petty_cash_access (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        can_encode INTEGER NOT NULL DEFAULT 1,
        can_approve INTEGER NOT NULL DEFAULT 0,
        granted_by_user_id INTEGER REFERENCES users(id),
        granted_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS calendar_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        event_date TEXT NOT NULL,
        start_time TEXT,
        end_time TEXT,
        description TEXT,
        location TEXT,
        visibility TEXT NOT NULL DEFAULT 'shared'
            CHECK (visibility IN ('shared','private')),
        owner_user_id INTEGER NOT NULL REFERENCES users(id),
        reminder_minutes INTEGER,
        is_done INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_calendar_events_date ON calendar_events(event_date, visibility, owner_user_id);
    """)

    # journal_learning_entry_templates for multi-account (VAT-split) patterns
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS journal_learning_entry_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL DEFAULT 1,
        group_key TEXT NOT NULL,
        account_code TEXT NOT NULL,
        account_title TEXT NOT NULL,
        normal_side TEXT NOT NULL,
        amount_ratio REAL NOT NULL DEFAULT 1.0,
        times_seen INTEGER NOT NULL DEFAULT 1,
        sample_description TEXT,
        learned_from_filename TEXT,
        last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(company_id, group_key, account_code)
    );
    CREATE TABLE IF NOT EXISTS journal_learning_basis_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL DEFAULT 1,
        filename TEXT NOT NULL,
        stored_path TEXT NOT NULL,
        uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS journal_learning_description_patterns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL DEFAULT 1,
        keywords TEXT NOT NULL,
        account_code TEXT NOT NULL,
        account_title TEXT NOT NULL,
        normal_side TEXT NOT NULL,
        times_seen INTEGER NOT NULL DEFAULT 1,
        sample_description TEXT,
        learned_from_filename TEXT,
        last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(company_id, keywords, account_code, normal_side)
    );
    """)

    # â”€â”€ Legacy GL migration blocks removed â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Company-specific account codes, bank account names, supplier mappings,
    # and one-time name fixes were removed from source code for security.
    # The data has already been applied to the production database.

    # journal_entries: also add rejected status to CHECK constraint workaround via ALTER
    je_cols = {r["name"] for r in conn.execute("PRAGMA table_info(journal_entries)").fetchall()}
    # Add rejected status support via app-level handling (SQLite ALTER TABLE CHECK is limited)
    je_extra_cols = {
        "payment_request_id": "INTEGER REFERENCES payment_requests(id) ON DELETE SET NULL",
    }
    for _col_name, _col_ddl in je_extra_cols.items():
        if je_cols and _col_name not in je_cols:
            conn.execute(f"ALTER TABLE journal_entries ADD COLUMN {_col_name} {_col_ddl}")

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS open_item_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        account_filter TEXT,
        open_side TEXT NOT NULL DEFAULT 'debit'
            CHECK (open_side IN ('debit','credit')),
        row_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'completed',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS open_item_details (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL REFERENCES open_item_runs(id) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK (status IN ('open','partial','closed')),
        open_date TEXT NOT NULL,
        account_code TEXT,
        account_title TEXT,
        reference TEXT,
        description TEXT,
        original_amount TEXT NOT NULL,
        closed_amount TEXT NOT NULL DEFAULT '0',
        open_balance TEXT NOT NULL DEFAULT '0',
        aging_days INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_open_item_details_run ON open_item_details(run_id, status);
    CREATE TABLE IF NOT EXISTS open_item_closures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL REFERENCES open_item_runs(id) ON DELETE CASCADE,
        open_reference TEXT,
        close_reference TEXT,
        open_date TEXT,
        close_date TEXT,
        amount TEXT NOT NULL,
        close_description TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_open_item_closures_run ON open_item_closures(run_id);
    CREATE TABLE IF NOT EXISTS commission_carry_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL,
        original_filename TEXT,
        stored_filename TEXT NOT NULL,
        total_payable TEXT NOT NULL DEFAULT '0',
        next_negative_carry TEXT NOT NULL DEFAULT '0',
        rows_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS sub_affiliate_dsp (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        sub_id      TEXT NOT NULL,
        sub_name    TEXT NOT NULL DEFAULT '',
        dsp_tag     TEXT NOT NULL DEFAULT '',
        updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(sub_id)
    );
    """)

    # Add company_id to open_item_runs for per-company isolation
    oir_cols = {r["name"] for r in conn.execute("PRAGMA table_info(open_item_runs)").fetchall()}
    if oir_cols and "company_id" not in oir_cols:
        conn.execute("ALTER TABLE open_item_runs ADD COLUMN company_id INTEGER NOT NULL DEFAULT 1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_open_item_runs_company ON open_item_runs(company_id)")

    # Migrate tax_filings to be company-scoped (recreate table to change unique constraint)
    tf_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tax_filings)").fetchall()}
    if tf_cols and "company_id" not in tf_cols:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tax_filings_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL DEFAULT 1,
                form_code TEXT NOT NULL,
                year INTEGER NOT NULL,
                period_index INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'not_started'
                    CHECK (status IN ('not_started','in_process','filed')),
                filed_date TEXT,
                reference_number TEXT,
                notes TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (company_id, form_code, year, period_index)
            );
            INSERT INTO tax_filings_new
                SELECT id, 1, form_code, year, period_index, status,
                       filed_date, reference_number, notes, updated_at
                FROM tax_filings;
            DROP TABLE tax_filings;
            ALTER TABLE tax_filings_new RENAME TO tax_filings;
        """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tax_filings_form_year ON tax_filings(company_id, form_code, year)")

    # Add session_version to users for server-side session invalidation.
    # When this column is incremented (e.g. on password change / admin reset),
    # all tokens that carry the old version are immediately rejected by
    # read_session_token() even before their natural expiry.
    u_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if u_cols and "session_version" not in u_cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0"
        )

    # TOTP two-factor authentication columns.
    # totp_secret      — base32 secret used to generate/verify TOTP codes
    # totp_enabled     — 1 when the user has completed 2FA setup
    # totp_backup_codes — JSON list of remaining single-use backup code hashes
    u_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    totp_cols = {
        "totp_secret": "TEXT",
        "totp_enabled": "INTEGER NOT NULL DEFAULT 0",
        "totp_backup_codes": "TEXT",
    }
    for col_name, col_ddl in totp_cols.items():
        if col_name not in u_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_ddl}")


# -----------------------------------------------------------------------------
# Seed data â€” chart of accounts for a fresh database installation.
# -----------------------------------------------------------------------------

SEED_ACCOUNTS = [
    # â”€â”€ Cash & Bank accounts â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # 1010 kept as the default JE cash-leg account (matched by CASH_ACCOUNT_CODE in generators)
    ("1010",    "Cash in Bank - Primary",                             "asset"),
    ("1101",    "Cash on Hand",                              "asset"),
    ("1102001", "Cash in Bank A/c 1 (Peso)",                  "asset"),
    ("1102002", "Cash in Bank A/c 2 (Peso)",               "asset"),
    ("1102004", "Cash in Bank A/c 1 (USD)",                   "asset"),
    ("1102005", "Cash in Bank A/c 3 (Peso)",                  "asset"),
    ("1102007", "Cash in Bank A/c 4 (Peso)",              "asset"),
    ("1102008", "Cash in Bank A/c 2 (USD)",                    "asset"),
    ("1102300", "Cash in Bank A/c 5 (Peso)",              "asset"),
    ("1103",    "E-Wallet Account 1",                         "asset"),
    ("1104",    "E-Wallet Account 2",                         "asset"),
    ("1105",    "Petty Cash",                                "asset"),
    ("1108",    "E-Wallet Account 3",                             "asset"),
    ("1109",    "E-Wallet Account 4",                                   "asset"),
    ("1110",    "E-Wallet Account 5",                             "asset"),
    ("1111",    "E-Wallet Account 6",                                  "asset"),
    ("1112",    "E-Wallet Account 7",                                    "asset"),
    ("1113",    "E-Wallet Account 8",                                   "asset"),
    ("1114",    "E-Wallet Account 9",                                   "asset"),
    # â”€â”€ Receivables & Other Assets â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    ("1147001", "Accounts Receivable - Trade",               "asset"),
    ("1201",    "Receivable from Customer",                  "asset"),
    ("1202",    "Advances to Officers and Employees",        "asset"),
    ("1203",    "Advances to Suppliers",                     "asset"),
    ("1207",    "Other Receivable",                          "asset"),
    ("1209",    "Receivable - Other",                       "asset"),
    ("1210",    "Input VAT",                                 "asset"),
    ("1218002", "Input VAT - Services",                      "asset"),
    ("1218004", "Input VAT - Other Than Capital Goods",      "asset"),
    ("1253008", "Prepaid Rent",                              "asset"),
    ("1253009", "Prepaid Expenses - Others",                 "asset"),
    ("1255",    "Prepaid Tax",                               "asset"),
    ("1533",    "Accum. Depr. - Furniture & Fixtures",       "asset"),
    ("1534",    "Accum. Depr. - Leasehold Improvement",      "asset"),
    ("1536",    "Accum. Depr. - Office Equipment",           "asset"),
    ("1543",    "Furniture & Fixtures",                      "asset"),
    ("1546",    "Office Equipment - Other",                  "asset"),
    ("1812",    "Security Deposit",                          "asset"),
    # â”€â”€ Liabilities â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    ("2110",    "Accounts Payable",                           "liability"),
    ("2113",    "Trade Payable - Regulatory",                   "liability"),
    ("2114",    "Trade Payable - Regulatory Audit",             "liability"),
    ("2120",    "Output VAT Payable",                       "liability"),
    ("2141",    "Accrued 13th Month Pay",                    "liability"),
    ("2142",    "Accrued Expenses",                          "liability"),
    ("2243",    "Advances from Related Party A",                         "liability"),
    ("2245",    "Customer Deposit - Others",                 "liability"),
    ("2246",    "Advances from Related Party B",                    "liability"),
    ("2249",    "Cash In from Payment Solutions",            "liability"),
    ("2250",    "Cash Out from Payment Solutions",           "liability"),
    ("2251",    "Advances from Players",                     "liability"),
    ("2257",    "Advances from OE",                          "liability"),
    ("2311",    "Expanded Withholding Tax Payable",          "liability"),
    ("2313",    "Final Withholding Tax Payable",             "liability"),
    ("2822",    "PhilHealth Premium Payable",                "liability"),
    ("2823",    "Pag-IBIG Premium Payable",                  "liability"),
    ("2825",    "SSS Loan Payable",                          "liability"),
    ("2826",    "SSS Premium Payable",                       "liability"),
    ("2827",    "Withholding Tax on Compensation",           "liability"),
    ("2828",    "HDMF Loan Payable",                         "liability"),
    ("2834",    "Provision for Income Tax",                  "liability"),
    # â”€â”€ Income â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    ("4101",    "Revenue - Primary Operations",               "income"),
    ("4106",    "Revenue Sharing - Partner A",                   "income"),
    ("4109",    "Revenue Sharing - Partner B",                 "income"),
    ("4111",    "Revenue Sharing - Partner C",                 "income"),
    ("4201",    "Revenue Share - Machines",            "income"),
    ("7211",    "Unrealized Forex Gain",                     "income"),
    ("7213",    "Interest Income",                           "income"),
    # â”€â”€ Cost of Sales â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    ("5010",    "Rent Expense",                              "expense"),
    ("5020",    "Utilities Expense",                         "expense"),
    ("5040",    "Miscellaneous Expense",                     "expense"),
    ("5050",    "Salaries Expense",                          "expense"),
    ("5641004", "Cost of Sales - Other",                     "expense"),
    # â”€â”€ Operating Expenses â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    ("6236",    "Light and Water",                           "expense"),
    ("6238",    "Percentage Tax",                            "expense"),
    ("6601",    "Professional Fees",                         "expense"),
    ("6602",    "Delivery Charges",                          "expense"),
    ("6603",    "Bank Charges",                              "expense"),
    ("6604",    "Communication - Tel & Internet",            "expense"),
    ("6605",    "Repairs and Maintenance",                   "expense"),
    ("6606",    "Taxes and Licenses",                        "expense"),
    ("6607",    "Documentary Stamp",                         "expense"),
    ("6608",    "Gasoline & Oil",                            "expense"),
    ("6609",    "Parking & Toll Fees",                       "expense"),
    ("6610001", "Transportation & Travel - Domestic",        "expense"),
    ("6611002", "Depreciation - Furniture & Fixtures",       "expense"),
    ("6611003", "Depreciation - Leasehold Improvement",      "expense"),
    ("6611004", "Depreciation - Office Equipment",           "expense"),
    ("6612001", "Commission Expenses",                       "expense"),
    ("6612003", "Advertising & Promotions - Raffle",         "expense"),
    ("6612005", "Advertising & Promotions - Materials",      "expense"),
    ("6612007", "Advertising & Promotions - Others",         "expense"),
    ("6612008", "Advertising Promotion Bonus - Brand A",     "expense"),
    ("6612010", "Advertising Promotion Bonus - Brand B",     "expense"),
    ("6613",    "Representation Expense",                    "expense"),
    ("6614",    "Office Supplies",                           "expense"),
    ("6616",    "Insurance Expenses",                        "expense"),
    ("6618",    "Training and Seminar",                      "expense"),
    ("6619001", "Salaries",                                  "expense"),
    ("6619002", "13th Month Pay",                            "expense"),
    ("6619003", "Overtime Pay",                              "expense"),
    ("6619004", "Employee Benefits",                         "expense"),
    ("6620001", "HDMF Contribution",                         "expense"),
    ("6620002", "PhilHealth Contribution",                   "expense"),
    ("6620003", "SSS Contribution",                          "expense"),
    ("6621",    "Rent Expenses",                             "expense"),
    ("6624",    "Association Dues",                          "expense"),
    ("6625",    "Subscription - Software and Services",      "expense"),
    ("6628",    "Payment Solution Charges",                  "expense"),
    ("7402",    "Unrealized Forex Loss",                     "expense"),
    ("7403",    "Other Loss",                                "expense"),
    ("8204",    "Franchise Tax",                             "expense"),
    # â”€â”€ Suspense (always last resort) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    ("5999",    "Suspense - To Be Classified",               "expense"),
]

# Known suppliers (user can add via the UI).
SEED_SUPPLIERS = [
    # (name, tin, default_expense_account_code, [aliases])
    (
        "Cella Storage Space Rental",
        "248-018-726-00000",
        "5010",
        ["CELLA STORAGE SPACE RENTAL"],
    ),
    (
        "Globe Telecom Inc",
        None,
        None,
        ["GLOBE B2B RAC PHIL HD M360 API"],
    ),
]

# Pattern-based classification rules. Lower priority number = applied first.
# direction:
#   'debit'  = the matched account is debited (transaction is an outflow / expense)
#   'credit' = the matched account is credited (transaction is an inflow / income)
#   'auto'   = derive from net_amount sign
SEED_RULES = [
    # priority, pattern, type, account code, direction, description
    # â”€â”€ Standard rules â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    (10, r"^INWARD CLEARING CHECK$",      "regex",   "5999",   "auto",   "Outgoing check cleared - needs supplier classification"),
    (10, r"^OUTWARD REMITTANCE VIA PCHC$","regex",   "5999",   "auto",   "Outgoing PCHC remittance - needs supplier classification"),
    (15, r"^Sent to ",                    "regex",   "5999",   "auto",   "Generic outgoing transfer - supplier classification needed"),
    (15, r"^Received from ",              "regex",   "4101",   "credit", "Generic incoming transfer - revenue (default)"),
    (20, r"ENCASHMENT",                   "keyword", "1105",   "debit",  "Cash withdrawal - moves to Petty Cash"),
    (20, r"CASH DEP",                     "keyword", "1101",   "credit", "Cash deposit - Cash on Hand"),
    (20, r"PAYROLL",                      "keyword", "5050",   "debit",  "Payroll outflow - Salaries"),
    (20, r"BIR EPAYMENT",                 "keyword", "6606",   "debit",  "BIR tax payment - Taxes and Licenses"),
    # â”€â”€ Employee advances (expense-related only) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Record as Advances to Officers and Employees ONLY when no receipt is available.
    # When the employee submits receipts, accounting creates a liquidation entry:
    #   Dr [Expense Account] / Cr Advances to Officers and Employees
    # If no receipts are submitted, the advance stays on the books as an asset.
    (22, r"ADVANCE",                      "keyword", "1202",   "debit",  "Employee advance (no receipt yet) â€” Dr Advances to Officers/Employees / Cr Cash"),
    (25, r"BILLS PAYMENT",                "keyword", "6236",   "debit",  "Bills payment - Light and Water (default)"),
    (25, r"SERVICE FEE",                  "keyword", "6603",   "debit",  "Bank service fee - Bank Charges"),
    (25, r"^BY INST .* LOCAL CLEARING$",  "regex",   "5999",   "auto",   "Local clearing instrument"),
    (30, r"Int\.Pd",                      "regex",   "7213",   "credit", "Interest income paid by bank"),
    (30, r"WTax\.Pd",                     "regex",   "2311",   "debit",  "Expanded withholding tax paid"),
    (30, r"INREM VIA PCHC",               "keyword", "4101",   "credit", "Incoming remittance - revenue"),
    (30, r"ONLINE FUND TRANSFER",         "keyword", "5999",   "auto",   "Online transfer - generic, needs classification"),
    (30, r"ONLINE FUNDS TRANSFER",        "keyword", "5999",   "auto",   "Online transfer - generic, needs classification"),
    (30, r"COMMISSION",                   "keyword", "6612001","debit",  "Commission expense"),
    (30, r"RENT",                         "keyword", "6621",   "debit",  "Rent expense"),
    (30, r"PROFESSIONAL FEE",             "keyword", "6601",   "debit",  "Professional fees"),
    (30, r"OFFICE SUPPLIES",              "keyword", "6614",   "debit",  "Office supplies"),
    (30, r"REPRESENTATION",              "keyword", "6613",   "debit",  "Representation expense"),
    (30, r"GASOLINE",                     "keyword", "6608",   "debit",  "Gasoline & oil"),
    (30, r"SUBSCRIPTION",                "keyword", "6625",   "debit",  "Subscription software / services"),
    (30, r"INSURANCE",                    "keyword", "6616",   "debit",  "Insurance expense"),
    (30, r"TRAINING",                     "keyword", "6618",   "debit",  "Training and seminar"),
    (30, r"REPAIRS",                      "keyword", "6605",   "debit",  "Repairs and maintenance"),
    (35, r"FRANCHISE TAX",               "keyword", "8204",   "debit",  "Franchise tax"),
    (35, r"PERCENTAGE TAX",              "keyword", "6238",   "debit",  "Percentage tax"),
]


def _seed_if_empty(conn: sqlite3.Connection) -> None:
    cur = conn.execute("SELECT COUNT(*) AS n FROM chart_of_accounts")
    if cur.fetchone()["n"] > 0:
        return  # already seeded

    # Accounts
    conn.executemany(
        "INSERT INTO chart_of_accounts (code, name, type) VALUES (?, ?, ?)",
        SEED_ACCOUNTS,
    )
    # Account code -> id map
    code_to_id = {row["code"]: row["id"]
                  for row in conn.execute("SELECT id, code FROM chart_of_accounts")}

    # Suppliers + aliases
    for name, tin, default_code, aliases in SEED_SUPPLIERS:
        default_id = code_to_id.get(default_code) if default_code else None
        cur = conn.execute(
            "INSERT INTO suppliers (name, tin, default_expense_account_id) VALUES (?, ?, ?)",
            (name, tin, default_id),
        )
        sup_id = cur.lastrowid
        for alias in aliases:
            conn.execute(
                "INSERT INTO supplier_aliases (supplier_id, alias) VALUES (?, ?)",
                (sup_id, alias),
            )

    # Classification rules
    for priority, pattern, ptype, code, direction, description in SEED_RULES:
        acct_id = code_to_id.get(code)
        if acct_id is None:
            raise RuntimeError(f"Seed rule references unknown account code {code!r}")
        conn.execute(
            """INSERT INTO classification_rules
               (pattern, pattern_type, target_account_id, direction, priority, description)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (pattern, ptype, acct_id, direction, priority, description),
        )

    conn.execute(
        "INSERT INTO audit_logs (action, entity_type, details_json) VALUES (?, ?, ?)",
        ("seed", "database", json.dumps({"accounts": len(SEED_ACCOUNTS),
                                          "suppliers": len(SEED_SUPPLIERS),
                                          "rules": len(SEED_RULES)})),
    )


# -----------------------------------------------------------------------------
# Audit log helper
# -----------------------------------------------------------------------------
def log_action(conn: sqlite3.Connection,
               action: str,
               entity_type: str,
               entity_id: Optional[int] = None,
               details: Optional[dict] = None,
               user_id: str = "system") -> None:
    conn.execute(
        """INSERT INTO audit_logs (action, entity_type, entity_id, user_id, details_json)
           VALUES (?, ?, ?, ?, ?)""",
        (action, entity_type, entity_id, user_id,
         json.dumps(details) if details else None),
    )
