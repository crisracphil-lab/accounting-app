"""File upload + ingestion pipeline."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.db import db, log_action
from app.parsers.bank_statement_generic import parse_generic_statement, GenericStatementParseError
from app.services.supplier_matcher import match_supplier
from app.services.classifier import classify, classify_all
from app.services.je_generator import (
    generate_for_bank_transaction,
    generate_multiline_for_bank_transaction,
)


@dataclass
class IngestionResult:
    uploaded_file_id: int
    filename: str
    transactions_total: int
    transactions_inserted: int
    transactions_skipped_duplicate: int
    transactions_skipped_zero_amount: int
    suppliers_matched: int
    journal_entries_drafted: int
    suspense_count: int


class DuplicateUploadError(Exception):
    pass


def ingest_statement(file_path, company_id: int = 1) -> IngestionResult:
    path = Path(file_path)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()

    # Duplicate check happens before parsing: cheaper fail and better UX.
    with db() as conn:
        existing = conn.execute(
            "SELECT id, filename FROM uploaded_files WHERE sha256 = ?", (sha,)
        ).fetchone()
        if existing:
            raise DuplicateUploadError(
                f"This file was already uploaded as id={existing['id']} "
                f"({existing['filename']}). SHA-256: {sha}"
            )

    txns = parse_generic_statement(path)

    with db() as conn:
        conn.execute("BEGIN")
        try:
            cur = conn.execute(
                "INSERT INTO uploaded_files (filename, file_type, file_size, sha256, "
                "bank_account, period_covered, company_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (path.name, path.suffix.lstrip(".").lower(), len(raw), sha,
                 None, None, company_id),
            )
            file_id = cur.lastrowid

            inserted = duplicate = matched = drafted = suspense = skipped_zero = 0

            for tx in txns:
                if tx.debit == 0 and tx.credit == 0:
                    skipped_zero += 1
                    continue

                try:
                    cur = conn.execute(
                        "INSERT INTO bank_transactions (uploaded_file_id, transaction_id, "
                        "transaction_date, posted_date, description, check_number, "
                        "debit_amount, credit_amount, net_amount, ending_balance, "
                        "reference_number, remarks, branch, biller_name, counterparty_name, company_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (file_id, f"{path.stem}-{tx.row_number}-{tx.reference or tx.date}",
                         tx.date, None, tx.description, None,
                         str(tx.debit), str(tx.credit), str(tx.amount),
                         str(tx.balance) if tx.balance is not None else None,
                         tx.reference, None, tx.bank_profile,
                         None, tx.description, company_id),
                    )
                    bt_id = cur.lastrowid
                    inserted += 1
                except sqlite3.IntegrityError:
                    duplicate += 1
                    continue

                m = match_supplier(conn,
                                   description=tx.description,
                                   counterparty_name=tx.description,
                                   remarks=None,
                                   biller_name=None)
                supplier_default = None
                if m:
                    matched += 1
                    sup_row = conn.execute(
                        "SELECT default_expense_account_id FROM suppliers WHERE id = ?",
                        (m.supplier_id,),
                    ).fetchone()
                    supplier_default = sup_row["default_expense_account_id"] if sup_row else None
                    conn.execute(
                        "UPDATE bank_transactions SET supplier_id = ?, "
                        "supplier_match_confidence = ?, supplier_match_reason = ?, "
                        "status = 'matched_supplier' WHERE id = ?",
                        (m.supplier_id, m.confidence, m.reason, bt_id),
                    )

                is_outflow = tx.debit > 0

                # Try multi-account (VAT-split) generation first when no supplier default
                multi_patterns = classify_all(
                    conn,
                    description=tx.description,
                    remarks=None,
                    biller_name=None,
                    supplier_default_account_id=supplier_default,
                    company_id=company_id,
                )

                cls = classify(
                    conn,
                    description=tx.description,
                    remarks=None,
                    biller_name=None,
                    supplier_default_account_id=supplier_default,
                    company_id=company_id,
                )
                conn.execute(
                    "UPDATE bank_transactions SET classification = ? WHERE id = ?",
                    (cls.target_account_code + " - " + cls.target_account_name, bt_id),
                )
                if cls.target_account_code == "5999":
                    suspense += 1

                if multi_patterns and len(multi_patterns) >= 2:
                    # Multi-line JE (e.g. Office Supplies + Input VAT + Cash)
                    confidence = multi_patterns[0].get("confidence", 0.80)
                    generate_multiline_for_bank_transaction(
                        conn,
                        bank_transaction_id=bt_id,
                        patterns=multi_patterns,
                        is_outflow=is_outflow,
                        confidence=confidence,
                    )
                else:
                    generate_for_bank_transaction(conn, bank_transaction_id=bt_id,
                                                  classification=cls)
                drafted += 1

            conn.execute(
                "UPDATE uploaded_files SET parsed_count = ? WHERE id = ?",
                (inserted, file_id),
            )

            log_action(conn, "ingest", "uploaded_file", file_id, {
                "filename": path.name,
                "sha256": sha,
                "transactions_total": len(txns),
                "transactions_inserted": inserted,
                "transactions_skipped_duplicate": duplicate,
                "transactions_skipped_zero_amount": skipped_zero,
                "suppliers_matched": matched,
                "journal_entries_drafted": drafted,
                "suspense_count": suspense,
            })

            conn.execute("COMMIT")
            return IngestionResult(
                uploaded_file_id=file_id, filename=path.name,
                transactions_total=len(txns),
                transactions_inserted=inserted,
                transactions_skipped_duplicate=duplicate,
                transactions_skipped_zero_amount=skipped_zero,
                suppliers_matched=matched,
                journal_entries_drafted=drafted,
                suspense_count=suspense,
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
