"""Tests for the detailed reconciliation against the real March data."""
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.parsers.combined_ggr_xls import parse_combined_xls, CombinedParseError
from app.services.detailed_reconciliation import (
    reconcile_detailed, ACCT_GGR, ACCT_PAGCOR, ACCT_AUDIT, ACCT_OP,
    ST_MATCHED, ST_MISSING, ST_WRONG,
)


_MARCH = Path("/dev/null/FOR RECONCILIATION MARCH GGR.xls")


@pytest.mark.skipif(not _MARCH.exists(), reason="March combined file not present")
class TestCombinedParser:
    def test_parses_both_sheets(self):
        cf = parse_combined_xls(_MARCH)
        assert "03/01/2026" in cf.period_label
        assert len(cf.ggr_rows) > 0
        assert len(cf.system_entries) > 0

    def test_ggr_rows_have_all_4_weeks(self):
        cf = parse_combined_xls(_MARCH)
        weeks = sorted({r.week_label for r in cf.ggr_rows})
        assert weeks == ["Week 1", "Week 2", "Week 3", "Week 4"]

    def test_system_entries_have_week_labels(self):
        cf = parse_combined_xls(_MARCH)
        labelled = [e for e in cf.system_entries if e.week_label]
        assert len(labelled) > 300
        weeks = sorted({e.week_label for e in labelled})
        assert weeks == ["Week 1", "Week 2", "Week 3", "Week 4"]

    def test_first_lakiwin_amount_present_in_system(self):
        cf = parse_combined_xls(_MARCH)
        first = next(r for r in cf.ggr_rows if r.outlet_code == "LW01MNL")
        assert first.ggr == Decimal("1275095.579999999")
        # That same amount should appear as a Credit in Week 1 system entries
        target = Decimal("1275095.58")
        match = [e for e in cf.system_entries
                 if e.week_label == "Week 1" and abs(e.credit - target) < Decimal("0.01")]
        assert len(match) >= 1


@pytest.mark.skipif(not _MARCH.exists(), reason="March combined file not present")
class TestDetailedEngine:
    def test_runs_against_real_march_file(self):
        cf = parse_combined_xls(_MARCH)
        result = reconcile_detailed(cf)
        assert result.summary.total_records > 100
        assert result.summary.matched_records >= 0
        assert result.summary.discrepancy_count == \
               result.summary.total_records - result.summary.matched_records

    def test_account_types_present(self):
        cf = parse_combined_xls(_MARCH)
        result = reconcile_detailed(cf)
        types = {r.account_type for r in result.rows}
        # Engine should produce at least GGR, PAGCOR, Audit (Operator may be 0/skipped)
        assert ACCT_GGR in types
        assert ACCT_PAGCOR in types
        assert ACCT_AUDIT in types

    def test_sign_rule_ggr(self):
        cf = parse_combined_xls(_MARCH)
        result = reconcile_detailed(cf)
        for r in result.rows:
            if r.account_type == ACCT_GGR:
                if r.ggr_amount > 0:
                    assert r.expected_side == "CREDIT"
                else:
                    assert r.expected_side == "DEBIT"
                assert r.expected_amount == abs(r.ggr_amount)

    def test_sign_rule_deductions(self):
        cf = parse_combined_xls(_MARCH)
        result = reconcile_detailed(cf)
        for r in result.rows:
            if r.account_type in (ACCT_PAGCOR, ACCT_AUDIT, ACCT_OP):
                if r.ggr_amount > 0:
                    assert r.expected_side == "DEBIT"
                else:
                    assert r.expected_side == "CREDIT"

    def test_no_double_match(self):
        """Each system entry should be consumed by at most one expected entry."""
        cf = parse_combined_xls(_MARCH)
        result = reconcile_detailed(cf)

        def money_key(value: Decimal) -> Decimal:
            # Parser values may carry spreadsheet floating precision; reconciliation
            # matches ledger amounts at cent precision.
            return value.quantize(Decimal("0.01"))

        available = Counter()
        for e in cf.system_entries:
            if not e.week_label:
                continue
            if e.debit > 0:
                available[(e.week_label, money_key(e.debit), "DEBIT")] += 1
            if e.credit > 0:
                available[(e.week_label, money_key(e.credit), "CREDIT")] += 1

        consumed = Counter()
        for r in result.rows:
            if r.discrepancy_type != ST_MATCHED:
                continue
            amount = r.system_debit if r.actual_side == "DEBIT" else r.system_credit
            consumed[(r.week_label, money_key(amount), r.actual_side)] += 1

        over_consumed = {
            key: {"matched": count, "available": available[key]}
            for key, count in consumed.items()
            if count > available[key]
        }
        assert over_consumed == {}


class TestParserErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(CombinedParseError, match="File not found"):
            parse_combined_xls(tmp_path / "nope.xls")

    def test_wrong_extension(self, tmp_path):
        bad = tmp_path / "bad.xlsx"
        bad.write_text("nope")
        with pytest.raises(CombinedParseError, match="Expected .xls"):
            parse_combined_xls(bad)
