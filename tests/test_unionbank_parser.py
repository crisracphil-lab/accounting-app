"""
Tests for the UnionBank XLSX parser.

Uses the real uploaded file '032026 UB PESO.xlsx' if present.
The path is auto-detected; if the file is not available, parser-feature
tests are skipped (still no mock data is constructed).
"""
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.parsers.unionbank_xlsx import (
    parse_statement, BankStatementParseError, _extract_counterparty,
)


_REAL_FILE_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "032026 UB PESO.xlsx",
    Path("/dev/null/032026 UB PESO.xlsx"),
]


def _real_file() -> Path | None:
    for p in _REAL_FILE_CANDIDATES:
        if p.exists():
            return p
    return None


class TestCounterpartyExtraction:
    @pytest.mark.parametrize("desc,expected", [
        ("Sent to CELLA STORAGE SPACE RENTAL    BDO 097718",  "CELLA STORAGE SPACE RENTAL"),
        ("Sent to Globe Telecom Inc             BPI 290214",  "Globe Telecom Inc"),
        ("Sent to John Bench P Rodriguez        EWB 892320",  "John Bench P Rodriguez"),
        ("Received from LYRA JARCE GALANG SEA 901637",         "LYRA JARCE GALANG"),
        ("ENCASHMENT",                                          None),
        ("INWARD CLEARING CHECK",                              None),
        ("BILLS PAYMENT",                                      None),
    ])
    def test_extract(self, desc, expected):
        assert _extract_counterparty(desc) == expected


@pytest.mark.skipif(_real_file() is None, reason="Real UB statement not present")
class TestRealUnionBankFile:
    @classmethod
    def setup_class(cls):
        cls.header, cls.txns = parse_statement(_real_file())

    def test_header_extracted(self):
        assert self.header.account_name == "RAC PHIL CORP"
        assert self.header.account_number == "000320028458"
        assert self.header.currency == "PHP"
        assert "2026/03" in self.header.period

    def test_transactions_deduplicated(self):
        # All transactions should have unique tx IDs
        ids = [t.transaction_id for t in self.txns]
        assert len(ids) == len(set(ids))
        # We saw 219 unique txs across the four sheets
        assert len(self.txns) >= 200, f"Expected ~219, got {len(self.txns)}"

    def test_cella_payment_present(self):
        # The Cella ₱118,417.75 payment we previously generated a JE for
        cella_payments = [t for t in self.txns
                          if t.debit_amount == Decimal("118417.75")]
        assert len(cella_payments) == 1, \
            f"Expected one ₱118,417.75 outflow, got {len(cella_payments)}"
        tx = cella_payments[0]
        assert tx.transaction_date.month == 3
        assert tx.transaction_date.year == 2026
        assert "INWARD CLEARING CHECK" in tx.description

    def test_amounts_parsed_correctly(self):
        # All txs should have either a debit or credit, not both
        for t in self.txns:
            both = t.debit_amount > 0 and t.credit_amount > 0
            neither = t.debit_amount == 0 and t.credit_amount == 0
            assert not both, f"Tx {t.transaction_id} has both debit and credit"
            assert not neither, f"Tx {t.transaction_id} has zero amount"

    def test_counterparty_extraction_on_real_data(self):
        sent_to = [t for t in self.txns if t.description.startswith("Sent to ")]
        assert len(sent_to) > 0
        # Every "Sent to" should have a counterparty extracted
        unparsed = [t for t in sent_to if not t.counterparty_name]
        assert len(unparsed) == 0, \
            f"Failed to extract counterparty from: {[t.description for t in unparsed]}"

    def test_dates_in_period(self):
        for t in self.txns:
            assert t.transaction_date.year == 2026
            assert t.transaction_date.month == 3, \
                f"Tx {t.transaction_id} dated {t.transaction_date} is outside March 2026"


class TestParserErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(BankStatementParseError, match="File not found"):
            parse_statement(tmp_path / "nope.xlsx")

    def test_wrong_extension(self, tmp_path):
        bad = tmp_path / "bad.txt"
        bad.write_text("hello")
        with pytest.raises(BankStatementParseError, match="Expected .xlsx"):
            parse_statement(bad)
