"""
Reconciliation engine.

A reconciliation template defines:
  - Which Excel metrics to consider (ggr, pagcor_share, audit_fee, ...)
  - For each metric, the user's sign rule:
      'positive_to_credit'  : positive  -> expected credit, negative -> expected debit
      'positive_to_debit'   : positive  -> expected debit,  negative -> expected credit
  - Which system account (by A/C No.) to compare against.

The engine sums Excel positives/negatives per metric, applies the rule to
get expected Dr and expected Cr per metric, then compares to the system's
actual Dr/Cr totals for the linked account. Variance is reported per metric.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from decimal import Decimal
from typing import List, Optional

from app.parsers.ggr_excel import GGRWorkbookTotals, MetricTotals


# Sign-rule tokens
RULE_POS_TO_CREDIT = "positive_to_credit"   # GGR-style: revenue
RULE_POS_TO_DEBIT  = "positive_to_debit"    # PAGCOR/Audit/Operators-style: deduction


@dataclass
class MetricRule:
    metric: str             # attribute name on GGRWorkbookTotals (e.g. 'ggr')
    label: str              # human label (e.g. 'Gross Gaming Revenue')
    rule: str               # RULE_POS_TO_CREDIT | RULE_POS_TO_DEBIT
    system_account_code: Optional[str] = None    # account in the ledger
    tolerance: Decimal = Decimal("1.00")


@dataclass
class MetricResult:
    metric: str
    label: str
    rule: str
    excel_positive: Decimal
    excel_negative: Decimal
    expected_debit: Decimal
    expected_credit: Decimal
    system_account_code: Optional[str]
    system_actual_debit: Optional[Decimal]
    system_actual_credit: Optional[Decimal]
    debit_variance:  Optional[Decimal]
    credit_variance: Optional[Decimal]
    within_tolerance: Optional[bool]


@dataclass
class ReconciliationResult:
    template_name: str
    period_start: Optional[date]
    period_end: Optional[date]
    excel_filename: str
    system_filename: str
    metrics: List[MetricResult] = field(default_factory=list)

    @property
    def all_within_tolerance(self) -> bool:
        return all(m.within_tolerance is True for m in self.metrics
                   if m.within_tolerance is not None)


def _expected_dr_cr(metric: MetricTotals, rule: str) -> tuple[Decimal, Decimal]:
    pos = metric.positive
    neg_abs = -metric.negative   # negative is stored as a negative number
    if rule == RULE_POS_TO_CREDIT:
        return neg_abs, pos
    if rule == RULE_POS_TO_DEBIT:
        return pos, neg_abs
    raise ValueError(f"Unknown rule {rule!r}")


def reconcile(excel_totals: GGRWorkbookTotals,
              system_summary,
              template_name: str,
              rules: List[MetricRule],
              excel_filename: str = "",
              system_filename: str = "") -> ReconciliationResult:
    result = ReconciliationResult(
        template_name=template_name,
        period_start=excel_totals.period_start,
        period_end=excel_totals.period_end,
        excel_filename=excel_filename,
        system_filename=system_filename,
    )

    for r in rules:
        metric_totals = getattr(excel_totals, r.metric, None)
        if metric_totals is None:
            raise ValueError(
                f"Excel totals has no metric {r.metric!r}. "
                f"Available: ggr, pagcor_share, audit_fee, operator, service_prov")

        expected_dr, expected_cr = _expected_dr_cr(metric_totals, r.rule)

        actual_dr = actual_cr = None
        dr_var = cr_var = None
        within = None

        if r.system_account_code:
            acct = system_summary.accounts.get(r.system_account_code)
            if acct:
                actual_dr = acct.total_debit
                actual_cr = acct.total_credit
                dr_var = expected_dr - actual_dr
                cr_var = expected_cr - actual_cr
                within = (abs(dr_var) <= r.tolerance and
                          abs(cr_var) <= r.tolerance)

        result.metrics.append(MetricResult(
            metric=r.metric, label=r.label, rule=r.rule,
            excel_positive=metric_totals.positive,
            excel_negative=metric_totals.negative,
            expected_debit=expected_dr, expected_credit=expected_cr,
            system_account_code=r.system_account_code,
            system_actual_debit=actual_dr, system_actual_credit=actual_cr,
            debit_variance=dr_var, credit_variance=cr_var,
            within_tolerance=within,
        ))

    return result


# ---- Built-in templates -----------------------------------------------------

def ggr_template(system_account_code: str = "4111") -> List[MetricRule]:
    """
    The user's GGR convention:
      GGR positive -> system credit (revenue earned)
      GGR negative -> system debit  (revenue reversal/refund)
      PAGCOR Share / Audit Fee / Operator / Service Provider:
          positive -> debit  (deduction expense)
          negative -> credit (deduction reversal)

    For now the system file only contains account 4111 'Revenue Sharing
    Lakiwin', so only the GGR metric has system_account_code set.
    PAGCOR/Audit/Operator/SP rows are still computed as 'expected' so you
    can see what should land where, but actual variance only appears once
    you upload their respective system exports.
    """
    return [
        MetricRule(metric="ggr",
                   label="Gross Gaming Revenue",
                   rule=RULE_POS_TO_CREDIT,
                   system_account_code=system_account_code),
        MetricRule(metric="pagcor_share",
                   label="PAGCOR Share (30% of GGR)",
                   rule=RULE_POS_TO_DEBIT,
                   system_account_code=None),
        MetricRule(metric="audit_fee",
                   label="Audit Fee (10% of PAGCOR Share)",
                   rule=RULE_POS_TO_DEBIT,
                   system_account_code=None),
        MetricRule(metric="operator",
                   label="Operator Share",
                   rule=RULE_POS_TO_DEBIT,
                   system_account_code=None),
        MetricRule(metric="service_prov",
                   label="Service Provider Share",
                   rule=RULE_POS_TO_DEBIT,
                   system_account_code=None),
    ]
