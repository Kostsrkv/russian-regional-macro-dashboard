"""Filter, export, chart-gap and optional-preview regression checks."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import pytest

from macro_rus.cloud_release import OVERRIDES, resolve_dashboard_config
from macro_rus.fuel_prices import CORE_GRADES, latest_fuel_prices, load_fuel_candidate
from macro_rus.macro_preview import load_macro_candidate
from macro_rus.fuel_view import (
    fuel_chart, fuel_chart_rows, fuel_csv, fuel_date_ticks, fuel_display_table, scoped_fuel_history,
)

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_CANDIDATE = ROOT / "outputs/new_prices_audit_2026-10-05"
CANDIDATE = (PRIVATE_CANDIDATE if PRIVATE_CANDIDATE.is_dir() else
             ROOT / "data/dashboard/releases/2026-10-05-cloud-v1/fuel")


@pytest.fixture(scope="module")
def candidate():
    config = resolve_dashboard_config(ROOT, {})
    macro, _ = load_macro_candidate(config.candidates["macro"])
    ids = list(macro["pit_receipts"].region_id.unique())
    return load_fuel_candidate(CANDIDATE, eligible_region_ids=ids)[0]


def test_same_scope_for_chart_table_and_csv_with_comparison_lineage(candidate):
    history = scoped_fuel_history(candidate, "RU-SVE", ["AI-95"], "2023-04-01")
    exported = pd.read_csv(BytesIO(fuel_csv(history)))
    assert len(history) == len(fuel_chart_rows(history)) == len(fuel_display_table(history)) == len(exported) == 16
    assert set(exported.region_id) == {"RU-SVE"}
    assert set(exported.fuel_grade) == {"AI-95"}
    assert exported.period.max() == "2023-04-01"
    assert exported.period_end.max() == "2023-04-30"
    assert exported.iloc[-1].previous_month_source_cell
    assert exported.iloc[-1].previous_year_source_cell
    assert set(exported.source_file) == {"sred_potreb_cen_2022.xlsx", "sred_potreb_cen_2023.xlsx"}


def test_gap_chart_does_not_connect_across_missing_record_or_missing_price(candidate):
    history = scoped_fuel_history(candidate, "RU-KLU", ["AI-92"], "2022-05-01")
    missing_record = history.loc[~history.period.eq(pd.Timestamp("2022-02-01"))]
    plot = fuel_chart_rows(missing_record)
    assert plot.line_segment.nunique() == 2
    invalid_value = history.copy()
    invalid_value.loc[invalid_value.period.eq(pd.Timestamp("2022-03-01")), "price_rub_per_litre"] = np.nan
    plot = fuel_chart_rows(invalid_value)
    assert plot.line_segment.nunique() == 2
    assert len(plot) == 4 and plot.price_rub_per_litre.notna().all()


def test_optional_missing_is_neither_zero_nor_old_price(candidate):
    history = scoped_fuel_history(candidate, "FNS-49", ["AI-98 and above"], "2026-08-01")
    assert history.price_rub_per_litre.isna().all()
    assert fuel_chart_rows(history).empty
    display = fuel_display_table(history)
    assert display["Price (RUB/litre)"].eq("Unavailable").all()
    assert display["MoM (%)"].eq("Unavailable").all()
    assert display["Review"].eq("Price unavailable").all()
    exported = pd.read_csv(BytesIO(fuel_csv(history)))
    assert exported.price_rub_per_litre.isna().all()


def test_chart_has_explicit_noncolor_encoding_and_month_end_dates(candidate):
    history = scoped_fuel_history(candidate, "RU-KLU", list(CORE_GRADES), "2026-08-01")
    spec = fuel_chart(alt, history).to_dict()
    assert spec["layer"][0]["encoding"]["strokeDash"]["field"] == "fuel_grade"
    assert spec["layer"][0]["encoding"]["detail"]["field"] == "line_segment"
    assert spec["layer"][2]["mark"]["shape"] == "diamond"
    assert spec["layer"][0]["encoding"]["x"]["field"] == "period_end"
    ticks = spec["layer"][0]["encoding"]["x"]["axis"]["values"]
    assert "width < 300" in ticks["expr"]
    assert "width < 1000" in ticks["expr"]
    assert "datetime(2022, 0, 31)" in ticks["expr"]
    assert "datetime(2026, 7, 31)" in ticks["expr"]
    assert spec["layer"][0]["encoding"]["x"]["axis"]["labelOverlap"] is False
    assert spec["layer"][0]["encoding"]["x"]["axis"]["labelFlush"] is True
    assert spec["layer"][0]["encoding"]["x"]["axis"]["labelBound"] is False
    assert len(spec["layer"][0]["encoding"]["color"]["scale"]["range"]) == 3
    assert "Missing months are not connected" in spec["description"]


@pytest.mark.parametrize("budget", range(2, 13))
def test_fuel_date_labels_keep_endpoints_and_spacing(budget):
    start, end = pd.Timestamp("2022-01-31"), pd.Timestamp("2026-08-31")
    ticks = fuel_date_ticks(start, end, budget)
    assert ticks[0] == start and ticks[-1] == end
    assert ticks == sorted(set(ticks)) and len(ticks) <= budget
    assert all(date.is_month_end for date in ticks)
    minimum_gap = (end - start) / (budget - 1) * 0.8
    assert all(right - left >= minimum_gap for left, right in zip(ticks, ticks[1:]))


def test_wide_history_has_half_year_labels_and_mobile_is_sparse():
    wide = fuel_date_ticks("2022-01-31", "2026-08-31", 10)
    assert len(wide) == 10
    assert [date.strftime("%b %Y") for date in wide] == [
        "Jan 2022", "Jul 2022", "Jan 2023", "Jul 2023", "Jan 2024",
        "Jul 2024", "Jan 2025", "Jul 2025", "Jan 2026", "Aug 2026",
    ]
    mobile = fuel_date_ticks("2022-01-31", "2026-08-31", 3)
    assert [date.strftime("%b %Y") for date in mobile] == ["Jan 2022", "Jan 2024", "Aug 2026"]


def test_short_history_shows_months_and_single_month_is_not_duplicated():
    ticks = fuel_date_ticks("2022-01-31", "2022-05-31", 6)
    assert [date.month for date in ticks] == [1, 2, 3, 4, 5]
    assert fuel_date_ticks("2022-01-31", "2022-01-31", 3) == [pd.Timestamp("2022-01-31")]
    with pytest.raises(ValueError):
        fuel_date_ticks("2022-05-31", "2022-01-31", 3)


def test_full_history_flags_reproduce_audited_core_count(candidate):
    flags = 0
    comparisons = 0
    for region in candidate.region_id.unique():
        history = scoped_fuel_history(candidate, region, list(CORE_GRADES), "2026-08-01")
        flags += int(history.mom_review_flag.sum())
        comparisons += int(history.mom_change_pct.notna().sum())
    assert comparisons == 12870 and flags == 120


def preview(monkeypatch, fuel_root=CANDIDATE):
    from streamlit.testing.v1 import AppTest
    for name in ["MACRO_RUS_DATA_DIR", *OVERRIDES.values(), "MACRO_RUS_FUEL_CANDIDATE_DIR"]:
        monkeypatch.delenv(name, raising=False)
    if fuel_root is not None:
        monkeypatch.setenv("MACRO_RUS_FUEL_CANDIDATE_DIR", str(fuel_root))
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=40).run()


def test_fuel_explicit_opt_in_and_three_region_prices(monkeypatch, candidate):
    from streamlit.proto.Metric_pb2 import Metric
    app = preview(monkeypatch)
    assert not app.exception and not app.error
    assert len(app.sidebar.selectbox[0].options) == 78
    app.sidebar.radio[0].set_value("Fuel prices (preview)").run()
    for region in ("RU-KLU", "RU-SVE", "FNS-16"):
        app.sidebar.selectbox[0].set_value(region).run()
        assert not app.exception and not app.error
        expected = latest_fuel_prices(candidate, region)
        assert [item.value for item in app.metric] == [f"{v:.2f}" for v in expected.price_rub_per_litre]
        assert [item.label for item in app.metric] == list(CORE_GRADES)
        assert all(item.color == Metric.GRAY for item in app.metric)
        assert any("31 Aug 2026" in item.value for item in app.caption)
        assert any("not official inflation" in item.value for item in app.info)
        table = next(item.value for item in app.dataframe if "Price (RUB/litre)" in item.value.columns)
        assert len(table) == 56 * 3
        assert table["Observation date"].iloc[-1] == "31 Aug 2026"
    app.sidebar.radio[0].set_value("Data & methodology").run()
    assert not app.exception and not app.error
    assert any(item.value == "Fuel source evidence" for item in app.subheader)


def test_month_grade_filters_early_period_and_missing_optional(monkeypatch):
    app = preview(monkeypatch)
    app.sidebar.radio[0].set_value("Fuel prices (preview)").run()
    month = next(item for item in app.selectbox if item.label == "Fuel reporting month")
    month.set_value(np.datetime64("2022-01-01")).run()
    assert not app.exception
    assert all(item.delta == "" for item in app.metric)
    table = next(item.value for item in app.dataframe if "Price (RUB/litre)" in item.value.columns)
    assert len(table) == 3
    assert table["MoM (%)"].eq("Unavailable").all()
    assert table["YoY (%)"].eq("Unavailable").all()
    app.multiselect[0].set_value([]).run()
    assert not app.exception and not app.metric
    assert any("Select at least one" in item.value for item in app.info)
    app.multiselect[0].set_value(["AI-98 and above"]).run()
    app.sidebar.selectbox[0].set_value("FNS-49").run()
    assert not app.exception and app.metric[0].value == "Unavailable"
    assert any("No zero prices" in item.value for item in app.info)


def test_optional_bad_fuel_root_does_not_break_existing_macro_pages(monkeypatch, tmp_path):
    app = preview(monkeypatch, tmp_path)
    assert not app.exception and not app.error
    app.sidebar.radio[0].set_value("Fuel prices (preview)").run()
    assert not app.exception
    assert any("No fallback prices" in item.value for item in app.error)
    app.sidebar.radio[0].set_value("Fiscal execution").run()
    assert not app.exception and not app.error


def test_default_cloud_navigation_uses_public_fuel_snapshot(monkeypatch):
    app = preview(monkeypatch, None)
    assert not app.exception and not app.error
    assert "Fuel prices (preview)" in app.sidebar.radio[0].options
    config = resolve_dashboard_config(ROOT, {})
    assert config.fuel_root.is_dir()
    assert len(config.render_arguments()) == 6
    app.sidebar.radio[0].set_value("Fuel prices (preview)").run()
    assert not app.exception and not app.error
    assert any("Published research preview" in item.value for item in app.caption)
