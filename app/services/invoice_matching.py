from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path

from app.db import db, log_action
from app.parsers.invoice_upload import ParsedInvoice, parse_invoice_file


class InvoiceMatchingError(ValueError):
    pass


AMOUNT_TOLERANCE = Decimal("0.05")


def _money(value: Decimal | str | None) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value).replace(",", ""))


def _similar(a: str | None, b: str | None) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _days(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except ValueError:
        return None


def _account_id(conn, code: str) -> int:
    row = conn.execute("SELECT id FROM chart_of_accounts WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise InvoiceMatchingError(f"Required account code {code} is missing.")
    return int(row["id"])


def _find_supplier(conn, name: str) -> int | None:
    row = conn.execute("SELECT id, name FROM suppliers WHERE lower(name)=lower(?)", (name,)).fetchone()
    if row:
        return int(row["id"])
    best = None
    for r in conn.execute("SELECT id, name FROM suppliers WHERE is_active=1").fetchall():
        score = _similar(name, r["name"])
        if score >= 0.82 and (best is None or score > best[0]):
            best = (score, r["id"])
    return int(best[1]) if best else None


def ingest_invoice_upload(path: str | Path) -> dict:
    path = Path(path)
    parsed = parse_invoice_file(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with db() as conn:
        conn.execute("BEGIN")
        try:
            cur = conn.execute(
                "INSERT INTO invoice_uploads (filename, file_size, sha256, parsed_count) VALUES (?, ?, ?, ?)",
                (path.name, path.stat().st_size, digest, len(parsed)),
            )
            upload_id = cur.lastrowid
            for inv in parsed:
                supplier_id = _find_supplier(conn, inv.supplier_name)
                conn.execute(
                    """INSERT INTO invoices
                       (uploaded_file_id, invoice_number, invoice_date, supplier_name, supplier_id,
                        description, gross_amount, vat_amount, ewt_amount, net_amount, due_date, source_filename)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (upload_id, inv.invoice_number, inv.invoice_date, inv.supplier_name, supplier_id,
                     inv.description, str(inv.gross_amount), str(inv.vat_amount) if inv.vat_amount is not None else None,
                     str(inv.ewt_amount) if inv.ewt_amount is not None else None, str(inv.net_amount),
                     inv.due_date, path.name),
                )
            log_action(conn, "invoice_upload", "invoice_upload", upload_id, {"filename": path.name, "count": len(parsed)})
            conn.execute("COMMIT")
            return {"upload_id": upload_id, "parsed_count": len(parsed)}
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _create_je(conn, *, entry_date: str, reference: str, description: str, lines: list[tuple[str, Decimal, Decimal, str]], source: str) -> int:
    cur = conn.execute(
        """INSERT INTO journal_entries (entry_date, reference, description, status, classification_method)
           VALUES (?, ?, ?, 'draft', ?)""",
        (entry_date, reference, description, source),
    )
    je_id = cur.lastrowid
    for idx, (code, debit, credit, line_desc) in enumerate(lines, start=1):
        conn.execute(
            """INSERT INTO journal_entry_lines (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (je_id, _account_id(conn, code), str(debit), str(credit), line_desc, idx),
        )
    return je_id


def _best_bank_match(conn, invoice) -> tuple[str, int, Decimal, float, str] | None:
    amount = _money(invoice["net_amount"])
    supplier = invoice["supplier_name"]
    inv_date = invoice["invoice_date"]
    candidates = []
    for r in conn.execute(
        """SELECT id, transaction_date, description, counterparty_name, reference_number, net_amount, journal_entry_id
           FROM bank_transactions
           WHERE id NOT IN (SELECT COALESCE(matched_bank_transaction_id, -1) FROM invoices WHERE matched_bank_transaction_id IS NOT NULL)
           ORDER BY transaction_date DESC LIMIT 1000"""
    ).fetchall():
        tx_amount = abs(_money(r["net_amount"]))
        diff = abs(tx_amount - amount)
        if diff > Decimal("100.00"):
            continue
        text_score = max(_similar(supplier, r["counterparty_name"]), _similar(supplier, r["description"]))
        day_gap = _days(inv_date, r["transaction_date"])
        score = 0.0
        if diff <= AMOUNT_TOLERANCE:
            score += 0.55
        elif diff <= Decimal("100.00"):
            score += 0.25
        if text_score >= 0.45:
            score += min(0.30, text_score * 0.30)
        if day_gap is None or day_gap <= 30:
            score += 0.15 if day_gap is None else max(0.0, 0.15 - (day_gap / 300))
        if score >= 0.58:
            candidates.append((score, "bank", r["id"], diff, f"amount diff {diff}; text score {text_score:.2f}; date gap {day_gap}"))
    for r in conn.execute(
        """SELECT id, transaction_date, beneficiary_name, remarks, tran_id, amount, journal_entry_id
           FROM payment_instructions
           WHERE id NOT IN (SELECT COALESCE(matched_payment_instruction_id, -1) FROM invoices WHERE matched_payment_instruction_id IS NOT NULL)
           ORDER BY transaction_date DESC LIMIT 1000"""
    ).fetchall():
        pay_amount = abs(_money(r["amount"]))
        diff = abs(pay_amount - amount)
        if diff > Decimal("100.00"):
            continue
        text_score = max(_similar(supplier, r["beneficiary_name"]), _similar(supplier, r["remarks"]))
        day_gap = _days(inv_date, r["transaction_date"])
        score = 0.0
        if diff <= AMOUNT_TOLERANCE:
            score += 0.55
        elif diff <= Decimal("100.00"):
            score += 0.25
        if text_score >= 0.45:
            score += min(0.30, text_score * 0.30)
        if day_gap is None or day_gap <= 30:
            score += 0.15 if day_gap is None else max(0.0, 0.15 - (day_gap / 300))
        if score >= 0.58:
            candidates.append((score, "payment", r["id"], diff, f"amount diff {diff}; text score {text_score:.2f}; date gap {day_gap}"))
    if not candidates:
        return None
    candidates.sort(reverse=True, key=lambda x: x[0])
    score, source, source_id, diff, reason = candidates[0]
    return source, int(source_id), diff, float(score), reason


def match_invoices_and_payments() -> dict:
    """Match invoice uploads to real bank/payment data; create draft JEs for accruals/advances."""
    result = {"matched_payment": 0, "accrued_expense": 0, "advance_payment": 0}
    with db() as conn:
        conn.execute("BEGIN")
        try:
            invoices = conn.execute("SELECT * FROM invoices WHERE status='unmatched' ORDER BY id").fetchall()
            for inv in invoices:
                match = _best_bank_match(conn, inv)
                amount = _money(inv["net_amount"])
                gross = _money(inv["gross_amount"])
                vat = _money(inv["vat_amount"])
                if match:
                    source, source_id, diff, score, reason = match
                    je_id = None
                    if inv["matched_journal_entry_id"]:
                        je_id = inv["matched_journal_entry_id"]
                    conn.execute(
                        "UPDATE invoices SET status='matched_payment', matched_bank_transaction_id=?, matched_payment_instruction_id=? WHERE id=?",
                        (source_id if source == "bank" else None, source_id if source == "payment" else None, inv["id"]),
                    )
                    conn.execute(
                        """INSERT INTO invoice_matches (invoice_id, bank_transaction_id, payment_instruction_id, match_type, amount_difference, score, reason, journal_entry_id)
                           VALUES (?, ?, ?, 'matched_payment', ?, ?, ?, ?)""",
                        (inv["id"], source_id if source == "bank" else None, source_id if source == "payment" else None, str(diff), score, reason, je_id),
                    )
                    result["matched_payment"] += 1
                else:
                    expense = gross - vat if vat else amount
                    lines = [("5999", expense, Decimal("0"), "Expense pending account classification")]
                    if vat:
                        lines.append(("1210", vat, Decimal("0"), "Input VAT per invoice"))
                    lines.append(("2142", Decimal("0"), amount, "Accrued expense pending payment"))
                    je_id = _create_je(conn,
                        entry_date=inv["invoice_date"] or date.today().isoformat(),
                        reference=inv["invoice_number"] or f"INV-{inv['id']}",
                        description=f"Accrued expense for invoice from {inv['supplier_name']}",
                        lines=lines,
                        source="invoice_accrual",
                    )
                    conn.execute("UPDATE invoices SET status='accrued_expense', matched_journal_entry_id=? WHERE id=?", (je_id, inv["id"]))
                    conn.execute(
                        """INSERT INTO invoice_matches (invoice_id, match_type, amount_difference, score, reason, journal_entry_id)
                           VALUES (?, 'accrued_expense', '0', 1, 'invoice uploaded but no payment found', ?)""",
                        (inv["id"], je_id),
                    )
                    result["accrued_expense"] += 1
            # bank/payment outflows with no linked invoice become advance draft JEs
            for r in conn.execute(
                """SELECT id, transaction_date, description, net_amount, journal_entry_id FROM bank_transactions
                   WHERE CAST(net_amount AS REAL) < 0
                     AND id NOT IN (SELECT COALESCE(matched_bank_transaction_id, -1) FROM invoices WHERE matched_bank_transaction_id IS NOT NULL)
                     AND id NOT IN (SELECT COALESCE(bank_transaction_id, -1) FROM invoice_matches WHERE match_type='advance_payment' AND bank_transaction_id IS NOT NULL)
                   LIMIT 200"""
            ).fetchall():
                amt = abs(_money(r["net_amount"]))
                je_id = r["journal_entry_id"] or _create_je(conn,
                    entry_date=r["transaction_date"], reference=f"ADV-BANK-{r['id']}",
                    description=f"Advance payment without uploaded invoice: {r['description']}",
                    lines=[("1203", amt, Decimal("0"), "Advance to supplier"), ("1010", Decimal("0"), amt, "Bank payment")],
                    source="invoice_advance_bank",
                )
                conn.execute(
                    """INSERT INTO invoice_matches (bank_transaction_id, match_type, amount_difference, score, reason, journal_entry_id)
                       VALUES (?, 'advance_payment', '0', 1, 'payment exists but no invoice uploaded', ?)""",
                    (r["id"], je_id),
                )
                result["advance_payment"] += 1
            for r in conn.execute(
                """SELECT id, transaction_date, beneficiary_name, amount, journal_entry_id FROM payment_instructions
                   WHERE id NOT IN (SELECT COALESCE(matched_payment_instruction_id, -1) FROM invoices WHERE matched_payment_instruction_id IS NOT NULL)
                     AND id NOT IN (SELECT COALESCE(payment_instruction_id, -1) FROM invoice_matches WHERE match_type='advance_payment' AND payment_instruction_id IS NOT NULL)
                   LIMIT 200"""
            ).fetchall():
                amt = abs(_money(r["amount"]))
                je_id = r["journal_entry_id"] or _create_je(conn,
                    entry_date=r["transaction_date"] or date.today().isoformat(), reference=f"ADV-PAY-{r['id']}",
                    description=f"Advance payment without uploaded invoice: {r['beneficiary_name']}",
                    lines=[("1203", amt, Decimal("0"), "Advance to supplier"), ("1010", Decimal("0"), amt, "Bank payment")],
                    source="invoice_advance_payment",
                )
                conn.execute(
                    """INSERT INTO invoice_matches (payment_instruction_id, match_type, amount_difference, score, reason, journal_entry_id)
                       VALUES (?, 'advance_payment', '0', 1, 'payment exists but no invoice uploaded', ?)""",
                    (r["id"], je_id),
                )
                result["advance_payment"] += 1
            log_action(conn, "invoice_match", "invoice_matches", None, result)
            conn.execute("COMMIT")
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise
