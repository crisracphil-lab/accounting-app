"""System subsidiary ledger parser (.xls format)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

try:
    import xlrd
except ImportError as e:
    raise ImportError("xlrd is required for .xls files. pip install xlrd") from e


class SystemLedgerParseError(Exception):
    pass


@dataclass
class LedgerEntry:
    account_code: str
    account_title: str
    voucher_date: Optional[date]
    voucher_no: Optional[str]
    department_no: Optional[str]
    department_name: Optional[str]
    description: Optional[str]
    remark: Optional[str]
    debit: Decimal
    credit: Decimal


@dataclass
class AccountTotals:
    account_code: str
    account_title: str
    total_debit: Decimal = Decimal("0")
    total_credit: Decimal = Decimal("0")
    rows: int = 0


@dataclass
class LedgerSummary:
    period_start: Optional[date]
    period_end: Optional[date]
    accounts: Dict[str, AccountTotals] = field(default_factory=dict)
    rows_total: int = 0


def _coerce_date(v) -> Optional[date]:
    if v is None or v == "" or (isinstance(v, str) and v.strip() == ""):
        return None
    if isinstance(v, (int, float)) and v > 0:
        try:
            dt = xlrd.xldate.xldate_as_datetime(v, 0)
            return dt.date()
        except Exception:
            return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        s = v.strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
    return None


def _to_decimal(v) -> Decimal:
    if v is None:
        return Decimal("0")
    s = str(v).strip()
    if s == "":
        return Decimal("0")
    try:
        return Decimal(s.replace(",", ""))
    except Exception:
        return Decimal("0")  # non-numeric stripe row, ignore


def _to_str(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def parse_system_ledger(file_path,
                        period_start: Optional[date] = None,
                        period_end: Optional[date] = None,
                        account_filter: Optional[str] = None) -> LedgerSummary:
    """
    Parse a 'GGR PER SYSTEM.xls' subsidiary ledger.

    Layout:
        Row 1-3: header (Created on, Period, etc.)
        Row 4: column header [A/C No., Acct Title, A/C Alias, Voucher Date,
               Voucher No., Department No., Department Name, Description,
               Remark, Debit, Credit, Balance]
        Row 5+: alternating account-section rows (col 1 = account code) and
                detail rows (col 1 blank, col 4-onward = entry data).
    """
    path = Path(file_path)
    if not path.exists():
        raise SystemLedgerParseError(f"File not found: {path}")
    if path.suffix.lower() != ".xls":
        raise SystemLedgerParseError(f"Expected .xls, got {path.suffix}")

    wb = xlrd.open_workbook(path)
    if wb.nsheets == 0:
        raise SystemLedgerParseError("Workbook has no sheets")
    ws = wb.sheet_by_index(0)

    # Find data start row by locating header 'A/C No.'
    header_row = None
    for r in range(min(10, ws.nrows)):
        v = ws.cell(r, 0).value
        if isinstance(v, str) and "A/C No" in v:
            header_row = r
            break
    if header_row is None:
        raise SystemLedgerParseError(
            "Could not find 'A/C No.' header row in the first 10 rows.")

    summary = LedgerSummary(period_start=period_start, period_end=period_end)

    current_code: Optional[str] = None
    current_title: Optional[str] = None
    for r in range(header_row + 1, ws.nrows):
        ac_raw = ws.cell(r, 0).value
        title_raw = ws.cell(r, 1).value

        # Section header row: numeric A/C No.
        if isinstance(ac_raw, (int, float)) and ac_raw > 0:
            current_code = f"{int(ac_raw)}"
            current_title = _to_str(title_raw) or current_code
            summary.accounts.setdefault(
                current_code,
                AccountTotals(account_code=current_code,
                              account_title=current_title))
            continue

        if current_code is None:
            continue
        if account_filter and current_code != account_filter:
            continue

        v_date = _coerce_date(ws.cell(r, 3).value)
        if period_start and v_date and v_date < period_start:
            continue
        if period_end and v_date and v_date > period_end:
            continue

        debit = _to_decimal(ws.cell(r, 9).value)
        credit = _to_decimal(ws.cell(r, 10).value)
        if debit == 0 and credit == 0:
            continue

        acct = summary.accounts[current_code]
        acct.total_debit += debit
        acct.total_credit += credit
        acct.rows += 1
        summary.rows_total += 1

    return summary
