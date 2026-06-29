"""
Rule-based transaction classifier.

Applies company-specific historical journal patterns first, then
classification_rules in priority order. Returns the first matching
Classification. For multi-account entries (VAT splits, etc.) use
classify_all() which delegates to match_all_historical_patterns().
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Optional

from app.services.historical_journal_learning import (
    match_historical_pattern,
    match_all_historical_patterns,
)


@dataclass
class Classification:
    target_account_id: int
    target_account_code: str
    target_account_name: str
    direction: str                # 'debit' | 'credit' | 'auto'
    rule_pattern: Optional[str]
    rule_description: Optional[str]
    confidence: float
    reason: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def classify(conn: sqlite3.Connection,
             *,
             description: str,
             remarks: Optional[str] = None,
             biller_name: Optional[str] = None,
             supplier_default_account_id: Optional[int] = None,
             company_id: int = 1) -> Classification:
    """
    Return a single best Classification (backward-compatible).
    Supplier default always wins. Then historical learning. Then rules.
    """
    if supplier_default_account_id is not None:
        row = conn.execute(
            "SELECT id, code, name FROM chart_of_accounts WHERE id = ?",
            (supplier_default_account_id,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"Supplier default_expense_account_id={supplier_default_account_id} "
                f"does not exist in chart_of_accounts"
            )
        return Classification(
            target_account_id=row["id"],
            target_account_code=row["code"],
            target_account_name=row["name"],
            direction="debit",
            rule_pattern=None,
            rule_description=None,
            confidence=0.95,
            reason=f"Supplier default account: {row['name']}",
        )

    haystacks = [s for s in (description, remarks, biller_name) if s]
    if not haystacks:
        return _suspense(conn, "No description, remarks, or biller name to classify on")

    hist = match_historical_pattern(conn, description=" ".join(haystacks), company_id=company_id)
    if hist:
        acct = conn.execute(
            "SELECT id, code, name FROM chart_of_accounts WHERE code = ?",
            (hist["account_code"],)).fetchone()
        if acct:
            return Classification(
                target_account_id=acct["id"],
                target_account_code=acct["code"],
                target_account_name=acct["name"],
                direction=hist["normal_side"] or "auto",
                rule_pattern=hist["keywords"],
                rule_description=f"Historical JE pattern from {hist['learned_from_filename'] or 'uploaded basis file'}",
                confidence=min(0.92, 0.60 + (hist["times_seen"] or 1) * 0.03),
                reason=f"Matched company historical journal pattern: {hist['keywords']}",
            )

    rules = conn.execute(
        """SELECT r.id, r.pattern, r.pattern_type, r.direction, r.priority,
                  r.description AS rule_description,
                  a.id AS account_id, a.code AS account_code, a.name AS account_name
           FROM classification_rules r
           JOIN chart_of_accounts a ON a.id = r.target_account_id
           WHERE r.is_active = 1
           ORDER BY r.priority ASC, r.id ASC"""
    ).fetchall()

    for rule in rules:
        if _rule_matches(rule, haystacks):
            return Classification(
                target_account_id=rule["account_id"],
                target_account_code=rule["account_code"],
                target_account_name=rule["account_name"],
                direction=rule["direction"],
                rule_pattern=rule["pattern"],
                rule_description=rule["rule_description"],
                confidence=0.70 if rule["account_code"] != "5999" else 0.30,
                reason=f"Rule matched (priority {rule['priority']}): {rule['pattern']!r}",
            )
    return _suspense(conn, "No classification rule matched")


def classify_all(conn: sqlite3.Connection,
                 *,
                 description: str,
                 remarks: Optional[str] = None,
                 biller_name: Optional[str] = None,
                 supplier_default_account_id: Optional[int] = None,
                 company_id: int = 1):
    """
    Return all matching patterns for multi-line JE generation (VAT splits etc.).

    Returns None when supplier default is used (no multi-line needed),
    a list of pattern dicts when historical templates match,
    or None when falling back to rules (caller uses classify() in that case).
    """
    # Supplier defaults always produce a simple 2-line entry
    if supplier_default_account_id is not None:
        return None

    haystacks = [s for s in (description, remarks, biller_name) if s]
    if not haystacks:
        return None

    patterns = match_all_historical_patterns(
        conn, description=" ".join(haystacks), company_id=company_id)

    # Only return multi-line when we actually have more than one account
    if patterns and len(patterns) >= 2:
        return patterns
    return None


def _rule_matches(rule, haystacks: list[str]) -> bool:
    pattern = rule["pattern"]
    ptype = rule["pattern_type"]
    if ptype == "keyword":
        needle = pattern.upper()
        return any(needle in s.upper() for s in haystacks)
    if ptype == "regex":
        try:
            rgx = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regex in classification_rules id={rule['id']}: "
                             f"{pattern!r} ({exc})") from exc
        return any(rgx.search(s) for s in haystacks)
    raise ValueError(f"Unknown pattern_type {ptype!r}")


def _suspense(conn: sqlite3.Connection, reason: str) -> Classification:
    row = conn.execute(
        "SELECT id, code, name FROM chart_of_accounts WHERE code = '5999'"
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "Suspense account (code 5999) is missing from chart_of_accounts. "
            "Database seed is incomplete."
        )
    return Classification(
        target_account_id=row["id"],
        target_account_code=row["code"],
        target_account_name=row["name"],
        direction="auto",
        rule_pattern=None,
        rule_description=None,
        confidence=0.0,
        reason=reason,
    )
