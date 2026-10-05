"""Hand-calculated source-shaped fixtures; no network or raw workbook writes."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from macro_rus.fuel_prices import (
    CORE_GRADES, DATASET, MEASURE, PRODUCT_GRADES, fuel_export, fuel_history,
    latest_fuel_prices, load_fuel_candidate, validate_fuel_prices,
)

KLU, SVE = "RU-KLU", "RU-SVE"


def fixture(observations):
    rows = []
    for number, (region, period, grade, value) in enumerate(observations, start=6):
        month = pd.Timestamp(period)
        rows.append(dict(
            region_id=region, period=month, fuel_grade=grade,
            source_geography_code="29000000000" if region == KLU else "65000000000",
            geography_name_ru="Калужская область" if region == KLU else "Свердловская область",
            geography_scope="region", eligible_for_existing_dashboard=True,
            product_code=next(code for code, label in PRODUCT_GRADES.items() if label == grade),
            product_name_ru=f"Source fixture: {grade}, л", price_rub_per_litre=value,
            measurement_basis=MEASURE, source_file=f"source_{month.year}.xlsx",
            source_sha256="a" * 64, source_sheet=month.strftime("%m(%Y)"),
            source_cell=f"B{number}", quality_flag="reported" if value is not None else "missing_or_invalid",
        ))
    return pd.DataFrame(rows)


def complete_fixture():
    return fixture([(region, month, grade, 50.0)
                    for region in (KLU, SVE)
                    for month in ("2026-01-01", "2026-02-01")
                    for grade in PRODUCT_GRADES.values()])


def save_candidate(tmp_path, data=None):
    data = complete_fixture() if data is None else data
    data.to_parquet(tmp_path / DATASET, index=False)
    summary = dict(
        status="candidate_not_promoted", independent_raw_cell_checks=1,
        independent_raw_cell_checks_failed=0, period_start="2026-01-01",
        period_end="2026-02-01", months=2, existing_dashboard_eligible_regions=2,
        eligible_all_fuel_observations_expected=20,
        eligible_core_fuel_observations_expected=12,
        eligible_core_fuel_observations_missing=0,
        eligible_core_fuel_observations_nonpositive=0,
    )
    sources = [dict(filename="source_2026.xlsx", sha256="a" * 64,
                    role="monthly_average_consumer_price_levels")]
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    (tmp_path / "source_manifest.json").write_text(json.dumps(sources))
    return summary


def test_loader_validates_candidate_and_declares_end_month_not_cpi(tmp_path):
    save_candidate(tmp_path)
    data, manifest = load_fuel_candidate(tmp_path, eligible_region_ids=(KLU, SVE))
    assert len(data) == 20
    assert data.period_end.dt.is_month_end.all()
    assert data.price_rub_per_litre.eq(50).all()
    assert manifest["measurement_basis"] == MEASURE
    assert manifest["observation_timing"] == "end_of_reporting_month"
    assert manifest["publication_date"] is None
    assert len(manifest["dataset_sha256"]) == 64
    assert manifest["status"] == "candidate_not_promoted"


def test_excel_numeric_region_code_can_drop_a_leading_zero():
    data = fixture([(KLU, "2026-01-01", "AI-92", 50)])
    data["source_geography_code"] = "3000000000"
    result = validate_fuel_prices(data)
    assert result.source_geography_code.iloc[0] == "3000000000"


@pytest.mark.parametrize("column,value", [
    ("geography_scope", "surveyed_city"), ("geography_scope", "national"),
    ("eligible_for_existing_dashboard", "True"),
    ("measurement_basis", "cpi_yoy"), ("period", pd.Timestamp("2026-01-31")),
    ("period", pd.Timestamp("2026-01-01 12:00")), ("source_sheet", "02(2026)"),
    ("source_geography_code", "29201000000"), ("product_code", "7804"),
    ("source_sha256", "bad"), ("source_file", "../source.xlsx"),
    ("source_cell", "bad"), ("region_id", "unknown"),
])
def test_validation_rejects_scope_semantic_and_lineage_failures(column, value):
    data = fixture([(KLU, "2026-01-01", "AI-92", 50)])
    data[column] = data[column].astype(object)
    data.loc[0, column] = value
    with pytest.raises(ValueError):
        validate_fuel_prices(data, eligible_region_ids=[KLU])


def test_duplicate_grain_blocks_metrics_and_loader(tmp_path):
    data = complete_fixture()
    data = pd.concat([data, data.iloc[[0]]], ignore_index=True)
    save_candidate(tmp_path, data)
    with pytest.raises(ValueError, match="Duplicate"):
        fuel_history(data, KLU)
    with pytest.raises(ValueError, match="Duplicate"):
        load_fuel_candidate(tmp_path, eligible_region_ids=(KLU, SVE))


@pytest.mark.parametrize("price", [0, -1, np.inf, -np.inf, np.nan, True, "invalid"])
def test_core_invalid_prices_are_withheld_and_loader_rejects(price):
    data = fixture([(KLU, "2026-01-01", "AI-92", price)])
    result = validate_fuel_prices(data)
    assert pd.isna(result.price_rub_per_litre.iloc[0])
    assert result.availability.iloc[0] != "reported"
    with pytest.raises(ValueError, match="core fuel"):
        validate_fuel_prices(data, strict_core=True)


@pytest.mark.parametrize("price", [0, -1, np.inf, None, "missing"])
def test_optional_invalid_is_missing_not_zero_with_source_value_preserved(price):
    data = fixture([(KLU, "2026-01-01", "AI-92", 50),
                    (KLU, "2026-01-01", "AI-98 and above", price)])
    first = validate_fuel_prices(data, strict_core=True)
    second = fuel_history(first, KLU)
    optional = second.loc[second.fuel_grade.eq("AI-98 and above")].iloc[0]
    assert pd.isna(optional.price_rub_per_litre)
    assert pd.isna(optional.mom_change_pct)
    if price is not None:
        assert optional.source_price_rub_per_litre == price


def test_hand_calculated_same_calendar_month_and_year_comparisons():
    data = fixture([(KLU, "2025-01-01", "AI-92", 50),
                    (KLU, "2026-01-01", "AI-92", 60),
                    (KLU, "2026-02-01", "AI-92", 66)])
    history = fuel_history(data, KLU)
    january = history.loc[history.period.eq(pd.Timestamp("2026-01-01"))].iloc[0]
    february = history.loc[history.period.eq(pd.Timestamp("2026-02-01"))].iloc[0]
    assert january.yoy_change_pct == pytest.approx(20)
    assert january.previous_year_price_rub_per_litre == 50
    assert january.previous_year_period_end == pd.Timestamp("2025-01-31")
    assert pd.isna(january.mom_change_pct)
    assert january.mom_status == "prior_period_unavailable"
    assert february.mom_change_pct == pytest.approx(10)
    assert february.previous_month_price_rub_per_litre == 60
    assert february.previous_month_period_end == pd.Timestamp("2026-01-31")
    assert february.mom_status == "matched"
    assert not february.mom_review_flag  # strictly above 10%, not >=10%.
    assert pd.isna(february.yoy_change_pct)


def test_gap_is_not_comparison_to_previous_surviving_record():
    history = fuel_history(fixture([(KLU, "2026-01-01", "AI-92", 50),
                                    (KLU, "2026-03-01", "AI-92", 60)]), KLU)
    march = history.iloc[-1]
    assert pd.isna(march.mom_change_pct)
    assert pd.isna(march.previous_month_price_rub_per_litre)
    assert march.expected_previous_month_period == pd.Timestamp("2026-02-01")
    assert pd.isna(march.previous_month_period)
    assert len(history) == 2  # no interpolated February row.


def test_missing_prior_value_has_distinct_status_and_no_change():
    history = fuel_history(fixture([(KLU, "2026-01-01", "AI-95", None),
                                    (KLU, "2026-02-01", "AI-95", 60)]), KLU)
    february = history.iloc[-1]
    assert february.previous_month_period == pd.Timestamp("2026-01-01")
    assert pd.isna(february.mom_change_pct)
    assert february.mom_status == "prior_price_unavailable"


def test_grade_comparisons_do_not_cross_join_different_products():
    history = fuel_history(fixture([(KLU, "2026-01-01", "AI-92", 50),
                                    (KLU, "2026-01-01", "AI-95", 100),
                                    (KLU, "2026-02-01", "AI-95", 110)]), KLU)
    assert history.iloc[-1].mom_change_pct == pytest.approx(10)
    assert history.iloc[-1].previous_month_price_rub_per_litre == 100


@pytest.mark.parametrize("current,expected", [(60, 20), (40, -20)])
def test_large_monthly_movements_are_preserved_and_flagged(current, expected):
    history = fuel_history(fixture([(KLU, "2026-01-01", "Diesel", 50),
                                    (KLU, "2026-02-01", "Diesel", current)]), KLU)
    row = history.iloc[-1]
    assert row.price_rub_per_litre == current
    assert row.mom_change_pct == expected
    assert row.mom_review_flag
    assert row.review_status == "review_large_monthly_change"


def test_latest_is_regional_reporting_month_not_global_or_last_valid_grade():
    data = fixture([(KLU, "2026-01-01", "AI-98 and above", 60),
                    (KLU, "2026-02-01", "AI-92", 50),
                    (SVE, "2027-01-01", "AI-92", 70)])
    latest = latest_fuel_prices(data, KLU, grades=("AI-92", "AI-98 and above"))
    assert latest.period.eq(pd.Timestamp("2026-02-01")).all()
    assert latest.period_end.eq(pd.Timestamp("2026-02-28")).all()
    absent = latest.loc[latest.fuel_grade.eq("AI-98 and above")].iloc[0]
    assert pd.isna(absent.price_rub_per_litre)
    assert pd.isna(absent.source_cell)
    assert not absent.observation_present
    assert absent.availability == "no_observation_for_reporting_month"


def test_explicit_period_and_export_keep_filter_scope_and_comparison_lineage():
    data = fixture([(KLU, "2026-01-01", "AI-92", 50),
                    (KLU, "2026-02-01", "AI-92", 60),
                    (SVE, "2026-02-01", "AI-92", 200)])
    latest = latest_fuel_prices(data, KLU, period="2026-01-01", grades=["AI-92"])
    assert latest.price_rub_per_litre.iloc[0] == 50
    exported = fuel_export(data, KLU, grades=["AI-92"])
    assert set(exported.region_id) == {KLU}
    assert len(exported) == 2
    assert exported.iloc[-1].previous_month_source_cell == "B6"
    assert exported.iloc[-1].source_cell == "B7"
    pd.testing.assert_frame_equal(exported, fuel_history(data, KLU, grades=["AI-92"]))
    pd.testing.assert_frame_equal(exported, fuel_history(data[data.region_id.eq(KLU)], KLU, grades=["AI-92"]))


def test_loader_blocks_dropped_core_calendar_cells_and_source_hash_disagreement(tmp_path):
    data = complete_fixture().drop(index=0)
    save_candidate(tmp_path, data)
    with pytest.raises(ValueError, match="coverage"):
        load_fuel_candidate(tmp_path, eligible_region_ids=(KLU, SVE))
    data = complete_fixture()
    data.loc[0, "source_sha256"] = "b" * 64
    save_candidate(tmp_path, data)
    with pytest.raises(ValueError, match="hashes"):
        load_fuel_candidate(tmp_path, eligible_region_ids=(KLU, SVE))


def test_unknown_region_empty_data_and_grade_are_explicit():
    data = fixture([(KLU, "2026-01-01", "AI-92", 50)])
    assert fuel_history(data, "uncovered").empty
    assert latest_fuel_prices(data, "uncovered").empty
    assert fuel_history(pd.DataFrame(), KLU).empty
    with pytest.raises(ValueError, match="Unknown fuel grade"):
        fuel_history(data, KLU, grades=["fake"])
