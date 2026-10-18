from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from macro_rus.cumulative_production import load_cumulative_candidate
from macro_rus.fiscal_view import load_candidate
from macro_rus.macro_preview import load_macro_candidate
from macro_rus.period_alignment import matched_pit_production
from macro_rus.regional_overview import regional_status_table
from macro_rus.revenue_view import load_revenue_candidate

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.private_data(
    "outputs/expanded_macro_candidate_2026-09-29_v2",
    "outputs/cumulative_production_candidate_2026-10-02",
    "outputs/regional_concordance_2026-09-29_v2/fiscal_preview",
    "outputs/regional_revenue_candidate_2026-09-30",
    "outputs/annual_social_candidate_2026-10-02",
)


def loaded():
    out = ROOT / "outputs"
    macro, _ = load_macro_candidate(out / "expanded_macro_candidate_2026-09-29_v2")
    cumulative, _ = load_cumulative_candidate(out / "cumulative_production_candidate_2026-10-02")
    fiscal, _ = load_candidate(out / "regional_concordance_2026-09-29_v2/fiscal_preview")
    revenue, _ = load_revenue_candidate(out / "regional_revenue_candidate_2026-09-30")
    return macro, cumulative, fiscal, revenue


def test_real_matching_windows_and_absent_mining_lineage():
    macro, cumulative, _, _ = loaded()
    result = matched_pit_production(macro["pit_receipts"], cumulative, macro["sources"])
    assert len(result) == 78 * 3 * 4
    assert result.availability.eq("matched").sum() == 78 * 3
    assert not result.duplicated(["region_id", "period", "okved_section"]).any()
    row = result.loc[result.region_id.eq("RU-KLU") & result.okved_section.eq("C") & result.period.eq(pd.Timestamp("2026-03-31"))].iloc[0]
    assert row.index_value == 106.3
    assert row.pit_yoy_pct == pytest.approx((4815340000/3841553000-1)*100)
    missing = result.loc[result.region_id.eq("RU-KLU") & result.okved_section.eq("B") & result.period.eq(pd.Timestamp("2026-03-31"))].iloc[0]
    assert pd.isna(missing.pit_current_rub) and missing.index_value == 81.3
    assert missing.pit_current_source_sha256 and "20260401" in missing.pit_current_source_id
    assert result.pit_current_source_file.str.contains("/", na=False).sum() == 0


def test_real_overview_common_cutoff_and_no_rank_or_missing_zero():
    _, cumulative, fiscal, revenue = loaded()
    result = regional_status_table(fiscal, revenue, cumulative, "2026-06-30")
    assert len(result) == 78 and result.region_id.nunique() == 78
    assert result.period.eq("2026-06-30").all() and result.window_start.eq("2026-01-01").all()
    assert result.region_name_ru.tolist() == sorted(result.region_name_ru)
    assert not any("rank" in name.lower() for name in result.columns)
    assert result.own_source_rub.notna().all()
    bad = cumulative.copy()
    bad.loc[bad.period.eq("2026-06-30"), "window_start"] = pd.Timestamp("2026-04-01")
    with pytest.raises(ValueError, match="1 January"):
        regional_status_table(fiscal, revenue, bad, "2026-06-30")
    missing = cumulative.loc[~(cumulative.region_id.eq("RU-KLU") & cumulative.period.eq("2026-06-30"))]
    row = regional_status_table(fiscal, revenue, missing, "2026-06-30").query('region_id == "RU-KLU"').iloc[0]
    assert pd.isna(row.production_growth_yoy_pct) and pd.notna(row.own_source_rub)


def preview_environment(monkeypatch):
    for variable, folder in {
        "MACRO_RUS_MACRO_CANDIDATE_DIR": "expanded_macro_candidate_2026-09-29_v2",
        "MACRO_RUS_REVENUE_CANDIDATE_DIR": "regional_revenue_candidate_2026-09-30",
        "MACRO_RUS_FISCAL_CANDIDATE_DIR": "regional_concordance_2026-09-29_v2/fiscal_preview",
        "MACRO_RUS_ANNUAL_CANDIDATE_DIR": "annual_social_candidate_2026-10-02",
        "MACRO_RUS_CUMULATIVE_CANDIDATE_DIR": "cumulative_production_candidate_2026-10-02",
    }.items():
        monkeypatch.setenv(variable, str(ROOT / "outputs" / folder))


def test_new_pages_region_basis_and_period_controls(monkeypatch):
    from streamlit.testing.v1 import AppTest
    preview_environment(monkeypatch)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    app.sidebar.radio[0].set_value("PIT–production check").run()
    assert not app.exception
    assert any("Both windows" in item.value for item in app.caption)
    table = app.dataframe[0].value
    assert table.loc[table["OKVED2 section"].eq("B"), "Nominal PIT YoY"].eq("Not available").all()
    assert table.loc[table["OKVED2 section"].eq("C"), "Production index"].eq("106.3").all()
    next(w for w in app.selectbox if w.label == "PIT–production reporting period").set_value(np.datetime64("2025-12-31")).run()
    assert not app.exception
    assert app.dataframe[0].value["Nominal PIT YoY"].eq("Not available").all()
    assert any("full year, not Q4" in item.value for item in app.caption)
    assert any("fns-1nom-20260101" in item.value for item in app.caption)
    assert not any("fns-1nom-20260401" in item.value for item in app.caption)
    app.sidebar.radio[0].set_value("Fiscal changes (preview)").run()
    assert not app.exception and len(app.metric) == 3
    cumulative_expenditure = app.metric[1].value
    next(w for w in app.selectbox if w.label == "Amount basis").set_value("period_flow").run()
    assert not app.exception and app.metric[1].value != cumulative_expenditure
    assert any("Q2 equals" in item.value for item in app.caption)
    kaluga = app.metric[1].value
    app.sidebar.selectbox[0].set_value("RU-SVE").run()
    assert not app.exception and app.metric[1].value != kaluga
    next(w for w in app.selectbox if w.label == "Fiscal reporting period").set_value(np.datetime64("2023-03-31")).run()
    assert not app.exception
    assert any("Prior-year same-window" in item.value for item in app.info)
    assert app.dataframe[0].value["Share change (pp)"].eq("Unavailable").all()
    app.sidebar.radio[0].set_value("Eligible-region overview (optional)").run()
    assert not app.exception and len(app.dataframe[0].value) == 78
    next(w for w in app.selectbox if w.label == "Overview reporting period").set_value(np.datetime64("2023-03-31")).run()
    assert not app.exception
    assert app.dataframe[0].value["Revenue YoY (%)"].eq("Unavailable").all()
