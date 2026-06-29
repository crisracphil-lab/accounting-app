"""Tests for RA260 parser + ingestion."""
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.parsers.ra260_payments import parse_ra260, RA260ParseError


_FILE = Path("/dev/null/RA260_20260430135455895.xlsx")


@pytest.mark.skipif(not _FILE.exists(), reason="RA260 file not present")
class TestRA260Parser:
    def test_row_count(self):
        rows = parse_ra260(_FILE)
        assert len(rows) == 96

    def test_status_breakdown(self):
        rows = parse_ra260(_FILE)
        by_status = {}
        for r in rows:
            by_status[r.transaction_status] = by_status.get(r.transaction_status, 0) + 1
        assert by_status["Transaction Successful"] == 84
        assert by_status["Transaction Released"] == 4
        assert by_status["Cancelled"] == 4

    def test_total_amount(self):
        rows = parse_ra260(_FILE)
        total = sum(r.amount for r in rows)
        assert total == Decimal("8065763.14")

    def test_amounts_handle_comma_strings(self):
        rows = parse_ra260(_FILE)
        # Find a known row: Jasmine Monterozo 1,500.00
        jas = next(r for r in rows if r.beneficiary_name == "Jasmine Monterozo")
        assert jas.amount == Decimal("1500.00")
        assert jas.tran_id == "UB352876"
        assert jas.remittance_type == "instaPay"

    def test_dates_parsed(self):
        rows = parse_ra260(_FILE)
        dated = [r for r in rows if r.transaction_date is not None]
        assert len(dated) >= 90  # most rows should have a date
        assert all(r.transaction_date.year == 2026 for r in dated)

    def test_cella_payment_present(self):
        rows = parse_ra260(_FILE)
        cella = [r for r in rows
                 if r.beneficiary_name and "CELLA" in r.beneficiary_name.upper()]
        assert len(cella) >= 1


@pytest.mark.skipif(not _FILE.exists(), reason="RA260 file not present")
class TestIngestion:
    def test_ingest_into_clean_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ACCOUNTING_DB", str(tmp_path / "test.db"))
        import importlib
        from app import db as db_module
        importlib.reload(db_module)
        db_module.init_db()
        from app.services import ra260_ingest
        importlib.reload(ra260_ingest)

        result = ra260_ingest.ingest_ra260(_FILE)
        assert result.rows_inserted == 96
        assert result.rows_skipped_duplicate == 0

        with db_module.db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM payment_instructions").fetchone()["n"]
            assert n == 96

    def test_duplicate_upload_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ACCOUNTING_DB", str(tmp_path / "test2.db"))
        import importlib
        from app import db as db_module
        importlib.reload(db_module)
        db_module.init_db()
        from app.services import ra260_ingest
        importlib.reload(ra260_ingest)

        ra260_ingest.ingest_ra260(_FILE)
        with pytest.raises(ra260_ingest.DuplicatePaymentsUploadError):
            ra260_ingest.ingest_ra260(_FILE)


class TestErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(RA260ParseError, match="File not found"):
            parse_ra260(tmp_path / "nope.xlsx")

    def test_wrong_extension(self, tmp_path):
        bad = tmp_path / "bad.csv"
        bad.write_text("a,b,c")
        with pytest.raises(RA260ParseError, match="Expected .xlsx"):
            parse_ra260(bad)
