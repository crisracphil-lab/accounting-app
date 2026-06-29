"""
Bank Reconciliation — two-directional amount matching + standard summary.

MATCHING RULE (strict mirror — no side crossover):
    System DEBIT  ↔  Bank CREDIT   (cash receipt: debit cash = bank credits account)
    System CREDIT ↔  Bank DEBIT    (cash payment: credit cash = bank debits account)

IMPORTANT: System files often contain full journal entries with BOTH debit and
credit sides (e.g., DR Cash / CR Revenue). To prevent the credit side
(revenue/payable account) from incorrectly consuming bank credit entries,
matching is done in two isolated pools:
    Pool A:  sys_debits   matched against  bank_credits
    Pool B:  sys_credits  matched against  bank_debits

Entries with no debit or credit (amount-only column) go into a third pool
matched by absolute amount regardless of side.

SUMMARY
    Bank:  bank_balance + deposits_in_transit − outstanding_checks  = adjusted_bank
    Book:  book_balance + bank_credits_not_in_books − bank_charges  = adjusted_book
    Check: adjusted_bank == adjusted_book
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional, Tuple

from app.parsers.bank_statement_generic import GenericTransaction

_DEFAULT_TOLERANCE = Decimal("0")
_ZERO = Decimal("0")

ST_MATCHED       = "MATCHED"
ST_MATCHED_DIFF  = "MATCHED (DIFFERENCE)"
ST_NOT_IN_BANK   = "NOT IN BANK"
ST_NOT_IN_SYSTEM = "NOT IN SYSTEM"


@dataclass
class ReconRow:
    direction: str
    status: str
    sys_date: Optional[str]
    sys_ref: Optional[str]
    sys_description: str
    sys_debit: Optional[Decimal]
    sys_credit: Optional[Decimal]
    sys_amount: Optional[Decimal]
    bank_date: Optional[str]
    bank_ref: Optional[str]
    bank_description: str
    bank_debit: Optional[Decimal]
    bank_credit: Optional[Decimal]
    bank_amount: Optional[Decimal]
    difference: Optional[Decimal] = None
    match_method: str = ""


@dataclass
class ReconciliationSummary:
    company_name: str = ""
    bank_account: str = ""
    statement_date: str = ""
    bank_balance: Decimal = _ZERO
    deposits_in_transit: Decimal = _ZERO
    outstanding_checks: Decimal = _ZERO
    adjusted_bank_balance: Decimal = _ZERO
    book_balance: Decimal = _ZERO
    bank_credits_not_in_books: Decimal = _ZERO
    bank_charges_not_in_books: Decimal = _ZERO
    adjusted_book_balance: Decimal = _ZERO
    reconciled: bool = False
    balance_difference: Decimal = _ZERO
    deposits_in_transit_count: int = 0
    outstanding_checks_count: int = 0
    bank_credits_count: int = 0
    bank_charges_count: int = 0


@dataclass
class BankSystemResult:
    system_filename: str
    bank_filename: str
    summary: ReconciliationSummary = field(default_factory=ReconciliationSummary)
    rows: List[ReconRow] = field(default_factory=list)

    @property
    def sys_to_bank(self):
        return [r for r in self.rows if r.direction == "SYS→BANK"]

    @property
    def bank_to_sys(self):
        return [r for r in self.rows if r.direction == "BANK→SYS"]

    @property
    def matched_count(self):
        return sum(1 for r in self.rows if r.status == ST_MATCHED)

    @property
    def discrepancy_count(self):
        return sum(1 for r in self.rows if r.status != ST_MATCHED)

    @property
    def not_in_bank_count(self):
        return sum(1 for r in self.rows if r.status == ST_NOT_IN_BANK)

    @property
    def not_in_system_count(self):
        return sum(1 for r in self.rows if r.status == ST_NOT_IN_SYSTEM)


def _pool_match(
    entries: List[Tuple[int, Decimal, GenericTransaction]],  # (original_idx, amount, txn)
    candidates: List[Tuple[int, Decimal, GenericTransaction]],
    tolerance: Decimal,
) -> Tuple[List[Tuple[int, int, Decimal]], set]:
    """
    Greedy closest-amount matching between two pools.
    Returns (matched_pairs [(entry_idx, cand_idx, diff)], used_cand_indices).
    Each candidate consumed at most once.
    """
    used: set[int] = set()
    pairs = []
    for e_idx, e_amt, _ in entries:
        best_ci, best_diff = None, None
        for c_i, (c_orig_idx, c_amt, _) in enumerate(candidates):
            if c_i in used:
                continue
            diff = abs(c_amt - e_amt)
            if diff <= tolerance and (best_diff is None or diff < best_diff):
                best_ci, best_diff = c_i, diff
        if best_ci is not None:
            pairs.append((e_idx, best_ci, best_diff))
            used.add(best_ci)
    return pairs, used


def reconcile_bank_system(
    system_txns: List[GenericTransaction],
    bank_txns: List[GenericTransaction],
    system_filename: str = "Per System",
    bank_filename: str = "Per Bank",
    tolerance: Decimal = _DEFAULT_TOLERANCE,
    bank_balance: Decimal = _ZERO,
    book_balance: Decimal = _ZERO,
    company_name: str = "",
    bank_account: str = "",
    statement_date: str = "",
) -> BankSystemResult:
    result = BankSystemResult(system_filename=system_filename, bank_filename=bank_filename)

    # ── Separate into pools by side ───────────────────────────────────────────
    # System pools
    sys_debits:  List[Tuple[int, Decimal, GenericTransaction]] = []   # (idx, amount, txn)
    sys_credits: List[Tuple[int, Decimal, GenericTransaction]] = []
    sys_amtonly: List[Tuple[int, Decimal, GenericTransaction]] = []   # no clear side

    for i, s in enumerate(system_txns):
        d = s.debit  or _ZERO
        c = s.credit or _ZERO
        a = s.amount or _ZERO
        if d > 0 and c == _ZERO:
            sys_debits.append((i, d, s))
        elif c > 0 and d == _ZERO:
            sys_credits.append((i, c, s))
        elif d > 0 or c > 0:
            # Both columns populated (unusual) — take the larger
            amt = max(d, c)
            sys_amtonly.append((i, amt, s))
        elif a > 0:
            # Amount-only entry (no debit/credit columns) — fallback pool
            sys_amtonly.append((i, a, s))

    # Bank pools
    bank_credits: List[Tuple[int, Decimal, GenericTransaction]] = []
    bank_debits:  List[Tuple[int, Decimal, GenericTransaction]] = []
    bank_amtonly: List[Tuple[int, Decimal, GenericTransaction]] = []

    for i, b in enumerate(bank_txns):
        d = b.debit  or _ZERO
        c = b.credit or _ZERO
        a = b.amount or _ZERO
        if c > 0 and d == _ZERO:
            bank_credits.append((i, c, b))
        elif d > 0 and c == _ZERO:
            bank_debits.append((i, d, b))
        elif d > 0 or c > 0:
            amt = max(d, c)
            bank_amtonly.append((i, amt, b))
        elif a > 0:
            # Amount-only bank entry (no debit/credit columns)
            bank_amtonly.append((i, a, b))

    # ── Pool A: sys_debits ↔ bank_credits (strict mirror) ────────────────────
    pairs_a, used_bc = _pool_match(sys_debits, bank_credits, tolerance)

    # ── Pool B: sys_credits ↔ bank_debits (strict mirror) ────────────────────
    pairs_b, used_bd = _pool_match(sys_credits, bank_debits, tolerance)

    # ── Pool C: fallback — amount-only for ambiguous entries ─────────────────
    # Remaining sys_amtonly vs remaining bank_amtonly + any unclaimed
    remaining_bank = [(i, amt, b) for (i, amt, b) in bank_amtonly]
    # Also add unclaimed bank_credits and bank_debits
    remaining_bank += [(i, amt, b) for (i, amt, b) in bank_credits if bank_credits.index((i, amt, b)) not in used_bc] \
        if False else []  # handled below via used sets

    # Build sets of consumed bank indices
    consumed_bank: set[int] = set()
    for _, ci, _ in pairs_a:
        consumed_bank.add(bank_credits[ci][0])
    for _, ci, _ in pairs_b:
        consumed_bank.add(bank_debits[ci][0])

    # Fallback pool: all bank entries not yet consumed
    def _bank_amt(b: GenericTransaction) -> Decimal:
        d = b.debit  or _ZERO
        c = b.credit or _ZERO
        a = b.amount or _ZERO
        return max(d, c) if (d > 0 or c > 0) else a

    fallback_bank = [(i, _bank_amt(b), b)
                     for i, b in enumerate(bank_txns) if i not in consumed_bank]
    pairs_c, used_fb = _pool_match(sys_amtonly, fallback_bank, tolerance)

    # Map fallback pool index back to original bank index
    consumed_bank.update(fallback_bank[fi][0] for _, fi, _ in pairs_c)

    # ── Build result rows ─────────────────────────────────────────────────────
    # Track which system entries were matched
    matched_sys: dict[int, Tuple[int, Decimal, str]] = {}  # sys_idx -> (bank_idx, diff, method)

    for si, ci, diff in pairs_a:
        s_orig = si  # si is already the original system_txns index
        b_orig = bank_credits[ci][0]
        matched_sys[s_orig] = (b_orig, diff, "mirror")

    for si, ci, diff in pairs_b:
        s_orig = si  # si is already the original system_txns index
        b_orig = bank_debits[ci][0]
        matched_sys[s_orig] = (b_orig, diff, "mirror")

    for si, fi, diff in pairs_c:
        s_orig = si  # si is already the original system_txns index
        b_orig = fallback_bank[fi][0]
        matched_sys[s_orig] = (b_orig, diff, "amount-only")

    # Direction 1: System → Bank
    for i, s in enumerate(system_txns):
        d = s.debit  or _ZERO
        c = s.credit or _ZERO
        sys_amt = max(d, c) if (d > 0 and c > 0) else (d if d > 0 else c)
        if sys_amt == _ZERO:
            continue

        if i in matched_sys:
            b_idx, diff, method = matched_sys[i]
            b = bank_txns[b_idx]
            row_status = ST_MATCHED_DIFF if diff and diff > _ZERO else ST_MATCHED
            result.rows.append(ReconRow(
                direction="SYS→BANK", status=row_status,
                sys_date=s.date, sys_ref=s.reference, sys_description=s.description,
                sys_debit=d if d > 0 else None,
                sys_credit=c if c > 0 else None,
                sys_amount=s.amount,
                bank_date=b.date, bank_ref=b.reference, bank_description=b.description,
                bank_debit=(b.debit or _ZERO) if (b.debit or _ZERO) > 0 else None,
                bank_credit=(b.credit or _ZERO) if (b.credit or _ZERO) > 0 else None,
                bank_amount=b.amount,
                difference=diff if diff != _ZERO else None,
                match_method=method,
            ))
        else:
            result.rows.append(ReconRow(
                direction="SYS→BANK", status=ST_NOT_IN_BANK,
                sys_date=s.date, sys_ref=s.reference, sys_description=s.description,
                sys_debit=d if d > 0 else None,
                sys_credit=c if c > 0 else None,
                sys_amount=s.amount,
                bank_date=None, bank_ref=None, bank_description="",
                bank_debit=None, bank_credit=None, bank_amount=None,
                difference=sys_amt,
                match_method="",
            ))

    # Direction 2: Bank → System (unconsumed)
    for i, b in enumerate(bank_txns):
        if i in consumed_bank:
            continue
        bd = b.debit  or _ZERO
        bc = b.credit or _ZERO
        bank_abs = max(bd, bc) if (bd > 0 and bc > 0) else (bd if bd > 0 else bc)
        result.rows.append(ReconRow(
            direction="BANK→SYS", status=ST_NOT_IN_SYSTEM,
            sys_date=None, sys_ref=None, sys_description="",
            sys_debit=None, sys_credit=None, sys_amount=None,
            bank_date=b.date, bank_ref=b.reference, bank_description=b.description,
            bank_debit=bd if bd > 0 else None,
            bank_credit=bc if bc > 0 else None,
            bank_amount=b.amount,
            difference=bank_abs,
            match_method="",
        ))

    # ── Reconciliation summary ─────────────────────────────────────────────────
    sm = ReconciliationSummary(
        company_name=company_name, bank_account=bank_account,
        statement_date=statement_date,
        bank_balance=bank_balance, book_balance=book_balance,
    )

    dit_rows  = [r for r in result.rows if r.status == ST_NOT_IN_BANK  and (r.sys_debit  or _ZERO) > 0]
    oc_rows   = [r for r in result.rows if r.status == ST_NOT_IN_BANK  and (r.sys_credit or _ZERO) > 0]
    bc_rows   = [r for r in result.rows if r.status == ST_NOT_IN_SYSTEM and (r.bank_credit or _ZERO) > 0]
    bch_rows  = [r for r in result.rows if r.status == ST_NOT_IN_SYSTEM and (r.bank_debit  or _ZERO) > 0]

    sm.deposits_in_transit       = sum(r.sys_debit  for r in dit_rows if r.sys_debit)
    sm.deposits_in_transit_count = len(dit_rows)
    sm.outstanding_checks        = sum(r.sys_credit for r in oc_rows  if r.sys_credit)
    sm.outstanding_checks_count  = len(oc_rows)
    sm.bank_credits_not_in_books = sum(r.bank_credit for r in bc_rows  if r.bank_credit)
    sm.bank_credits_count        = len(bc_rows)
    sm.bank_charges_not_in_books = sum(r.bank_debit  for r in bch_rows if r.bank_debit)
    sm.bank_charges_count        = len(bch_rows)

    sm.adjusted_bank_balance = bank_balance + sm.deposits_in_transit - sm.outstanding_checks
    sm.adjusted_book_balance = book_balance + sm.bank_credits_not_in_books - sm.bank_charges_not_in_books

    diff_bal = abs(sm.adjusted_bank_balance - sm.adjusted_book_balance)
    sm.reconciled        = diff_bal <= tolerance
    sm.balance_difference = sm.adjusted_bank_balance - sm.adjusted_book_balance
    result.summary = sm
    return result
