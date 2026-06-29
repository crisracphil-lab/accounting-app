"""Tests for app/services/tax_summary.py — pure functions only, no DB needed."""
import pytest
from datetime import date
from app.services.tax_summary import (
    BIR_FORMS,
    TaxForm,
    due_date,
    get_form,
    period_dates,
    valid_quarters,
)


# ---------------------------------------------------------------------------
# get_form
# ---------------------------------------------------------------------------

def test_get_form_known_codes():
    for code in ("1601-C", "0619-E", "1601-EQ", "0619-F", "1601-FQ",
                 "2550Q", "1702Q", "1702", "2553Q"):
        assert get_form(code) is not None, f"Expected {code} in catalog"


def test_get_form_unknown_returns_none():
    assert get_form("9999-X") is None


def test_get_form_returns_correct_object():
    f = get_form("2550Q")
    assert f.frequency == "quarterly"
    assert "2120" in f.account_codes
    assert "1210" in f.account_codes


# ---------------------------------------------------------------------------
# period_dates — monthly
# ---------------------------------------------------------------------------

def test_period_dates_monthly_january():
    f = get_form("1601-C")  # monthly
    start, end = period_dates(f, 2025, 1)
    assert start == date(2025, 1, 1)
    assert end == date(2025, 1, 31)


def test_period_dates_monthly_february_non_leap():
    f = get_form("1601-C")
    start, end = period_dates(f, 2025, 2)
    assert start == date(2025, 2, 1)
    assert end == date(2025, 2, 28)


def test_period_dates_monthly_february_leap():
    f = get_form("1601-C")
    start, end = period_dates(f, 2024, 2)
    assert end == date(2024, 2, 29)


def test_period_dates_monthly_december():
    f = get_form("1601-C")
    start, end = period_dates(f, 2025, 12)
    assert start == date(2025, 12, 1)
    assert end == date(2025, 12, 31)


# ---------------------------------------------------------------------------
# period_dates — quarterly
# ---------------------------------------------------------------------------

def test_period_dates_quarterly_q1():
    f = get_form("2550Q")
    start, end = period_dates(f, 2025, 1)
    assert start == date(2025, 1, 1)
    assert end == date(2025, 3, 31)


def test_period_dates_quarterly_q2():
    f = get_form("2550Q")
    start, end = period_dates(f, 2025, 2)
    assert start == date(2025, 4, 1)
    assert end == date(2025, 6, 30)


def test_period_dates_quarterly_q3():
    f = get_form("2550Q")
    start, end = period_dates(f, 2025, 3)
    assert start == date(2025, 7, 1)
    assert end == date(2025, 9, 30)


def test_period_dates_quarterly_q4():
    f = get_form("2550Q")
    start, end = period_dates(f, 2025, 4)
    assert start == date(2025, 10, 1)
    assert end == date(2025, 12, 31)


# ---------------------------------------------------------------------------
# period_dates — annual
# ---------------------------------------------------------------------------

def test_period_dates_annual():
    f = get_form("1702")
    start, end = period_dates(f, 2024, 1)
    assert start == date(2024, 1, 1)
    assert end == date(2024, 12, 31)


# ---------------------------------------------------------------------------
# due_date — monthly forms (due day 10, 1 month after period end)
# ---------------------------------------------------------------------------

def test_due_date_1601c_january_due_in_february():
    f = get_form("1601-C")  # due_day=10, due_offset_months=1
    d = due_date(f, 2025, 1)
    assert d == date(2025, 2, 10)


def test_due_date_1601c_december_due_in_january():
    f = get_form("1601-C")
    d = due_date(f, 2024, 12)
    assert d == date(2025, 1, 10)


# ---------------------------------------------------------------------------
# due_date — 2550Q (quarterly, due day 25 of month after quarter end)
# ---------------------------------------------------------------------------

def test_due_date_2550q_q1():
    f = get_form("2550Q")  # quarterly, due_day=25, due_offset_months=1
    d = due_date(f, 2025, 1)
    assert d == date(2025, 4, 25)  # Q1 ends Mar 31 → April 25


def test_due_date_2550q_q4():
    f = get_form("2550Q")
    d = due_date(f, 2025, 4)
    assert d == date(2026, 1, 25)  # Q4 ends Dec 31 → Jan 25 next year


# ---------------------------------------------------------------------------
# due_date — 1702 annual (due April 15 of the following year)
# ---------------------------------------------------------------------------

def test_due_date_1702_annual():
    f = get_form("1702")  # due_day=15, due_offset_months=4
    d = due_date(f, 2024, 1)
    assert d == date(2025, 4, 15)


# ---------------------------------------------------------------------------
# due_date — 1702Q (60 days after quarter end, pushed to Monday if weekend)
# ---------------------------------------------------------------------------

def test_due_date_1702q_q1_2025():
    f = get_form("1702Q")
    # Q1 ends March 31 2025 + 60 days = May 30 2025 (Friday) → stays Friday
    d = due_date(f, 2025, 1)
    assert d == date(2025, 5, 30)


def test_due_date_1702q_q2_2025():
    f = get_form("1702Q")
    # Q2 ends June 30 2025 + 60 days = August 29 2025 (Friday)
    d = due_date(f, 2025, 2)
    assert d == date(2025, 8, 29)


# ---------------------------------------------------------------------------
# valid_quarters
# ---------------------------------------------------------------------------

def test_valid_quarters_2550q_has_four():
    f = get_form("2550Q")
    assert valid_quarters(f) == (1, 2, 3, 4)


def test_valid_quarters_1702q_has_three():
    """1702Q is only filed for Q1-Q3; the annual 1702 covers the full year."""
    f = get_form("1702Q")
    assert valid_quarters(f) == (1, 2, 3)


# ---------------------------------------------------------------------------
# Catalog completeness
# ---------------------------------------------------------------------------

def test_all_forms_have_required_fields():
    for f in BIR_FORMS:
        assert f.code, f"Form missing code: {f}"
        assert f.name, f"Form {f.code} missing name"
        assert f.frequency in ("monthly", "quarterly", "annual"), \
            f"Form {f.code} has invalid frequency: {f.frequency!r}"
        # 1702Q uses due_day=60 meaning "60 calendar days after quarter end", not a day-of-month
        if f.code != "1702Q":
            assert 1 <= f.due_day <= 31, f"Form {f.code} due_day out of range"
        assert f.due_offset_months >= 0, f"Form {f.code} negative offset"
