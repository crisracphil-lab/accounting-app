"""
Journal entry generator for bank transactions.

Supports both 2-line (simple) and multi-line (VAT-split / co-account) entries.
All generated entries have status='draft' and require explicit approval before posting.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional


CASH_ACCOUNT_CODE = "1010"   # Unionbank CIB - matches your existing JE convention


class JournalEntryGenerationError(Exception):
    pass


def generate_for_bank_transaction(conn: sqlite3.Connection,
                                  *,
                                  bank_transaction_id: int,
                                  classification,
                                  reference_prefix: str = "Bank") -> int:
    """
    Create a 2-line journal entry (simple: one expense/income account + cash).
    Returns the new journal_entry id.
    """
    tx = conn.execute(
        "SELECT * FROM bank_transactions WHERE id = ?", (bank_transaction_id,)
    ).fetchone()
    if tx is None:
        raise JournalEntryGenerationError(
            f"bank_transactions.id={bank_transaction_id} not found")

    cash_acct = conn.execute(
        "SELECT id, code, name FROM chart_of_accounts WHERE code = ?",
        (CASH_ACCOUNT_CODE,),
    ).fetchone()
    if cash_acct is None:
        raise JournalEntryGenerationError(
            f"Cash account {CASH_ACCOUNT_CODE} not in chart_of_accounts")

    debit_amt  = Decimal(str(tx["debit_amount"] or "0"))
    credit_amt = Decimal(str(tx["credit_amount"] or "0"))

    if debit_amt > 0 and credit_amt > 0:
        raise JournalEntryGenerationError(
            f"Bank tx {tx['transaction_id']} has both debit and credit > 0")
    if debit_amt == 0 and credit_amt == 0:
        raise JournalEntryGenerationError(
            f"Bank tx {tx['transaction_id']} has zero net effect")

    is_outflow = debit_amt > 0
    amount = debit_amt if is_outflow else credit_amt

    direction = classification.direction
    if direction == "auto":
        direction = "debit" if is_outflow else "credit"

    if is_outflow:
        if direction != "debit":
            raise JournalEntryGenerationError(
                f"Outflow classified as credit-side {classification.target_account_name!r}: "
                f"would unbalance the books. Tx {tx['transaction_id']}")
        debit_account_id  = classification.target_account_id
        credit_account_id = cash_acct["id"]
    else:
        if direction != "credit":
            raise JournalEntryGenerationError(
                f"Inflow classified as debit-side {classification.target_account_name!r}. "
                f"Tx {tx['transaction_id']}")
        debit_account_id  = cash_acct["id"]
        credit_account_id = classification.target_account_id

    reference = (f"{reference_prefix} {tx['transaction_id']} - "
                 f"{(tx['counterparty_name'] or tx['description'])[:60]}")
    description = (tx["remarks"] or tx["description"]).strip()

    method = ("historical_learning" if classification.rule_description and
              "Historical JE pattern" in classification.rule_description
              else ("rule_based" if classification.rule_pattern else "supplier_default"))

    cur = conn.execute(
        """INSERT INTO journal_entries
           (bank_transaction_id, uploaded_file_id, entry_date, reference,
            description, status, confidence_score, classification_method, company_id)
           VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?)""",
        (bank_transaction_id, tx["uploaded_file_id"], tx["transaction_date"],
         reference, description,
         classification.confidence,
         method,
         tx["company_id"] if "company_id" in tx.keys() else 1),
    )
    je_id = cur.lastrowid

    conn.execute(
        """INSERT INTO journal_entry_lines
           (journal_entry_id, account_id, debit, credit, description, line_order)
           VALUES (?, ?, ?, '0', ?, 1)""",
        (je_id, debit_account_id, str(amount), description),
    )
    conn.execute(
        """INSERT INTO journal_entry_lines
           (journal_entry_id, account_id, debit, credit, description, line_order)
           VALUES (?, ?, '0', ?, ?, 2)""",
        (je_id, credit_account_id, str(amount), description),
    )
    conn.execute(
        "UPDATE bank_transactions SET journal_entry_id = ?, status = 'journal_generated' "
        "WHERE id = ?",
        (je_id, bank_transaction_id),
    )

    _verify_balance(conn, je_id)
    return je_id


def generate_multiline_for_bank_transaction(
        conn: sqlite3.Connection,
        *,
        bank_transaction_id: int,
        patterns: List[dict],          # from match_all_historical_patterns()
        is_outflow: bool,
        confidence: float,
        reference_prefix: str = "Bank") -> int:
    """
    Create a multi-line journal entry from learned entry templates.

    For outflows (bank debit), each pattern produces a Dr line; cash is Cr.
    For inflows (bank credit), each pattern produces a Cr line; cash is Dr.

    patterns: list of {account_code, account_title, normal_side, amount_ratio, ...}
    Amounts are allocated proportionally; the last line absorbs any rounding remainder.
    """
    tx = conn.execute(
        "SELECT * FROM bank_transactions WHERE id = ?", (bank_transaction_id,)
    ).fetchone()
    if tx is None:
        raise JournalEntryGenerationError(f"bank_transactions.id={bank_transaction_id} not found")

    cash_acct = conn.execute(
        "SELECT id, code, name FROM chart_of_accounts WHERE code = ?", (CASH_ACCOUNT_CODE,)
    ).fetchone()
    if cash_acct is None:
        raise JournalEntryGenerationError(f"Cash account {CASH_ACCOUNT_CODE} not in chart_of_accounts")

    debit_amt  = Decimal(str(tx["debit_amount"] or "0"))
    credit_amt = Decimal(str(tx["credit_amount"] or "0"))
    total_amount = debit_amt if is_outflow else credit_amt
    if total_amount == 0:
        raise JournalEntryGenerationError(f"Bank tx {tx['transaction_id']} has zero net effect")

    reference = (f"{reference_prefix} {tx['transaction_id']} - "
                 f"{(tx['counterparty_name'] or tx['description'])[:60]}")
    description = (tx["remarks"] or tx["description"]).strip()

    cur = conn.execute(
        """INSERT INTO journal_entries
           (bank_transaction_id, uploaded_file_id, entry_date, reference,
            description, status, confidence_score, classification_method, company_id)
           VALUES (?, ?, ?, ?, ?, 'draft', ?, 'historical_learning', ?)""",
        (bank_transaction_id, tx["uploaded_file_id"], tx["transaction_date"],
         reference, description, confidence,
         tx["company_id"] if "company_id" in tx.keys() else 1),
    )
    je_id = cur.lastrowid

    # Allocate amounts using ratios; last line gets the remainder
    allocated = Decimal("0")
    line_order = 1
    for i, pat in enumerate(patterns):
        is_last = (i == len(patterns) - 1)
        if is_last:
            line_amount = total_amount - allocated
        else:
            line_amount = (total_amount * Decimal(str(pat["amount_ratio"]))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
        allocated += line_amount
        if line_amount <= 0:
            continue

        acct = conn.execute(
            "SELECT id FROM chart_of_accounts WHERE code = ?", (pat["account_code"],)
        ).fetchone()
        if acct is None:
            # Insert from learned data so the COA stays consistent
            _type = "asset" if "vat" in pat["account_title"].lower() or "input" in pat["account_title"].lower() else "expense"
            conn.execute(
                "INSERT OR IGNORE INTO chart_of_accounts (code, name, type) VALUES (?, ?, ?)",
                (pat["account_code"], pat["account_title"], _type))
            acct = conn.execute(
                "SELECT id FROM chart_of_accounts WHERE code = ?", (pat["account_code"],)
            ).fetchone()

        if is_outflow:
            # expense/asset lines are Dr; cash is Cr
            conn.execute(
                """INSERT INTO journal_entry_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES (?, ?, ?, '0', ?, ?)""",
                (je_id, acct["id"], str(line_amount), description, line_order))
        else:
            # income lines are Cr; cash is Dr
            conn.execute(
                """INSERT INTO journal_entry_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES (?, ?, '0', ?, ?, ?)""",
                (je_id, acct["id"], str(line_amount), description, line_order))
        line_order += 1

    # Cash leg (always the balancing entry)
    if is_outflow:
        conn.execute(
            """INSERT INTO journal_entry_lines
               (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES (?, ?, '0', ?, ?, ?)""",
            (je_id, cash_acct["id"], str(total_amount), description, line_order))
    else:
        conn.execute(
            """INSERT INTO journal_entry_lines
               (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES (?, ?, ?, '0', ?, ?)""",
            (je_id, cash_acct["id"], str(total_amount), description, line_order))

    conn.execute(
        "UPDATE bank_transactions SET journal_entry_id = ?, status = 'journal_generated' WHERE id = ?",
        (je_id, bank_transaction_id),
    )
    _verify_balance(conn, je_id)
    return je_id


def _verify_balance(conn: sqlite3.Connection, je_id: int) -> None:
    sums = conn.execute(
        """SELECT SUM(CAST(debit AS REAL)) AS dr, SUM(CAST(credit AS REAL)) AS cr
           FROM journal_entry_lines WHERE journal_entry_id = ?""", (je_id,)
    ).fetchone()
    if abs((sums["dr"] or 0) - (sums["cr"] or 0)) > 0.005:
        raise JournalEntryGenerationError(
            f"Generated JE {je_id} is unbalanced: Dr={sums['dr']} Cr={sums['cr']}")
