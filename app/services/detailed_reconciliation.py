"""
Detailed GGR reconciliation engine.

Two-directional reconciliation:

DIRECTION 1 — GGR → System (for each GGR row, find a matching system entry):
  1. Same week, correct side, amount within tolerance  → MATCHED
  2. Same week, wrong side,   amount within tolerance  → WRONG SIDE
  3. No match found                                    → MISSING ENTRY IN SYSTEM
     (nearest_system_amount shown as reference — entry NOT consumed)

DIRECTION 2 — System → GGR (for each unconsumed system entry, check GGR):
  Any system entry left unconsumed after Direction 1                  → IN SYSTEM ONLY
  (amount exists in the system ledger but no corresponding GGR row)

Each system entry is consumed at most once across both directions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from app.parsers.combined_ggr_xls import (
    CombinedFile, GGRWeekRow, SystemEntry,
)

# Account-type tokens
ACCT_GGR    = "GGR"
ACCT_PAGCOR = "PAGCOR Share"
ACCT_AUDIT  = "Audit Fee"
ACCT_OP     = "Operator Rev"

# Statuses
ST_MATCHED       = "MATCHED"
ST_WRONG         = "WRONG SIDE"
ST_AMOUNT        = "AMOUNT DISCREPANCY"
ST_MISSING       = "MISSING ENTRY IN SYSTEM"
ST_SYSTEM_ONLY   = "IN SYSTEM ONLY"        # system entry with no GGR counterpart

_DEFAULT_TOLERANCE = Decimal("0.05")


@dataclass
class DetailedRow:
    week_label: str
    outlet_code: str
    outlet_name: str
    account_type: str
    ggr_amount: Decimal           # signed GGR value (0 for ST_SYSTEM_ONLY rows)
    expected_side: str            # 'DEBIT' | 'CREDIT' | '' for system-only
    expected_amount: Decimal      # abs(ggr_amount); 0 for system-only
    system_debit: Optional[Decimal]
    system_credit: Optional[Decimal]
    actual_side: str
    discrepancy_type: str
    amount_difference: Optional[Decimal] = None
    nearest_system_amount: Optional[Decimal] = None


@dataclass
class DetailedSummary:
    period_label: Optional[str]
    total_records: int = 0
    matched_records: int = 0
    discrepancy_count: int = 0
    by_week_missing:     Dict[str, int] = field(default_factory=dict)
    by_week_wrong:       Dict[str, int] = field(default_factory=dict)
    by_week_amount:      Dict[str, int] = field(default_factory=dict)
    by_week_system_only: Dict[str, int] = field(default_factory=dict)
    by_week_total:       Dict[str, int] = field(default_factory=dict)
    findings: List[str] = field(default_factory=list)


@dataclass
class DetailedResult:
    summary: DetailedSummary
    rows: List[DetailedRow] = field(default_factory=list)

    @property
    def discrepancies(self) -> List[DetailedRow]:
        return [r for r in self.rows if r.discrepancy_type != ST_MATCHED]


def _expected_for_metric(ggr_row: GGRWeekRow, account_type: str,
                         signed_amount: Decimal):
    if signed_amount == 0:
        return None
    side = ("CREDIT" if signed_amount > 0 else "DEBIT") if account_type == ACCT_GGR \
           else ("DEBIT" if signed_amount > 0 else "CREDIT")
    return side, abs(signed_amount)


def reconcile_detailed(combined: CombinedFile,
                       account_filter: Optional[str] = None,
                       tolerance: Decimal = _DEFAULT_TOLERANCE) -> DetailedResult:
    """
    Two-directional GGR reconciliation.
    account_filter: restrict system pool to this account_code (None = all).
    """
    # Build week-pool: { week_label: [(idx, entry), ...] }
    week_pool: Dict[str, List[Tuple[int, SystemEntry]]] = {}
    for idx, e in enumerate(combined.system_entries):
        if account_filter and e.account_code != account_filter:
            continue
        if e.week_label is None:
            continue
        week_pool.setdefault(e.week_label, []).append((idx, e))

    consumed: set[int] = set()
    rows: List[DetailedRow] = []

    metrics_in_order = (
        (ACCT_GGR,    "ggr"),
        (ACCT_PAGCOR, "pagcor_share"),
        (ACCT_AUDIT,  "audit_fee"),
        (ACCT_OP,     "operator"),
    )

    # ── Direction 1: GGR → System ─────────────────────────────────────────────
    for ggr in combined.ggr_rows:
        for label, attr in metrics_in_order:
            signed = getattr(ggr, attr)
            exp = _expected_for_metric(ggr, label, signed)
            if exp is None:
                continue
            expected_side, expected_amt = exp
            other_side = "CREDIT" if expected_side == "DEBIT" else "DEBIT"

            row = DetailedRow(
                week_label=ggr.week_label,
                outlet_code=ggr.outlet_code,
                outlet_name=ggr.outlet_name,
                account_type=label,
                ggr_amount=signed,
                expected_side=expected_side,
                expected_amount=expected_amt,
                system_debit=None,
                system_credit=None,
                actual_side="NOT FOUND",
                discrepancy_type=ST_MISSING,
            )

            pool = week_pool.get(ggr.week_label, [])

            # Step 1: correct side, within tolerance → MATCHED
            match_idx = _find_match(pool, expected_amt, expected_side, consumed, tolerance)
            if match_idx is not None:
                _apply_match(row, combined.system_entries[match_idx], expected_side, ST_MATCHED)
                consumed.add(match_idx)

            else:
                # Step 2: wrong side, within tolerance → WRONG SIDE
                match_idx = _find_match(pool, expected_amt, other_side, consumed, tolerance)
                if match_idx is not None:
                    _apply_match(row, combined.system_entries[match_idx], other_side, ST_WRONG)
                    consumed.add(match_idx)

                else:
                    # Step 3: MISSING — non-consuming nearest hint
                    hint_idx, hint_amt = _find_closest(pool, expected_side, expected_amt, consumed)
                    if hint_idx is None:
                        hint_idx, hint_amt = _find_closest(pool, other_side, expected_amt, consumed)
                    if hint_amt is not None:
                        row.nearest_system_amount = hint_amt
                        row.amount_difference = hint_amt - expected_amt

            rows.append(row)

    # ── Direction 2: System → GGR (unconsumed system entries) ─────────────────
    for week_label, pool in week_pool.items():
        for idx, e in pool:
            if idx in consumed:
                continue
            # This system entry was never matched to any GGR row
            side = "DEBIT" if e.debit > 0 else "CREDIT"
            amt  = e.debit if e.debit > 0 else e.credit
            rows.append(DetailedRow(
                week_label=week_label,
                outlet_code="",
                outlet_name=f"[System: {e.account_title}] {e.description[:60] if e.description else ''}",
                account_type=e.account_code,
                ggr_amount=Decimal("0"),
                expected_side="",
                expected_amount=Decimal("0"),
                system_debit=e.debit  if e.debit  > 0 else None,
                system_credit=e.credit if e.credit > 0 else None,
                actual_side=side,
                discrepancy_type=ST_SYSTEM_ONLY,
                amount_difference=amt,   # full system amount = the discrepancy
            ))

    summary = _build_summary(combined.period_label, rows)
    return DetailedResult(summary=summary, rows=rows)


def _apply_match(row: DetailedRow, e: SystemEntry,
                 actual_side: str, status: str) -> None:
    row.system_debit  = e.debit  if e.debit  > 0 else None
    row.system_credit = e.credit if e.credit > 0 else None
    row.actual_side = actual_side
    row.discrepancy_type = status
    system_amt = e.debit if actual_side == "DEBIT" else e.credit
    if row.expected_amount and system_amt:
        diff = system_amt - row.expected_amount
        if diff != 0:
            row.amount_difference = diff


def _find_match(pool, amount: Decimal, side: str,
                consumed: set, tolerance: Decimal) -> Optional[int]:
    for idx, e in pool:
        if idx in consumed:
            continue
        v = e.debit if side == "DEBIT" else e.credit
        if v == 0:
            continue
        if abs(v - amount) <= tolerance:
            return idx
    return None


def _find_closest(pool, side: str, expected_amt: Decimal,
                  consumed: set) -> Tuple[Optional[int], Optional[Decimal]]:
    best_idx = None
    best_amt = None
    best_diff = None
    for idx, e in pool:
        if idx in consumed:
            continue
        v = e.debit if side == "DEBIT" else e.credit
        if v == 0:
            continue
        diff = abs(v - expected_amt)
        if best_diff is None or diff < best_diff:
            best_idx  = idx
            best_amt  = v
            best_diff = diff
    return best_idx, best_amt


def _build_summary(period_label: Optional[str],
                   rows: List[DetailedRow]) -> DetailedSummary:
    s = DetailedSummary(period_label=period_label)
    s.total_records     = len(rows)
    s.matched_records   = sum(1 for r in rows if r.discrepancy_type == ST_MATCHED)
    s.discrepancy_count = s.total_records - s.matched_records

    for r in rows:
        wl = r.week_label or "(no week)"
        s.by_week_total[wl] = s.by_week_total.get(wl, 0) + 1
        if r.discrepancy_type == ST_MISSING:
            s.by_week_missing[wl]     = s.by_week_missing.get(wl, 0) + 1
        elif r.discrepancy_type == ST_WRONG:
            s.by_week_wrong[wl]       = s.by_week_wrong.get(wl, 0) + 1
        elif r.discrepancy_type == ST_AMOUNT:
            s.by_week_amount[wl]      = s.by_week_amount.get(wl, 0) + 1
        elif r.discrepancy_type == ST_SYSTEM_ONLY:
            s.by_week_system_only[wl] = s.by_week_system_only.get(wl, 0) + 1

    # Key findings
    bad_weeks = sorted(s.by_week_missing.items(), key=lambda kv: -kv[1])
    if bad_weeks and bad_weeks[0][1] >= 5:
        s.findings.append(
            f"{bad_weeks[0][0].upper()}: {bad_weeks[0][1]} GGR entries have no system match."
        )
    sys_only_total = sum(s.by_week_system_only.values())
    if sys_only_total > 0:
        s.findings.append(
            f"{sys_only_total} system entr{'y' if sys_only_total == 1 else 'ies'} "
            f"found with no corresponding GGR row — possible erroneous posting."
        )
    for r in [r for r in rows if r.discrepancy_type == ST_WRONG][:3]:
        s.findings.append(
            f"{r.week_label} — {r.outlet_code} ({r.outlet_name}) {r.account_type}: "
            f"posted to {r.actual_side}, expected {r.expected_side}."
        )
    return s
