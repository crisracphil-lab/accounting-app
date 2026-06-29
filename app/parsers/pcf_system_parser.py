"""pcf_system_parser.py — Dedicated parser for PCF PER SYSTEM.XLS.

Reads the two-sheet accounting-system voucher export:

  Header sheet  — voucher metadata.  Row 3, col 3 contains the Voucher Date
                  (format "2026/05/22") applied to every transaction row.

  Detail sheet  — individual journal lines.  Only DEBIT lines (Dr/Cr = 1.0)
                  are extracted; credit lines (Dr/Cr = -1.0) are the offsetting
                  bank / cash-clearing entries and are intentionally ignored.

Returns list[GenericTransaction] with:
    debit  = Amt (Local Curr.)
    credit = 0
    amount = Amt (Local Curr.)
so that Pool-A matching (sys_debits ↔ bank_credits) works correctly when
paired with parse_pcf_excel().

Sheet detection is structural: works regardless of sheet order.
Column detection falls back to positional defaults (row 2 header confirmed
at col indices below) if the header scan fails.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.parsers.bank_statement_generic import GenericTransaction, GenericStatementParseError


class PCFSystemParseError(GenericStatementParseError):
    pass


# Default column positions in the Detail sheet (0-based), based on observed structure:
#   0  Serial No.
#   1  Dr/Cr            ← 1.0 = debit, −1.0 = credit
#   2  A/C No.
#   3  Acct Title
#   4  Dept.
#   5  Department Name
#   6  Currency
#   7  Exch. Rate
#   8  Amt in Trans. Curr.
#   9  Amt (Local Curr.)   ← PRIMARY AMOUNT
#  10  Contra A/C(1)
#  11  Description         ← NARRATIVE
_DEFAULT_DRCR_COL = 1
_DEFAULT_AMT_COL  = 9
_DEFAULT_DESC_COL = 11


def _parse_voucher_date(raw: Any) -> str:
    """Parse the voucher date cell from the Header sheet."""
    if raw is None:
        return ""
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    s = str(raw).strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return s  # return as-is if no format matches


def _to_amount(value: Any) -> Decimal | None:
    """Return a positive Decimal from a numeric cell, or None."""
    if value is None or str(value).strip() == "":
        return None
    try:
        d = Decimal(str(value))
        return d if d > 0 else None
    except (InvalidOperation, Exception):
        return None


def _find_header_cols(sheet: Any) -> tuple[int, int, int]:
    """
    Scan the first 10 rows for a header containing 'Dr/Cr', 'Amt' and
    'Description'.  Returns (drcr_col, amt_col, desc_col).
    Falls back to positional defaults if not found.
    """
    try:
        import xlrd
    except ImportError:
        return _DEFAULT_DRCR_COL, _DEFAULT_AMT_COL, _DEFAULT_DESC_COL

    for r in range(min(sheet.nrows, 10)):
        row = [str(sheet.cell(r, c).value).lower().strip()
               for c in range(min(sheet.ncols, 20))]
        if any("dr" in v and "cr" in v for v in row):
            drcr_col = next((c for c, v in enumerate(row) if "dr" in v and "cr" in v),
                            _DEFAULT_DRCR_COL)
            amt_col  = next((c for c, v in enumerate(row)
                             if "local curr" in v and "amt" in v),
                            _DEFAULT_AMT_COL)
            desc_col = next((c for c, v in enumerate(row)
                             if v in ("description", "desc", "narration", "remarks")),
                            _DEFAULT_DESC_COL)
            return drcr_col, amt_col, desc_col

    return _DEFAULT_DRCR_COL, _DEFAULT_AMT_COL, _DEFAULT_DESC_COL


# ── Main entry point ──────────────────────────────────────────────────────────

def parse_pcf_system(path: str | Path) -> list[GenericTransaction]:
    """Parse PCF PER SYSTEM.XLS and return a list of GenericTransaction objects."""
    path = Path(path)
    try:
        import xlrd
    except ImportError as exc:
        raise PCFSystemParseError(
            "xlrd is required to parse .xls accounting-system exports. "
            "Install it with: pip install xlrd"
        ) from exc

    try:
        wb = xlrd.open_workbook(str(path))
    except Exception as exc:
        raise PCFSystemParseError(f"Cannot open {path.name}: {exc}") from exc

    sheet_names_lower = {s.lower(): s for s in wb.sheet_names()}

    # ── Voucher date from Header sheet ────────────────────────────────────────
    voucher_date = ""
    if "header" in sheet_names_lower:
        hsh = wb.sheet_by_name(sheet_names_lower["header"])
        if hsh.nrows >= 4:
            raw = hsh.cell(3, 3).value  # row 3 (0-based), col 3 = Voucher Date
            voucher_date = _parse_voucher_date(raw)

    # ── Debit lines from Detail sheet ─────────────────────────────────────────
    if "detail" not in sheet_names_lower:
        raise PCFSystemParseError(
            "Expected a 'Detail' sheet in the PCF System export but none was found. "
            f"Sheets found: {', '.join(wb.sheet_names())}"
        )

    dsh = wb.sheet_by_name(sheet_names_lower["detail"])
    drcr_col, amt_col, desc_col = _find_header_cols(dsh)

    # The data starts at row 3 (0-based); row 2 is the column header
    data_start = 3

    out: list[GenericTransaction] = []
    row_no = 0

    for r in range(data_start, dsh.nrows):
        # Dr/Cr flag
        drcr_cell = dsh.cell(r, drcr_col)
        try:
            drcr = float(drcr_cell.value)
        except (TypeError, ValueError):
            continue
        if drcr != 1.0:
            continue  # skip credit lines

        # Description
        desc = str(dsh.cell(r, desc_col).value).strip() if desc_col < dsh.ncols else ""
        if not desc:
            continue

        # Amount
        amt_val = dsh.cell(r, amt_col).value if amt_col < dsh.ncols else None
        amount = _to_amount(amt_val)
        if amount is None:
            continue

        row_no += 1
        out.append(GenericTransaction(
            row_number=row_no,
            date=voucher_date,
            description=desc,
            reference=None,
            debit=amount,
            credit=Decimal("0"),
            amount=amount,
            balance=None,
            bank_profile="PCF_SYSTEM",
        ))

    if not out:
        raise PCFSystemParseError(
            "No debit transaction rows found in the PCF System Detail sheet. "
            "Expected rows with Dr/Cr = 1.0."
        )

    return out
