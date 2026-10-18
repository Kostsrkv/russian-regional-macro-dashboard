from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from openpyxl import Workbook

from macro_rus.social_spending import social_history
from macro_rus.annual_production import read_annual, select_vintages, load_annual_candidate

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "outputs/annual_social_candidate_2026-10-02"


def fiscal_fixture():
    rows = []
    for region, factor in [("R", 1), ("OTHER", 10)]:
        for date, factor2 in [("2025-03-31", 1), ("2025-06-30", 2), ("2026-03-31", 1.5), ("2026-06-30", 4)]:
            for code, value in [("1000", 20), ("0700", 30), ("0900", 10), ("expenditure_total", 100)]:
                rows.append(dict(region_id=region, period=pd.Timestamp(date), metric_code=code,
                    kind="summary" if code == "expenditure_total" else "expenditure_function",
                    actual_ytd_rub=value * factor * factor2, budget_level="consolidated_regional_budget"))
    return pd.DataFrame(rows)


def test_social_same_window_region_and_denominator():
    result = social_history(fiscal_fixture(), "R", "2026-03-31")
    assert set(result.period.dt.month) == {3}
    assert set(result.region_id) == {"R"}
    row = result.loc[result.period.eq("2026-03-31") & result.metric_code.eq("1000")].iloc[0]
    assert row.actual_ytd_rub == 30
    assert row.share_pct == 20
    assert row.growth_yoy_pct == 50
    assert row.share_change_pp == 0
    h1 = social_history(fiscal_fixture(), "R", "2026-06-30")
    assert h1.loc[h1.period.eq("2026-06-30"), "growth_yoy_pct"].eq(100).all()


def test_social_missing_zero_negative_and_scope():
    fixture = fiscal_fixture()
    missing = fixture.loc[~(fixture.region_id.eq("R") & fixture.period.eq("2026-03-31") & fixture.metric_code.eq("1000"))]
    result = social_history(missing, "R", "2026-03-31")
    row = result.loc[result.period.eq("2026-03-31") & result.metric_code.eq("1000")].iloc[0]
    assert pd.isna(row.actual_ytd_rub) and row.availability == "unavailable"
    fixture.loc[fixture.period.eq("2025-03-31") & fixture.metric_code.eq("1000"), "actual_ytd_rub"] = 0
    fixture.loc[fixture.period.eq("2026-03-31") & fixture.metric_code.eq("1000"), "actual_ytd_rub"] = -5
    result = social_history(fixture, "R", "2026-03-31")
    row = result.loc[result.period.eq("2026-03-31") & result.metric_code.eq("1000")].iloc[0]
    assert row.actual_ytd_rub == -5 and pd.isna(row.growth_yoy_pct)
    with pytest.raises(ValueError, match="unique"):
        social_history(pd.concat([fixture, fixture.iloc[[0]]]), "R", "2026-03-31")
    with pytest.raises(ValueError, match="consolidated"):
        social_history(fixture.assign(budget_level="federal"), "R", "2026-03-31")
    assert social_history(None, "R", None).empty


def annual_fixture(path, date="26.08.2026", value=110, basis="к предыдущему году"):
    w = Workbook()
    w.active.title = "Содержание"
    w.active["B14"] = "Обновлено: " + date
    for number in range(1, 6):
        s = w.create_sheet(str(number))
        s["A3"] = basis
        s["B4"] = "20241,2"
        s["C4"] = "20251"
        s["A5"] = "Калужская область"
        s["B5"] = 95.5
        s["C5"] = value
        s["A7"] = "1 В качестве весов используется структура 2023 базисного года."
    w.save(path)
    return [{"region_id": "R", "rosstat_name_ru": "Калужская область", "treasury_name_ru": "Калужская область"}]


def test_annual_source_cells_headers_and_missing_vintage(tmp_path):
    older = tmp_path / "old.xlsx"
    newer = tmp_path / "new.xlsx"
    records = annual_fixture(older, "06.02.2026")
    annual_fixture(newer, value="2)")
    old, source = read_annual(older, records)
    new, _ = read_annual(newer, records)
    assert source["source_vintage"] == "2026-02-06"
    assert len(old) == 10
    assert old.loc[old.period.dt.year.eq(2024), "growth_yoy_pct"].eq(-4.5).all()
    assert old.loc[old.period.dt.year.eq(2025), "source_cell"].eq("C5").all()
    assert old.loc[old.okved_section.eq("TOTAL"), "index_base"].str.contains("2023 GVA").all()
    selected = select_vintages([old, new])
    assert selected.source_vintage.eq("2026-08-26").all()
    assert selected.loc[selected.period.dt.year.eq(2025), "index_value"].isna().all()
    assert selected.loc[selected.period.dt.year.eq(2025), "availability"].eq("unavailable_source_marker").all()
    with pytest.raises(ValueError, match="Ambiguous"):
        select_vintages([new, new])


def test_annual_basis_and_region_mismatch_fail_closed(tmp_path):
    path = tmp_path / "bad.xlsx"
    records = annual_fixture(path, basis="к предыдущему месяцу")
    with pytest.raises(ValueError, match="basis"):
        read_annual(path, records)
    annual_fixture(path)
    with pytest.raises(ValueError, match="unmatched"):
        read_annual(path, [{**records[0], "rosstat_name_ru": "Missing region"}])


@pytest.mark.private_data("outputs/annual_social_candidate_2026-10-02")
def test_real_annual_candidate_and_source_evidence():
    data, manifest = load_annual_candidate(CANDIDATE)
    assert manifest["regions"] == 78
    assert len(data) == 78 * 11 * 5
    assert data.loc[data.period.dt.year.ge(2020), "source_vintage"].eq("2026-08-26").all()
    assert np.allclose(data.growth_yoy_pct, data.index_value-100, equal_nan=True)
    assert data.source_cell.notna().all()
    assert not data.duplicated(["region_id", "period", "okved_section"]).any()


@pytest.mark.private_data("outputs/expanded_macro_candidate_2026-09-29_v2",
                          "outputs/regional_revenue_candidate_2026-09-30",
                          "outputs/regional_concordance_2026-09-29_v2/fiscal_preview",
                          "outputs/annual_social_candidate_2026-10-02")
def test_dashboard_social_and_annual_controls(monkeypatch):
    from streamlit.testing.v1 import AppTest
    for variable, folder in {
        "MACRO_RUS_MACRO_CANDIDATE_DIR": "expanded_macro_candidate_2026-09-29_v2",
        "MACRO_RUS_REVENUE_CANDIDATE_DIR": "regional_revenue_candidate_2026-09-30",
        "MACRO_RUS_FISCAL_CANDIDATE_DIR": "regional_concordance_2026-09-29_v2/fiscal_preview",
        "MACRO_RUS_ANNUAL_CANDIDATE_DIR": "annual_social_candidate_2026-10-02",
    }.items():
        monkeypatch.setenv(variable, str(ROOT / "outputs" / folder))
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    app.sidebar.radio[0].set_value("Social spending (preview)").run()
    assert not app.exception
    assert len(app.metric) == 3
    assert len(app.dataframe[0].value) == 12
    assert app.dataframe[0].value.loc[app.dataframe[0].value.Year.eq("2023"), "Nominal YoY (%)"].eq("Unavailable").all()
    latest = app.metric[0].value
    next(w for w in app.selectbox if w.label == "Social-spending period").set_value(np.datetime64("2023-03-31")).run()
    assert not app.exception
    assert app.metric[0].value != latest
    assert app.metric[0].delta == ""
    next(w for w in app.selectbox if w.label == "Social-spending period").set_value(np.datetime64("2026-06-30")).run()
    kaluga = app.metric[0].value
    app.sidebar.selectbox[0].set_value("RU-SVE").run()
    assert not app.exception
    assert app.metric[0].value != kaluga
    app.sidebar.radio[0].set_value("Annual production (preview)").run()
    assert not app.exception
    assert len(app.dataframe[0].value) == 11
    app.sidebar.selectbox[0].set_value("FNS-77").run()
    next(w for w in app.selectbox if w.label == "Annual production scope").set_value("B").run()
    assert not app.exception
    assert app.dataframe[0].value.index_value.eq("Unavailable").all()
    assert any("No numeric annual indices" in item.value for item in app.info)
    assert set(app.dataframe[0].value.source_vintage) == {"2026-02-06", "2026-08-26"}
    next(w for w in app.selectbox if w.label == "Annual production scope").set_value("C").run()
    assert not app.exception
    assert len(app.dataframe[0].value) == 11
