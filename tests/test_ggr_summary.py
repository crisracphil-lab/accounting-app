import pytest
from pathlib import Path

from app.parsers.ggr_summary import parse_ggr_weekly_summary

_SAMPLE = Path('/mnt/data/RAC-(EGS)-MONTHLY Report of GGR and PS Summary for APRIL 2026 All(6).xlsx')


@pytest.mark.skipif(not _SAMPLE.exists(), reason="April GGR file not present on this machine")
def test_parse_uploaded_april_ggr_weekly_summary():
    summary = parse_ggr_weekly_summary(_SAMPLE)

    assert summary.period_label == '04/01/2026 to 04/30/2026'
    assert len(summary.weeks) == 5
    assert summary.total.outlet_count == 131
    assert round(float(summary.total.ggr), 2) == 8936653.17
    assert summary.weeks[0].week_label == 'Week 1'
    assert summary.weeks[-1].week_label == 'Week 5'
