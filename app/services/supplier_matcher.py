"""Supplier matcher."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional


@dataclass
class SupplierMatch:
    supplier_id: int
    supplier_name: str
    confidence: float
    reason: str

    def as_dict(self) -> dict:
        return {"supplier_id": self.supplier_id, "supplier_name": self.supplier_name,
                "confidence": self.confidence, "reason": self.reason}


_THRESHOLD = 0.35


def match_supplier(conn, *, description, counterparty_name=None,
                   remarks=None, biller_name=None) -> Optional[SupplierMatch]:
    candidates_text = []
    if counterparty_name:
        candidates_text.append(("counterparty", counterparty_name))
    if biller_name:
        candidates_text.append(("biller", biller_name))
    if description:
        candidates_text.append(("description", description))
    if remarks:
        candidates_text.append(("remarks", remarks))

    if not candidates_text:
        return None

    aliases = conn.execute(
        "SELECT a.alias AS alias, s.id AS sid, s.name AS sname "
        "FROM supplier_aliases a JOIN suppliers s ON s.id = a.supplier_id "
        "WHERE s.is_active = 1"
    ).fetchall()

    sup_rows = conn.execute(
        "SELECT id AS sid, name AS sname FROM suppliers WHERE is_active = 1"
    ).fetchall()
    name_aliases = [(r["sname"], r["sid"], r["sname"]) for r in sup_rows]
    all_aliases = [(r["alias"], r["sid"], r["sname"]) for r in aliases] + name_aliases

    best: Optional[SupplierMatch] = None
    for source, text in candidates_text:
        text_upper = text.upper()
        for alias, sid, sname in all_aliases:
            alias_upper = alias.upper()
            if not alias_upper:
                continue
            if alias_upper == text_upper:
                conf = 0.98 if source == "counterparty" else 0.90
                cand = SupplierMatch(sid, sname, conf,
                                     f"Exact alias match in {source}: {alias!r}")
            elif alias_upper in text_upper:
                ratio = len(alias_upper) / max(len(text_upper), 1)
                base = {"counterparty": 0.85, "biller": 0.80,
                        "description": 0.65, "remarks": 0.60}.get(source, 0.5)
                conf = round(base * (0.75 + 0.25 * min(ratio, 1.0)), 3)
                cand = SupplierMatch(sid, sname, conf,
                                     f"Substring alias match in {source}: {alias!r}")
            else:
                continue

            if cand.confidence >= _THRESHOLD and (best is None or cand.confidence > best.confidence):
                best = cand
    return best
