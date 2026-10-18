from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from macro_rus.regional_profile import (production_history, budget_history, fiscal_history,
    fiscal_comparison, transfer_share, profile_notes, profile_evidence, same_period_history)
from macro_rus.revenue_view import OWN, PIT, CIT, NON_REPAYABLE, TRANSFERS

ROOT = Path(__file__).resolve().parents[1]


def fixture_budget():
    rows = []
    for date, total, own, transfers in [("2025-03-31", 100, 80, 15), ("2025-06-30", 200, 160, 30),
                                       ("2026-03-31", 125, 90, 20), ("2026-06-30", 300, 200, 40)]:
        for code, value in [("TOTAL_REVENUE", total), (OWN, own), (PIT, own / 2), (CIT, own / 4),
                            (NON_REPAYABLE, total - own), (TRANSFERS, transfers)]:
            rows.append(dict(region_id="R", period=pd.Timestamp(date), revenue_code=code,
                revenue_name_en="Total revenue" if code == "TOTAL_REVENUE" else "",
                budget_level="consolidated_regional_budget", actual_ytd_rub=value,
                approved_plan_rub=400, source_id="treasury", source_vintage="fixture"))
    return pd.DataFrame(rows)


def fixture_fiscal():
    rows = []
    for date, total, expenditure in [("2025-03-31", 100, 90), ("2025-06-30", 200, 180),
                                     ("2026-03-31", 125, 110), ("2026-06-30", 300, 310)]:
        for code, value in [("revenue_total", total), ("expenditure_total", expenditure), ("budget_balance", total - expenditure)]:
            rows.append(dict(region_id="R", period=pd.Timestamp(date), metric_code=code, kind="summary",
                             actual_ytd_rub=value, budget_level="consolidated_regional_budget"))
    return pd.DataFrame(rows)


def test_published_production_basis_and_calendar_gaps():
    frame = pd.DataFrame(dict(period=pd.to_datetime(["2026-01-31", "2026-03-31", "2026-03-31"]),
        index_value=[95, 110, 200], okved_section=["TOTAL", "TOTAL", "C"],
        index_measure=["same_month_previous_year_pct"] * 3, frequency=["monthly"] * 3,
        source_id=["s"] * 3, source_vintage=["v"] * 3))
    result = production_history(frame)
    assert len(result) == 3
    assert result.growth_yoy_pct.iloc[0] == -5
    assert pd.isna(result.growth_yoy_pct.iloc[1])
    assert result.growth_yoy_pct.iloc[2] == 10
    assert result.segment.iloc[0] != result.segment.iloc[2]
    with pytest.raises(ValueError, match="unique"):
        production_history(pd.concat([frame, frame.iloc[[0]]]))
    assert production_history(frame.assign(index_measure="same_period_previous_year_pct")).empty


def test_fiscal_scope_same_period_growth_and_transfer_denominator():
    revenues = budget_history(fixture_budget())
    fiscal = fiscal_history(fixture_fiscal())
    period = pd.Timestamp("2026-03-31")
    assert transfer_share(revenues, period) == pytest.approx(16)
    assert transfer_share(revenues, pd.Timestamp("2025-03-31")) == pytest.approx(15)
    assert revenues.loc[revenues.period.eq(period) & revenues.revenue_code.eq(OWN), "growth_yoy_pct"].iloc[0] == pytest.approx(12.5)
    comparison = fiscal_comparison(revenues, fiscal, period)
    assert set(comparison.period.dt.month) == {3}
    assert set(comparison.Year) == {"2025", "2026"}
    assert len(comparison) == 4
    assert fiscal.loc[fiscal.period.eq(period) & fiscal.metric_code.eq("expenditure_total"), "growth_yoy_pct"].iloc[0] == pytest.approx(22.222222)
    wrong = fiscal.copy()
    wrong.loc[wrong.metric_code.eq("revenue_total"), "actual_ytd_rub"] += 1
    with pytest.raises(ValueError, match="disagree"):
        fiscal_comparison(revenues, wrong, period)


def test_missing_values_are_not_zero_or_shortfall_claims():
    revenues = budget_history(fixture_budget())
    assert transfer_share(revenues.loc[~revenues.revenue_code.eq(TRANSFERS)], pd.Timestamp("2026-03-31")) is None
    zero = revenues.copy()
    zero.loc[zero.revenue_code.eq("TOTAL_REVENUE"), "actual_ytd_rub"] = 0
    assert transfer_share(zero, pd.Timestamp("2026-03-31")) is None
    empty = pd.DataFrame()
    assert production_history(None).empty
    assert budget_history(None).empty
    assert fiscal_history(None).empty
    assert fiscal_comparison(empty, empty, None).empty
    assert profile_notes(empty, empty, empty, empty, None) == []
    assert profile_notes(pd.DataFrame({"period": [pd.Timestamp("2026-03-31")], "growth_yoy_pct": [1]}), empty, empty, empty, None) == []
    # Missing previous-year observations remain missing, even with a measured zero.
    assert revenues.loc[revenues.period.dt.year.eq(2025), "growth_yoy_pct"].isna().all()


def test_profile_export_preserves_windows_and_units():
    revenue = budget_history(fixture_budget())
    fiscal = fiscal_history(fixture_fiscal())
    exported = profile_evidence(pd.DataFrame(), revenue, fiscal, None, pd.Timestamp("2025-03-31"))
    assert set(exported.region_id) == {"R"}
    assert set(pd.to_datetime(exported.period)) == {pd.Timestamp("2025-03-31")}
    assert set(exported.units) == {"RUB"}
    assert set(exported.amount_basis) == {"YTD"}
    assert exported.growth_yoy_pct.isna().all()
    assert same_period_history(revenue, pd.Timestamp("2026-06-30")).period.dt.month.eq(6).all()


@pytest.mark.private_data("outputs/regional_revenue_candidate_2026-09-30",
                          "outputs/regional_concordance_2026-09-29_v2/fiscal_preview")
def test_real_regional_profile_consistency():
    budget = pd.read_parquet(ROOT / "outputs/regional_revenue_candidate_2026-09-30/budget_execution.parquet")
    fiscal = pd.read_csv(ROOT / "outputs/regional_concordance_2026-09-29_v2/fiscal_preview/fiscal_observations.csv", dtype={"metric_code": str})
    assert budget.region_id.nunique() == 78
    for region in budget.region_id.unique():
        r = budget_history(budget.loc[budget.region_id.eq(region)])
        f = fiscal_history(fiscal.loc[fiscal.region_id.eq(region)])
        assert len(fiscal_comparison(r, f, pd.Timestamp("2026-06-30"))) == 8
        assert transfer_share(r, pd.Timestamp("2026-06-30")) is not None


@pytest.mark.private_data("outputs/expanded_macro_candidate_2026-09-29_v2",
                          "outputs/regional_revenue_candidate_2026-09-30",
                          "outputs/regional_concordance_2026-09-29_v2/fiscal_preview")
def test_profile_period_switch_and_region_filter(monkeypatch):
    from streamlit.testing.v1 import AppTest
    for variable, folder in {
        "MACRO_RUS_MACRO_CANDIDATE_DIR": "expanded_macro_candidate_2026-09-29_v2",
        "MACRO_RUS_REVENUE_CANDIDATE_DIR": "regional_revenue_candidate_2026-09-30",
        "MACRO_RUS_FISCAL_CANDIDATE_DIR": "regional_concordance_2026-09-29_v2/fiscal_preview",
    }.items():
        monkeypatch.setenv(variable, str(ROOT / "outputs" / folder))
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert not app.exception
    assert len(app.metric) == 4
    assert any(item.value == "Main changes" for item in app.subheader)
    assert any("aggregate YoY is withheld" in item.value for item in app.warning)
    economic = app.metric[1].value
    retained = app.metric[2].value
    next(w for w in app.selectbox if w.label == "Fiscal profile period").set_value(pd.Timestamp("2023-03-31")).run()
    assert not app.exception
    assert app.metric[1].value == economic  # fiscal control does not change the economic date
    assert app.metric[2].value != retained
    assert app.metric[2].delta == ""
    app.sidebar.selectbox[0].set_value("FNS-77").run()
    assert not app.exception
    assert app.metric[1].value != economic
    next(w for w in app.selectbox if w.label == "Fiscal profile period").set_value(pd.Timestamp("2026-06-30")).run()
    assert not app.exception
    assert any("Budget balance" in item.value for item in app.markdown)
