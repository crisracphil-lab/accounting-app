"""
BIR tax monitoring service.

Static catalog of forms RAC PHIL files:
    1601-C    Monthly  Withholding tax on compensation        (acct 2130)
    0619-E    Monthly  Expanded Withholding Tax remittance    (acct 2110)
    1601-EQ   Quarterly Expanded Withholding Tax return       (acct 2110)
    0619-F    Monthly  Final Withholding Tax remittance       (acct 2140)
    1601-FQ   Quarterly Final Withholding Tax return          (acct 2140)
    2550Q     Quarterly VAT return                            (accts 1210 Input + 2120 Output)
    1702Q     Quarterly Corporate income tax                  (computed: revenues - expenses)
    1702      Annual   Corporate income tax                   (computed)
    2553Q     Quarterly (user-defined; placeholder until accounts mapped)

For each form + period, computes:
  - opening balance of relevant accounts
  - debits + credits posted in period
  - closing balance (= filing amount for the form)
  - due date based on filing frequency
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional


# ---- Form catalog -----------------------------------------------------------

@dataclass(frozen=True)
class TaxForm:
    code: str
    name: str
    frequency: str            # 'monthly' | 'quarterly' | 'annual'
    due_day: int              # day-of-period the form is due (e.g., 10 for 1601-C)
    due_offset_months: int    # months after period end (1 = next month)
    account_codes: tuple      # COA codes summarized for this form
    description: str = ""


BIR_FORMS = (
    TaxForm("1601-C",  "Monthly Remittance Return of Income Taxes Withheld on Compensation",
            "monthly",   10, 1, ("2130",),
            "WTax on compensation - employees' withheld portion of salary"),
    TaxForm("0619-E",  "Monthly Remittance Form of Creditable Income Taxes Withheld - Expanded",
            "monthly",   10, 1, ("2110",),
            "Monthly remittance of EWT (months 1 and 2 of a quarter)"),
    TaxForm("1601-EQ", "Quarterly Remittance Return of Creditable Income Taxes Withheld - Expanded",
            "quarterly", 30, 1, ("2110",),
            "Quarterly EWT return reconciling 0619-E remittances"),
    TaxForm("0619-F",  "Monthly Remittance Form of Final Income Taxes Withheld",
            "monthly",   10, 1, ("2140",),
            "Monthly remittance of Final WT (months 1 and 2 of a quarter)"),
    TaxForm("1601-FQ", "Quarterly Remittance Return of Final Income Taxes Withheld",
            "quarterly", 30, 1, ("2140",),
            "Quarterly Final WT return"),
    TaxForm("2550Q",   "Quarterly Value-Added Tax Return",
            "quarterly", 25, 1, ("1210", "2120"),
            "VAT Payable = Output VAT - Input VAT"),
    TaxForm("1702Q",   "Quarterly Income Tax Return for Corporations",
            "quarterly", 60, 0, (),
            "Corp income tax (computed from revenues - expenses for the quarter)"),
    TaxForm("1702",    "Annual Income Tax Return for Corporations",
            "annual",    15, 4, (),
            "Annual corp income tax, due Apr 15 of following year"),
    TaxForm("2553Q",   "2553Q (per company convention)",
            "quarterly", 30, 1, (),
            "User-defined quarterly form - account mapping to be configured"),
)


def get_form(code: str) -> Optional[TaxForm]:
    for f in BIR_FORMS:
        if f.code == code:
            return f
    return None


# Quarterly forms that are NOT filed for Q4.
# 1702Q covers only the first three quarters; the annual 1702RT/1702MX
# covers the full year and is due April 15 of the following year.
_QUARTERLY_FORM_QUARTERS: dict[str, tuple[int, ...]] = {
    "1702Q": (1, 2, 3),
}


def valid_quarters(form: TaxForm) -> tuple[int, ...]:
    """Return the valid period indices for a quarterly form (default 1–4)."""
    if form.frequency == "quarterly":
        return _QUARTERLY_FORM_QUARTERS.get(form.code, (1, 2, 3, 4))
    return (1,)


# ---- Period helpers --------------------------------------------------------

def period_dates(form: TaxForm, year: int, period_index: int) -> tuple[date, date]:
    """
    period_index:
      monthly:    1..12
      quarterly:  1..4
      annual:     1
    """
    if form.frequency == "monthly":
        return (date(year, period_index, 1),
                date(year, period_index, calendar.monthrange(year, period_index)[1]))
    if form.frequency == "quarterly":
        start_month = (period_index - 1) * 3 + 1
        end_month   = start_month + 2
        return (date(year, start_month, 1),
                date(year, end_month, calendar.monthrange(year, end_month)[1]))
    if form.frequency == "annual":
        return (date(year, 1, 1), date(year, 12, 31))
    raise ValueError(f"Unknown frequency {form.frequency!r}")


def due_date(form: TaxForm, year: int, period_index: int) -> date:
    # 1702Q: due exactly 60 calendar days after the quarter-end date,
    # then pushed to the next Monday if that day lands on a weekend.
    if form.code == "1702Q":
        _, quarter_end = period_dates(form, year, period_index)
        due = quarter_end + timedelta(days=60)
        if due.weekday() == 5:    # Saturday → Monday
            due += timedelta(days=2)
        elif due.weekday() == 6:  # Sunday → Monday
            due += timedelta(days=1)
        return due

    _, end = period_dates(form, year, period_index)
    # Add offset months
    m = end.month + form.due_offset_months
    y = end.year
    while m > 12:
        m -= 12
        y += 1
    last_day = calendar.monthrange(y, m)[1]
    return date(y, m, min(form.due_day, last_day))


# ---- Balance computation ---------------------------------------------------

@dataclass
class AccountBalance:
    account_code: str
    account_name: str
    opening_balance: Decimal       # signed (positive = Dr balance, negative = Cr)
    period_debit: Decimal
    period_credit: Decimal
    closing_balance: Decimal
    natural_side: str              # 'debit' | 'credit'

    @property
    def net_movement(self) -> Decimal:
        """Signed change during the period (Dr increases assets, Cr increases liabilities)."""
        return self.period_debit - self.period_credit


@dataclass
class FormSummary:
    form: TaxForm
    year: int
    period_index: int
    period_start: date
    period_end: date
    due: date
    accounts: List[AccountBalance] = field(default_factory=list)
    estimated_filing_amount: Optional[Decimal] = None
    notes: List[str] = field(default_factory=list)


def _balance_for_account(conn, account_code: str, period_start: date, period_end: date) -> AccountBalance:
    """Compute opening / period activity / closing for one account."""
    row = conn.execute(
        "SELECT id, name, type FROM chart_of_accounts WHERE code = ?",
        (account_code,)).fetchone()
    if row is None:
        # Account doesn't exist yet - return zeros so the UI still renders
        return AccountBalance(account_code=account_code, account_name=f"({account_code} not in COA)",
                              opening_balance=Decimal("0"),
                              period_debit=Decimal("0"), period_credit=Decimal("0"),
                              closing_balance=Decimal("0"), natural_side="debit")
    acct_id = row["id"]
    natural = "debit" if row["type"] in ("asset", "expense") else "credit"

    # Opening: sum all postings BEFORE period_start
    opening = conn.execute(
        "SELECT COALESCE(SUM(CAST(l.debit AS REAL)),0) - COALESCE(SUM(CAST(l.credit AS REAL)),0) AS bal "
        "FROM journal_entry_lines l "
        "JOIN journal_entries je ON je.id = l.journal_entry_id "
        "WHERE l.account_id = ? AND je.entry_date < ? AND je.status IN ('approved','posted','draft')",
        (acct_id, period_start.isoformat())).fetchone()
    period = conn.execute(
        "SELECT COALESCE(SUM(CAST(l.debit AS REAL)),0) AS dr, "
        "       COALESCE(SUM(CAST(l.credit AS REAL)),0) AS cr "
        "FROM journal_entry_lines l "
        "JOIN journal_entries je ON je.id = l.journal_entry_id "
        "WHERE l.account_id = ? AND je.entry_date BETWEEN ? AND ? "
        "AND je.status IN ('approved','posted','draft')",
        (acct_id, period_start.isoformat(), period_end.isoformat())).fetchone()
    opening_d = Decimal(str(opening["bal"] or 0))
    period_dr = Decimal(str(period["dr"] or 0))
    period_cr = Decimal(str(period["cr"] or 0))
    closing_d = opening_d + period_dr - period_cr
    return AccountBalance(account_code=account_code, account_name=row["name"],
                          opening_balance=opening_d,
                          period_debit=period_dr, period_credit=period_cr,
                          closing_balance=closing_d, natural_side=natural)


def summarize_form(conn, form: TaxForm, year: int, period_index: int) -> FormSummary:
    p_start, p_end = period_dates(form, year, period_index)
    due = due_date(form, year, period_index)

    summary = FormSummary(form=form, year=year, period_index=period_index,
                          period_start=p_start, period_end=p_end, due=due)
    for code in form.account_codes:
        summary.accounts.append(_balance_for_account(conn, code, p_start, p_end))

    # Form-specific filing amount
    if form.code == "2550Q":
        # VAT Payable = Output VAT (2120 closing) - Input VAT (1210 closing)
        in_vat  = next((a.closing_balance for a in summary.accounts if a.account_code == "1210"), Decimal("0"))
        out_vat = next((a.closing_balance for a in summary.accounts if a.account_code == "2120"), Decimal("0"))
        # 2120 is a liability so credit balance is positive owed; we stored as Dr-Cr so flip
        out_owed = -out_vat
        in_carried = in_vat   # Dr balance (asset)
        summary.estimated_filing_amount = max(out_owed - in_carried, Decimal("0"))
        summary.notes.append(
            f"VAT Payable = Output VAT (closing Cr {out_owed:,.2f}) "
            f"- Input VAT (closing Dr {in_carried:,.2f}) = {summary.estimated_filing_amount:,.2f}")
    elif form.code in ("1601-C", "0619-E", "1601-EQ", "0619-F", "1601-FQ"):
        # Filing amount = closing credit balance of the WTax payable account
        if summary.accounts:
            a = summary.accounts[0]
            summary.estimated_filing_amount = max(-a.closing_balance, Decimal("0"))
            summary.notes.append(
                f"Filing amount = closing credit balance of {a.account_code} {a.account_name} "
                f"= {summary.estimated_filing_amount:,.2f}")
    elif form.code in ("1702Q", "1702"):
        # Approximate: revenues - expenses for period (very rough, doesn't apply
        # tax rate or include allowable deductions / add-backs)
        rev = conn.execute(
            "SELECT COALESCE(SUM(CAST(l.credit AS REAL)),0) - COALESCE(SUM(CAST(l.debit AS REAL)),0) AS n "
            "FROM journal_entry_lines l "
            "JOIN chart_of_accounts a ON a.id = l.account_id "
            "JOIN journal_entries je ON je.id = l.journal_entry_id "
            "WHERE a.type = 'income' AND je.entry_date BETWEEN ? AND ? "
            "AND je.status IN ('approved','posted','draft')",
            (p_start.isoformat(), p_end.isoformat())).fetchone()["n"]
        exp = conn.execute(
            "SELECT COALESCE(SUM(CAST(l.debit AS REAL)),0) - COALESCE(SUM(CAST(l.credit AS REAL)),0) AS n "
            "FROM journal_entry_lines l "
            "JOIN chart_of_accounts a ON a.id = l.account_id "
            "JOIN journal_entries je ON je.id = l.journal_entry_id "
            "WHERE a.type = 'expense' AND je.entry_date BETWEEN ? AND ? "
            "AND je.status IN ('approved','posted','draft')",
            (p_start.isoformat(), p_end.isoformat())).fetchone()["n"]
        net = Decimal(str(rev)) - Decimal(str(exp))
        # Apply standard 25% CIT (2024+ corporate rate); user can override
        summary.estimated_filing_amount = max(net * Decimal("0.25"), Decimal("0"))
        summary.notes.append(
            f"Approx CIT base = revenues {rev:,.2f} - expenses {exp:,.2f} = {net:,.2f}; "
            f"x 25% (regular CIT) = {summary.estimated_filing_amount:,.2f}. "
            f"Excludes deductions/add-backs - use as starting point only.")
    elif form.code == "2553Q":
        summary.notes.append(
            "Form 2553Q: account mapping not configured. "
            "Edit BIR_FORMS in app/services/tax_summary.py to add account_codes.")
    return summary
