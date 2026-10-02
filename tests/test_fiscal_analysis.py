import json

import numpy as np
import pandas as pd
import pytest

from macro_rus.fiscal_analysis import (
    SCOPE, budget_balance_bridge, derive_fiscal_flows,
    spending_function_history, transfer_history,
)
from macro_rus.fiscal_view import FUNCTIONS
from macro_rus.revenue_view import OWN, TRANSFERS


def observation(period, code, amount, *, kind="summary", region="A"):
    date = pd.Timestamp(period)
    return {"region_id": region, "period": date, "kind": kind,
            "metric_code": code, "actual_ytd_rub": amount,
            "budget_level": SCOPE, "units": "RUB", "frequency": "YTD",
            "source_file": f"{date:%Y%m%d}.zip", "source_sha256": f"hash-{date:%Y%m%d}",
            "source_member": f"{region}.html", "reporting_cutoff": (date + pd.DateOffset(days=1)).strftime("%Y-%m-%d")}


@pytest.fixture
def fiscal():
    rows = []
    for region, multiplier in (("A", 1), ("B", 2)):
        for period, revenue, expenditure in (
            ("2023-03-31", 100, 80), ("2023-06-30", 180, 160),
            ("2024-03-31", 120, 100), ("2024-06-30", 210, 200),
        ):
            for code, amount in (("revenue_total", revenue), ("expenditure_total", expenditure),
                                 ("budget_balance", revenue - expenditure)):
                rows.append(observation(period, code, amount * multiplier, region=region))
            for i, code in enumerate(FUNCTIONS):
                amount = expenditure * (.35 if i == 0 else .05)
                rows.append(observation(period, code, amount * multiplier, kind="expenditure_function", region=region))
    return pd.DataFrame(rows)


@pytest.fixture
def revenue():
    rows = []
    # Transfers (202) deliberately differ from the other non-repayable code.
    for region in ("A", "B"):
        for period, total, own, transfers in (
            ("2023-03-31", 100, 60, 30), ("2023-06-30", 200, 130, 50),
            ("2024-03-31", 120, 80, 25), ("2024-06-30", 210, 150, 45),
        ):
            for code, value in (("TOTAL_REVENUE", total), (OWN, own), (TRANSFERS, transfers), ("20000000000000000", total - own)):
                row = observation(period, code, value, region=region)
                row["revenue_code"] = row.pop("metric_code")
                row["frequency"] = "quarterly"
                rows.append(row)
    return pd.DataFrame(rows)


def test_q2_hand_calculation_and_both_endpoint_evidence(fiscal):
    result = derive_fiscal_flows(fiscal)
    row = result.loc[result.region_id.eq("A") & result.period.eq("2024-06-30") & result.metric_code.eq("budget_balance")].iloc[0]
    assert row.quarter_flow_rub == -10  # (210 - 200) - (120 - 100)
    assert row.start_actual_ytd_rub == 20
    evidence = json.loads(row.flow_source_evidence)
    assert evidence["start"]["source_sha256"] == "hash-20240331"
    assert evidence["end"]["source_member"] == "A.html"
    assert evidence["start"]["reporting_cutoff"] == "2024-04-01"
    assert evidence["end"]["reporting_cutoff"] == "2024-07-01"
    first = result.loc[result.region_id.eq("A") & result.period.eq("2024-03-31") & result.metric_code.eq("budget_balance")].iloc[0]
    assert first.quarter_flow_rub == first.actual_ytd_rub
    assert json.loads(first.flow_source_evidence)["start"] is None
    assert "quarter_flow_rub" not in fiscal


def test_missing_nonadjacent_and_different_kind_endpoints(fiscal):
    selected = fiscal.loc[fiscal.region_id.eq("A") & fiscal.metric_code.eq("revenue_total")].copy()
    selected = selected.loc[~selected.period.eq("2024-03-31")]
    selected = pd.concat([selected, pd.DataFrame([observation("2024-01-31", "revenue_total", 10)])], ignore_index=True)
    result = derive_fiscal_flows(selected)
    current = result.loc[result.period.eq("2024-06-30")].iloc[0]
    assert pd.isna(current.quarter_flow_rub)
    assert current.flow_status == "missing endpoint"
    changed_kind = fiscal.loc[fiscal.region_id.eq("A") & fiscal.metric_code.eq("0100")].copy()
    changed_kind.loc[changed_kind.period.eq("2024-03-31"), "kind"] = "summary"
    assert derive_fiscal_flows(changed_kind).loc[lambda x: x.period.eq("2024-06-30"), "quarter_flow_rub"].isna().all()


def test_missing_amount_and_negative_adjustment_survive(fiscal):
    data = fiscal.loc[fiscal.region_id.eq("A") & fiscal.metric_code.eq("0100")].copy()
    data.loc[data.period.eq("2024-06-30"), "actual_ytd_rub"] = 27
    row = derive_fiscal_flows(data).loc[lambda x: x.period.eq("2024-06-30")].iloc[0]
    assert row.quarter_flow_rub == -8
    data.loc[data.period.eq("2024-03-31"), "actual_ytd_rub"] = np.nan
    assert derive_fiscal_flows(data).loc[lambda x: x.period.eq("2024-06-30"), "quarter_flow_rub"].isna().all()


@pytest.mark.parametrize("mutation", ["scope", "duplicate", "units"])
def test_invalid_grain_or_scope_rejected(fiscal, mutation):
    data = fiscal.copy()
    if mutation == "scope":
        data.loc[0, "budget_level"] = "federal_budget"
    elif mutation == "units":
        data.loc[0, "units"] = "thousand RUB"
    else:
        data = pd.concat([data, data.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError):
        derive_fiscal_flows(data)


def test_function_change_contributions_and_all_14_shares(fiscal):
    result = spending_function_history(fiscal, "period_flow", "A")
    current = result.loc[result.period.eq("2024-06-30")]
    assert len(current) == 14
    assert set(current.metric_code) == set(FUNCTIONS)
    assert current.amount_rub.sum() == 100
    assert current.share_pct.sum() == pytest.approx(100)
    assert current.contribution_to_spending_change_rub.sum() == pytest.approx(20)
    assert current.contribution_to_spending_growth_pp.sum() == pytest.approx(25)
    assert current.share_change_pp.sum() == pytest.approx(0)
    assert current.comparison_complete.all()
    first = current.loc[current.metric_code.eq("0100")].iloc[0]
    assert first.change_yoy_rub == 7
    assert first.growth_yoy_pct == 25
    assert first.share_pct == 35
    assert json.loads(first.total_source_evidence)["start"]["source_sha256"] == "hash-20240331"
    assert set(result.region_id) == {"A"}


def test_missing_function_is_visible_and_not_zero(fiscal):
    data = fiscal.loc[~(fiscal.region_id.eq("A") & fiscal.period.eq("2024-06-30") & fiscal.metric_code.eq("0200"))]
    result = spending_function_history(data, "YTD", "A")
    current = result.loc[result.period.eq("2024-06-30")]
    assert len(current) == 14
    assert pd.isna(current.loc[current.metric_code.eq("0200"), "amount_rub"]).all()
    assert not current.functions_complete.any()
    assert not current.comparison_complete.any()
    assert current.change_reconciliation_gap_rub.isna().all()


def test_missing_prior_total_keeps_function_levels_but_withholds_rates(fiscal):
    data = fiscal.loc[~(fiscal.region_id.eq("A") & fiscal.period.eq("2023-06-30") & fiscal.metric_code.eq("expenditure_total"))]
    result = spending_function_history(data, "YTD", "A")
    prior = result.loc[result.period.eq("2023-06-30")]
    current = result.loc[result.period.eq("2024-06-30")]
    assert len(prior) == 14
    assert prior.amount_rub.sum() == 160
    assert prior.share_pct.isna().all()
    assert current.prior_amount_rub.notna().all()
    assert current.share_change_pp.isna().all()
    assert current.contribution_to_spending_growth_pp.isna().all()
    assert not current.comparison_complete.any()


def test_signed_function_contributions_reconcile_after_mix_shift(fiscal):
    data = fiscal.copy()
    mask = data.region_id.eq("A") & data.period.eq("2024-06-30")
    data.loc[mask & data.metric_code.eq("0100"), "actual_ytd_rub"] -= 20
    data.loc[mask & data.metric_code.eq("0200"), "actual_ytd_rub"] += 20
    current = spending_function_history(data, "YTD", "A").loc[lambda x: x.period.eq("2024-06-30")]
    assert current.loc[current.metric_code.eq("0100"), "contribution_to_spending_change_rub"].iloc[0] == -6
    assert current.loc[current.metric_code.eq("0200"), "contribution_to_spending_change_rub"].iloc[0] == 22
    assert current.contribution_to_spending_change_rub.sum() == 40
    assert current.share_change_pp.sum() == pytest.approx(0)


def test_missing_or_zero_prior_denominator(fiscal, revenue):
    first = spending_function_history(fiscal, "YTD", "A").loc[lambda x: x.period.eq("2023-03-31")]
    assert first.growth_yoy_pct.isna().all()
    assert first.share_change_pp.isna().all()
    data = fiscal.copy()
    data.loc[data.period.eq("2023-03-31") & (data.kind.eq("expenditure_function") | data.metric_code.eq("expenditure_total")), "actual_ytd_rub"] = 0
    current = spending_function_history(data, "YTD", "A").loc[lambda x: x.period.eq("2024-03-31")]
    assert current.growth_yoy_pct.isna().all()
    assert current.contribution_to_spending_growth_pp.isna().all()
    assert current.contribution_to_spending_change_rub.sum() == 100
    revenue.loc[revenue.period.eq("2024-06-30") & revenue.revenue_code.eq("TOTAL_REVENUE"), "actual_ytd_rub"] = 0
    assert transfer_history(revenue).loc[lambda x: x.period.eq("2024-06-30"), "transfer_share_pct"].isna().all()


@pytest.mark.parametrize("basis, expected", [("YTD", (20, 10, 30, 40)), ("period_flow", (0, -10, 10, 20))])
def test_balance_bridge_reconciles_and_filters_region(fiscal, basis, expected):
    result, reason = budget_balance_bridge(fiscal, "A", "2024-06-30", basis)
    start, end, dr, de = expected
    assert reason is None
    assert set(result.region_id) == {"A"}
    assert result.value_rub.tolist() == [start, dr, -de, end]
    assert result.iloc[2].to_rub == end
    assert start + dr - de == end
    assert result.reconciliation_gap_rub.abs().max() == 0
    other, _ = budget_balance_bridge(fiscal, "B", "2024-06-30", basis)
    assert other.iloc[-1].value_rub == 2 * end


def test_bridge_unavailable_prior_and_invalid_endpoint(fiscal):
    result, reason = budget_balance_bridge(fiscal, "A", "2023-06-30", "YTD")
    assert result.empty and "Prior-year" in reason
    missing = fiscal.loc[~(fiscal.region_id.eq("A") & fiscal.period.eq("2023-03-31"))]
    result, reason = budget_balance_bridge(missing, "A", "2024-06-30", "period_flow")
    assert result.empty and "Prior-year" in reason
    bad = fiscal.copy()
    bad.loc[bad.region_id.eq("A") & bad.period.eq("2024-06-30") & bad.metric_code.eq("budget_balance"), "actual_ytd_rub"] += 1
    with pytest.raises(ValueError, match="balance"):
        budget_balance_bridge(bad, "A", "2024-06-30")


def test_transfer_exact_202_and_same_window_own_source(revenue):
    result = transfer_history(revenue, "period_flow", "A")
    row = result.loc[result.period.eq("2024-06-30")].iloc[0]
    assert row.transfers_rub == 20
    assert row.own_source_rub == 70
    assert row.total_revenue_rub == 90
    assert row.transfer_share_pct == pytest.approx(20 / 90 * 100)
    assert row.prior_transfers_rub == 20
    assert row.transfers_change_yoy_rub == 0
    assert row.own_source_change_yoy_rub == 0
    assert row.transfer_share_change_pp == pytest.approx(20 / 90 * 100 - 20)
    assert set(result.region_id) == {"A"}
    assert json.loads(row.transfers_source_evidence)["start"]["source_sha256"] == "hash-20240331"


def test_missing_transfer_preserves_missing(revenue):
    data = revenue.loc[~(revenue.region_id.eq("A") & revenue.period.eq("2024-03-31") & revenue.revenue_code.eq(TRANSFERS))]
    row = transfer_history(data, "period_flow", "A").loc[lambda x: x.period.eq("2024-06-30")].iloc[0]
    assert pd.isna(row.transfers_rub)
    assert pd.isna(row.transfer_share_pct)
    assert row.own_source_rub == 70


def test_revenue_duplicates_or_wrongscope_rejected(revenue):
    with pytest.raises(ValueError):
        transfer_history(pd.concat([revenue, revenue.iloc[[0]]]))
    data = revenue.copy()
    data.loc[0, "budget_level"] = "territorial_health_insurance"
    with pytest.raises(ValueError):
        transfer_history(data)
