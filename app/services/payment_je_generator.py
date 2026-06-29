"""
Generate journal entries from payment_instructions.

For each eligible payment (Successful or Released), produce a balanced JE:
    Dr  <expense account from classifier or supplier default>     amount
    Cr  Unionbank CIB                                              amount

Skips if the payment already has a journal_entry_id.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from app.db import db, log_action
from app.services.supplier_matcher import match_supplier
from app.services.classifier import classify
from app.services.je_generator import CASH_ACCOUNT_CODE


ELIGIBLE_STATUSES = ("Transaction Successful", "Transaction Released")


@dataclass
class PaymentJEResult:
    payments_examined: int
    skipped_ineligible_status: int
    skipped_already_has_je: int
    je_generated: int
    suspense_count: int


def generate_jes_for_payments(payment_ids: Optional[List[int]] = None,
                              uploaded_file_id: Optional[int] = None) -> PaymentJEResult:
    """
    Generate JEs for the specified payment IDs, or for all payments in a
    given uploaded_file_id, or for everything if both are None.
    """
    examined = ineligible = already = generated = suspense = 0

    with db() as conn:
        cash_acct = conn.execute(
            "SELECT id FROM chart_of_accounts WHERE code = ?",
            (CASH_ACCOUNT_CODE,),
        ).fetchone()
        if cash_acct is None:
            raise RuntimeError(
                f"Cash account {CASH_ACCOUNT_CODE} missing from chart_of_accounts")
        cash_id = cash_acct["id"]

        # Pull eligible payments
        if payment_ids:
            # placeholders is derived from len(payment_ids), never from the
            # ID values themselves, so "?" * n is safe to concatenate into
            # the SQL string — the actual IDs still travel through ? params.
            placeholders = ",".join("?" * len(payment_ids))
            rows = conn.execute(
                "SELECT * FROM payment_instructions WHERE id IN (" + placeholders + ")",
                payment_ids,
            ).fetchall()
        elif uploaded_file_id is not None:
            rows = conn.execute(
                "SELECT * FROM payment_instructions WHERE uploaded_file_id = ?",
                (uploaded_file_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM payment_instructions"
            ).fetchall()

        for p in rows:
            examined += 1
            if p["transaction_status"] not in ELIGIBLE_STATUSES:
                ineligible += 1
                continue
            if p["journal_entry_id"]:
                already += 1
                continue

            amount = Decimal(p["amount"] or "0")
            if amount <= 0:
                ineligible += 1
                continue

            # Supplier match using beneficiary + remarks
            m = match_supplier(conn,
                               description=p["beneficiary_name"] or "",
                               counterparty_name=p["beneficiary_name"],
                               remarks=p["remarks"],
                               biller_name=None)
            supplier_default = None
            if m:
                sup_row = conn.execute(
                    "SELECT default_expense_account_id FROM suppliers WHERE id = ?",
                    (m.supplier_id,),
                ).fetchone()
                supplier_default = sup_row["default_expense_account_id"] if sup_row else None

            cls = classify(conn,
                           description=p["beneficiary_name"] or p["remarks"] or "",
                           remarks=p["remarks"],
                           biller_name=None,
                           supplier_default_account_id=supplier_default)
            if cls.target_account_code == "5999":
                suspense += 1

            # Build the JE
            ref_who = p["beneficiary_name"] or "(unknown beneficiary)"
            ref = f"Pmt {p['tran_id']} - {ref_who[:50]}"
            descr = (p["remarks"] or p["beneficiary_name"] or "Payment").strip()

            cur = conn.execute(
                "INSERT INTO journal_entries "
                "(uploaded_file_id, entry_date, reference, description, status, "
                " confidence_score, classification_method) "
                "VALUES (?, ?, ?, ?, 'draft', ?, ?)",
                (p["uploaded_file_id"], p["transaction_date"] or "",
                 ref, descr, cls.confidence,
                 "payment_register"),
            )
            je_id = cur.lastrowid

            # Two lines: Dr expense, Cr cash
            conn.execute(
                "INSERT INTO journal_entry_lines (journal_entry_id, account_id, debit, credit, description, line_order) "
                "VALUES (?, ?, ?, '0', ?, 1)",
                (je_id, cls.target_account_id, str(amount), descr),
            )
            conn.execute(
                "INSERT INTO journal_entry_lines (journal_entry_id, account_id, debit, credit, description, line_order) "
                "VALUES (?, ?, '0', ?, ?, 2)",
                (je_id, cash_id, str(amount), descr),
            )

            # Verify balance
            sums = conn.execute(
                "SELECT SUM(CAST(debit AS REAL)) AS dr, SUM(CAST(credit AS REAL)) AS cr "
                "FROM journal_entry_lines WHERE journal_entry_id = ?",
                (je_id,),
            ).fetchone()
            if abs((sums["dr"] or 0) - (sums["cr"] or 0)) > 0.005:
                raise RuntimeError(
                    f"Generated JE {je_id} unbalanced: Dr={sums['dr']} Cr={sums['cr']}")

            conn.execute(
                "UPDATE payment_instructions SET journal_entry_id = ?, je_status = 'draft' "
                "WHERE id = ?",
                (je_id, p["id"]),
            )
            generated += 1

        log_action(conn, "generate_jes", "payment_instructions", uploaded_file_id, {
            "examined": examined,
            "skipped_ineligible_status": ineligible,
            "skipped_already_has_je": already,
            "je_generated": generated,
            "suspense_count": suspense,
        })

    return PaymentJEResult(
        payments_examined=examined,
        skipped_ineligible_status=ineligible,
        skipped_already_has_je=already,
        je_generated=generated,
        suspense_count=suspense,
    )
