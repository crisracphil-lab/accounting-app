from decimal import Decimal
from pathlib import Path

import openpyxl

from app.parsers.combined_ggr_xls import parse_two_file_ggr
from app.services.detailed_reconciliation import reconcile_detailed, ST_MATCHED


def _make_raw_ggr(path: Path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Raw GGR"
    ws.append(["Period Covered: 02/01/2026 to 02/28/2026"])
    ws.append(["Operator Name", "Outlet Name", "Outlet Code", "GGR", "PAGCOR", "Audit", "Operator"])
    ws.append(["", "WEEK 1 (02/01/2026 to 02/07/2026)", "", "", "", "", ""])
    ws.append(["RAC PHIL CORP.", "Manila Branch", "LW01MNL", 1000, 200, 50, 0])
    wb.save(path)


def _make_per_system(path: Path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Per System"
    ws.append(["Report"])
    ws.append(["A/C No.", "A/C Title", "", "Date", "Voucher No", "Department", "", "Description", "", "Debit", "Credit"])
    ws.append([4111, "Gross Gaming Revenue"])
    ws.append(["", "", "", "2026-02-03", "JV-001", "OPS", "", "WEEK 1 Manila Branch GGR", "", 0, 1000])
    ws.append(["", "", "", "2026-02-03", "JV-002", "OPS", "", "WEEK 1 Manila Branch PAGCOR", "", 200, 0])
    ws.append(["", "", "", "2026-02-03", "JV-003", "OPS", "", "WEEK 1 Manila Branch Audit", "", 50, 0])
    wb.save(path)


def test_detailed_ggr_accepts_two_separate_xlsx_files(tmp_path):
    raw = tmp_path / "raw_ggr.xlsx"
    system = tmp_path / "per_system.xlsx"
    _make_raw_ggr(raw)
    _make_per_system(system)

    combined = parse_two_file_ggr(raw, system)
    assert combined.period_label == "02/01/2026 to 02/28/2026"
    assert len(combined.ggr_rows) == 1
    assert len(combined.system_entries) == 3

    result = reconcile_detailed(combined, account_filter="4111")
    assert result.summary.total_records == 3
    assert result.summary.matched_records == 3
    assert all(row.discrepancy_type == ST_MATCHED for row in result.rows)
