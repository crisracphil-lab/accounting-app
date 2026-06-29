"""Ingest RA260 payments register into payment_instructions."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.db import db, log_action
from app.parsers.ra260_payments import parse_ra260, RA260ParseError


@dataclass
class PaymentsIngestionResult:
    uploaded_file_id: int
    filename: str
    rows_total: int
    rows_inserted: int
    rows_skipped_duplicate: int


class DuplicatePaymentsUploadError(Exception):
    pass


def ingest_ra260(file_path) -> PaymentsIngestionResult:
    path = Path(file_path)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()

    rows = parse_ra260(path)

    with db() as conn:
        conn.execute("BEGIN")
        try:
            existing = conn.execute(
                "SELECT id, filename FROM uploaded_files WHERE sha256 = ?", (sha,)
            ).fetchone()
            if existing:
                raise DuplicatePaymentsUploadError(
                    f"This file was already uploaded as id={existing['id']} "
                    f"({existing['filename']}). SHA-256: {sha}")

            cur = conn.execute(
                "INSERT INTO uploaded_files (filename, file_type, file_size, sha256, "
                "bank_account, period_covered) VALUES (?, ?, ?, ?, ?, ?)",
                (path.name, path.suffix.lstrip(".").lower(), len(raw), sha,
                 rows[0].source_account if rows else None,
                 None),
            )
            file_id = cur.lastrowid

            inserted = duplicate = 0
            for r in rows:
                try:
                    conn.execute(
                        "INSERT INTO payment_instructions (uploaded_file_id, tran_id, "
                        "batch_id, company, source_account, remarks, remittance_type, "
                        "transaction_date, transaction_status, amount, transaction_count, "
                        "beneficiary_code, beneficiary_name, beneficiary_account, beneficiary_address) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (file_id, r.tran_id, r.batch_id, r.company, r.source_account,
                         r.remarks, r.remittance_type,
                         r.transaction_date.isoformat() if r.transaction_date else None,
                         r.transaction_status, str(r.amount), r.transaction_count,
                         r.beneficiary_code, r.beneficiary_name, r.beneficiary_account,
                         r.beneficiary_address),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    duplicate += 1

            conn.execute("UPDATE uploaded_files SET parsed_count = ? WHERE id = ?",
                         (inserted, file_id))
            log_action(conn, "ingest", "payments_register", file_id, {
                "filename": path.name,
                "sha256": sha,
                "rows_total": len(rows),
                "rows_inserted": inserted,
                "rows_skipped_duplicate": duplicate,
            })
            conn.execute("COMMIT")
            return PaymentsIngestionResult(
                uploaded_file_id=file_id, filename=path.name,
                rows_total=len(rows), rows_inserted=inserted,
                rows_skipped_duplicate=duplicate,
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
