import pandas as pd
import pytest

from macro_rus.metrics import add_ytd_flows, fiscal_execution, industry_contributions


def test_ytd_flow_preserves_negative_correction_and_resets_each_year():
    frame = pd.DataFrame(
        {
            "region_id": ["RU-KLU"] * 4,
            "okved_raw": ["C"] * 4,
            "period": ["2025-03-31", "2025-06-30", "2025-09-30", "2026-03-31"],
            "pit_ytd_rub": [100.0, 150.0, 140.0, 110.0],
        }
    )
    result = add_ytd_flows(
        frame,
        value_column="pit_ytd_rub",
        group_columns=["region_id", "okved_raw"],
        output_column="pit_flow_rub",
    )
    assert result["pit_flow_rub"].tolist() == [100.0, 50.0, -10.0, 110.0]


def test_ytd_flow_does_not_treat_late_first_snapshot_as_period_flow():
    frame = pd.DataFrame(
        {
            "region_id": ["RU-KLU"],
            "okved_raw": ["C"],
            "period": ["2025-12-31"],
            "frequency": ["quarterly"],
            "pit_ytd_rub": [400.0],
        }
    )
    result = add_ytd_flows(
        frame,
        value_column="pit_ytd_rub",
        group_columns=["region_id", "okved_raw"],
        output_column="pit_flow_rub",
    )
    assert pd.isna(result.loc[0, "pit_flow_rub"])


def test_industry_contributions_reconcile_to_total_growth():
    current = pd.DataFrame({"okved_section": ["B", "C"], "pit_ytd_rub": [120, 220]})
    previous = pd.DataFrame({"okved_section": ["B", "C"], "pit_ytd_rub": [100, 200]})
    result = industry_contributions(current, previous)
    assert result["contribution_pp"].sum() == pytest.approx((340 / 300 - 1) * 100)


def test_fiscal_execution_prefers_revised_plan():
    frame = pd.DataFrame(
        {
            "actual_ytd_rub": [50.0, 40.0],
            "approved_plan_rub": [100.0, 100.0],
            "revised_plan_rub": [80.0, None],
        }
    )
    result = fiscal_execution(frame)
    assert result["execution_pct"].tolist() == [62.5, 40.0]
