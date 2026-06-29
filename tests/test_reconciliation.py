"""Tests for the reconciliation engine using the real GGR files."""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.parsers.ggr_excel import parse_ggr_excel, GGRParseError
from app.parsers.system_ledger import parse_system_ledger, SystemLedgerParseError
from app.services.reconciliation import (
    reconcile, ggr_template, MetricRule,
    RULE_POS_TO_CREDIT, RULE_POS_TO_DEBIT,
)


_EXCEL = Path("/dev/null/GGR PER EXCEL.xlsx")
_SYSTEM = Path("/dev/null/GGR PER SYSTEM.xls")


@pytest.mark.skipif(not _EXCEL.exists(), reason="GGR Excel file not present")
class TestGGRExcelParser:
    def test_parses_all_outlets(self):
        t = parse_ggr_excel(_EXCEL)
        assert t.sheet_count == 26
        assert t.daily_rows > 0
        assert len(t.per_outlet) == 26

    def test_period_filter_april_first_27(self):
        t = parse_ggr_excel(_EXCEL,
                            period_start=date(2026, 4, 1),
                            period_end=date(2026, 4, 27))
        # 26 outlets x 27 days = max 702 daily rows (some may be all-zero
        # but they still parse)
        assert t.daily_rows == 702

    def test_ggr_split_signs(self):
        t = parse_ggr_excel(_EXCEL,
                            period_start=date(2026, 4, 1),
                            period_end=date(2026, 4, 27))
        # GGR has both positive (winning days) and negative (losing days) values
        assert t.ggr.positive > 0
        assert t.ggr.negative < 0

    def test_pagcor_30_percent_of_ggr_directional(self):
        # Only verify direction - the per-row 30% will not perfectly match
        # the SUM of 30% on positives because positives and negatives mix.
        t = parse_ggr_excel(_EXCEL,
                            period_start=date(2026, 4, 1),
                            period_end=date(2026, 4, 27))
        # PAGCOR positive should be roughly 30% of GGR positive (some rounding)
        ratio = float(t.pagcor_share.positive) / float(t.ggr.positive)
        assert 0.29 <= ratio <= 0.31, f"PAGCOR/GGR ratio {ratio} outside 29-31%"


@pytest.mark.skipif(not _SYSTEM.exists(), reason="GGR System file not present")
class TestSystemLedgerParser:
    def test_account_4111_extracted(self):
        s = parse_system_ledger(_SYSTEM)
        assert "4111" in s.accounts
        acct = s.accounts["4111"]
        assert "Revenue" in acct.account_title
        assert acct.total_debit > 0
        assert acct.total_credit > 0
        assert acct.rows > 0

    def test_period_filter(self):
        s_full = parse_system_ledger(_SYSTEM)
        s_april = parse_system_ledger(_SYSTEM,
                                      period_start=date(2026, 4, 1),
                                      period_end=date(2026, 4, 30))
        # April-filtered should be subset of full
        assert s_april.accounts["4111"].rows <= s_full.accounts["4111"].rows


@pytest.mark.skipif(not (_EXCEL.exists() and _SYSTEM.exists()),
                    reason="GGR files not present")
class TestReconciliationEngine:
    def test_full_reconciliation_runs(self):
        ex = parse_ggr_excel(_EXCEL,
                             period_start=date(2026, 4, 1),
                             period_end=date(2026, 4, 27))
        sys = parse_system_ledger(_SYSTEM,
                                  period_start=date(2026, 4, 1),
                                  period_end=date(2026, 4, 27))
        result = reconcile(ex, sys, template_name="GGR April",
                           rules=ggr_template("4111"))
        # All 5 metrics produced
        assert len(result.metrics) == 5
        labels = [m.label for m in result.metrics]
        assert "Gross Gaming Revenue" in labels
        assert "PAGCOR Share (30% of GGR)" in labels

    def test_ggr_metric_has_system_link(self):
        ex = parse_ggr_excel(_EXCEL,
                             period_start=date(2026, 4, 1),
                             period_end=date(2026, 4, 27))
        sys = parse_system_ledger(_SYSTEM,
                                  period_start=date(2026, 4, 1),
                                  period_end=date(2026, 4, 27))
        result = reconcile(ex, sys, template_name="GGR April",
                           rules=ggr_template("4111"))
        ggr_metric = next(m for m in result.metrics
                          if m.label == "Gross Gaming Revenue")
        # Engine populated actuals from system
        assert ggr_metric.system_actual_debit is not None
        assert ggr_metric.system_actual_credit is not None
        # Variance is computed (may not be zero - that's the whole point)
        assert ggr_metric.debit_variance is not None
        assert ggr_metric.credit_variance is not None

    def test_other_metrics_have_no_system_link(self):
        ex = parse_ggr_excel(_EXCEL,
                             period_start=date(2026, 4, 1),
                             period_end=date(2026, 4, 27))
        sys = parse_system_ledger(_SYSTEM)
        result = reconcile(ex, sys, template_name="x",
                           rules=ggr_template("4111"))
        for m in result.metrics:
            if m.label != "Gross Gaming Revenue":
                assert m.system_actual_debit is None
                assert m.within_tolerance is None

    def test_sign_rule_application(self):
        # GGR positive should land on Cr expected; positive PAGCOR on Dr expected
        ex = parse_ggr_excel(_EXCEL,
                             period_start=date(2026, 4, 1),
                             period_end=date(2026, 4, 27))
        sys = parse_system_ledger(_SYSTEM)
        result = reconcile(ex, sys, template_name="x",
                           rules=ggr_template("4111"))
        ggr_m = next(m for m in result.metrics if m.label == "Gross Gaming Revenue")
        pag_m = next(m for m in result.metrics if "PAGCOR" in m.label)
        # GGR rule: positive -> credit
        assert ggr_m.expected_credit == ggr_m.excel_positive
        assert ggr_m.expected_debit == -ggr_m.excel_negative
        # PAGCOR rule: positive -> debit
        assert pag_m.expected_debit == pag_m.excel_positive
        assert pag_m.expected_credit == -pag_m.excel_negative


class TestErrors:
    def test_missing_excel(self, tmp_path):
        with pytest.raises(GGRParseError, match="File not found"):
            parse_ggr_excel(tmp_path / "nope.xlsx")

    def test_wrong_extension_excel(self, tmp_path):
        bad = tmp_path / "bad.csv"
        bad.write_text("a,b,c")
        with pytest.raises(GGRParseError, match="Expected .xlsx"):
            parse_ggr_excel(bad)

    def test_missing_system(self, tmp_path):
        with pytest.raises(SystemLedgerParseError, match="File not found"):
            parse_system_ledger(tmp_path / "nope.xls")
