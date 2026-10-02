import numpy as np
import pandas as pd
import pytest

from macro_rus.period_alignment import matched_pit_production


def fixture():
    pit = pd.DataFrame([dict(region_id="R", period=date, okved_section="C", okved_raw="C",
        pit_ytd_rub=value, pit_flow_rub=flow, source_id=date, source_vintage=date)
        for date, value, flow in [("2025-03-31", 100, 50), ("2026-03-31", 120, 10), ("2025-12-31", 900, np.nan)]])
    production = pd.DataFrame([dict(region_id="R", period="2026-03-31", window_start="2026-01-01",
        okved_section="C", index_value=95, frequency="YTD", index_measure="same_period_previous_year_pct",
        source_cell="D7", source_sha256="a"*64, source_vintage="2026-08-26")])
    return pit, production


def test_ytd_not_flow_and_explicit_missing_sections():
    pit, production = fixture()
    result = matched_pit_production(pit, production, region_id="R", period="2026-03-31")
    assert len(result) == 4
    c = result.loc[result.okved_section.eq("C")].iloc[0]
    assert c.pit_yoy_pct == 20 and c.production_yoy_pct == -5
    assert c.pit_current_rub == 120 and c.pit_change_rub == 20
    assert c.window_start == pd.Timestamp("2026-01-01") and c.availability == "matched"
    assert c.production_source_cell == "D7"
    b = result.loc[result.okved_section.eq("B")].iloc[0]
    assert pd.isna(b.pit_yoy_pct) and pd.isna(b.index_value)
    assert "current_pit_unavailable" in b.availability
    assert matched_pit_production(pit, production, region_id="OTHER").empty


def test_full_year_not_q4_missing_prior_and_negative_adjustment():
    pit, production = fixture()
    annual = matched_pit_production(pit, production, period="2025-12-31")
    row = annual.loc[annual.okved_section.eq("C")].iloc[0]
    assert row.pit_current_rub == 900 and pd.isna(row.pit_yoy_pct)
    assert row.window_start == pd.Timestamp("2025-01-01")
    pit.loc[pit.period.eq("2026-03-31"), "pit_ytd_rub"] = -20
    row = matched_pit_production(pit, production, period="2026-03-31").query('okved_section == "C"').iloc[0]
    assert row.pit_current_rub == -20 and row.pit_yoy_pct == -120
    pit.loc[pit.period.eq("2025-03-31"), "pit_ytd_rub"] = 0
    row = matched_pit_production(pit, production, period="2026-03-31").query('okved_section == "C"').iloc[0]
    assert pd.isna(row.pit_yoy_pct) and "nonpositive" in row.availability


def test_perimeter_window_grain_and_lineage_fail_closed():
    pit, production = fixture()
    for changes in [dict(index_measure="same_month_previous_year_pct"), dict(window_start="2026-03-01"), dict(frequency="monthly")]:
        with pytest.raises(ValueError, match="source-native"):
            matched_pit_production(pit, production.assign(**changes))
    with pytest.raises(ValueError, match="unique"):
        matched_pit_production(pd.concat([pit, pit.iloc[[0]]]), production)
    with pytest.raises(ValueError, match="parent"):
        matched_pit_production(pit.assign(okved_raw="C.10"), production)
    sources = pd.DataFrame([dict(source_id=date, local_path="/private/raw/file.csv", sha256="b"*64, source_url="https://example.org") for date in pit.source_id])
    row = matched_pit_production(pit, production, sources, period="2026-03-31").query('okved_section == "C"').iloc[0]
    assert row.pit_current_source_file == "file.csv" and row.pit_prior_source_sha256 == "b"*64
    with pytest.raises(ValueError, match="Orphan"):
        matched_pit_production(pit, production, sources.iloc[:1])
