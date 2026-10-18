import json

import numpy as np
import pandas as pd
import pytest

from macro_rus.fiscal_analysis import SCOPE
from macro_rus.fiscal_diagnostics import COMPONENTS, detailed_balance_bridge, revenue_component_history
from macro_rus.revenue_view import CIT, NON_REPAYABLE, OWN, PIT, TRANSFERS


def observation(region, period, code, amount, revenue=False):
    date = pd.Timestamp(period)
    row = {"region_id": region, "period": date, "budget_level": SCOPE,
           "actual_ytd_rub": amount, "units": "RUB", "frequency": "YTD",
           "source_file": f"{date:%Y%m%d}.zip", "source_sha256": f"hash-{date:%Y%m%d}",
           "source_member": f"{region}.html", "reporting_cutoff": f"{date + pd.Timedelta(days=1):%Y-%m-%d}"}
    if revenue:
        row["revenue_code"] = code
    else:
        row.update(kind="summary", metric_code=code)
    return row


@pytest.fixture
def inputs():
    fiscal, revenue = [], []
    observations = [
        ("2023-03-31", 100, 60, 30, 20, 35, 80),
        ("2023-06-30", 220, 150, 80, 40, 60, 200),
        ("2024-03-31", 120, 80, 45, 25, 30, 110),
        ("2024-06-30", 250, 180, 100, 50, 65, 240),
    ]
    for region, multiplier in (("A", 1), ("B", 2)):
        for period, total, own, pit, cit, transfers, spending in observations:
            for code, value in (("TOTAL_REVENUE", total), (OWN, own), (PIT, pit),
                                (CIT, cit), (NON_REPAYABLE, total - own), (TRANSFERS, transfers)):
                revenue.append(observation(region, period, code, value * multiplier, True))
            for code, value in (("revenue_total", total), ("expenditure_total", spending),
                                ("budget_balance", total - spending)):
                fiscal.append(observation(region, period, code, value * multiplier))
    return pd.DataFrame(fiscal), pd.DataFrame(revenue)


@pytest.mark.parametrize("basis, expected", [("YTD", [20, 10, 0, 5, -5]),
                                             ("period_flow", [5, 5, 0, 10, -10])])
def test_hand_worked_revenue_contributions(inputs, basis, expected):
    _, revenue = inputs
    original = revenue.copy(deep=True)
    history = revenue_component_history(revenue, basis, "A")
    current = history.loc[history.period.eq("2024-06-30")]
    assert current.component_code.tolist() == list(COMPONENTS)
    assert current.change_yoy_rub.tolist() == expected
    assert current.change_yoy_rub.sum() == current.total_revenue_change_rub.iloc[0]
    assert current.share_pct.sum() == pytest.approx(100)
    assert current.comparison_complete.all()
    prior_total = 220 if basis == "YTD" else 120
    assert current.contribution_to_revenue_growth_pp.sum() == pytest.approx(sum(expected) / prior_total * 100)
    pd.testing.assert_frame_equal(revenue, original)


def test_signed_residuals_and_endpoints_survive(inputs):
    _, revenue = inputs
    result = revenue_component_history(revenue, "period_flow", "A")
    row = result.loc[result.period.eq("2024-06-30") & result.component_code.eq("other_non_repayable")].iloc[0]
    assert row.amount_rub == -5
    assert row.negative_amount_flag
    assert row.component_basis == "derived_residual"
    evidence = json.loads(row.source_evidence)
    assert set(evidence) == {NON_REPAYABLE, TRANSFERS}
    assert evidence[TRANSFERS]["start"]["source_sha256"] == "hash-20240331"
    assert evidence[TRANSFERS]["end"]["reporting_cutoff"] == "2024-07-01"


@pytest.mark.parametrize("basis, balance_change", [("YTD", -10), ("period_flow", 0)])
def test_detailed_balance_and_region_filter(inputs, basis, balance_change):
    fiscal, revenue = inputs
    result, reason = detailed_balance_bridge(fiscal, revenue, "A", "2024-06-30", basis)
    assert reason is None
    assert len(result) == 8
    assert result.region_id.eq("A").all()
    assert result.role.eq("change").sum() == 6
    assert result.loc[result.role.eq("change"), "value_rub"].sum() == pytest.approx(balance_change)
    assert result.iloc[-2].to_rub == pytest.approx(result.end_balance_rub.iloc[0])
    other, reason = detailed_balance_bridge(fiscal, revenue, "B", "2024-06-30", basis)
    assert reason is None
    assert other.end_balance_rub.iloc[0] == 2 * result.end_balance_rub.iloc[0]


def test_missing_transfer_is_not_zero_or_reconstructed(inputs):
    fiscal, revenue = inputs
    missing = revenue.loc[~(revenue.region_id.eq("A") & revenue.period.eq("2024-06-30") & revenue.revenue_code.eq(TRANSFERS))]
    result = revenue_component_history(missing, "YTD", "A")
    current = result.loc[result.period.eq("2024-06-30")]
    assert current.loc[current.component_code.isin(["transfers", "other_non_repayable"]), "amount_rub"].isna().all()
    assert not current.comparison_complete.any()
    bridge, reason = detailed_balance_bridge(fiscal, missing, "A", "2024-06-30")
    assert bridge.empty and "unavailable" in reason


def test_missing_q1_endpoint_withholds_q2(inputs):
    fiscal, revenue = inputs
    missing = revenue.loc[~(revenue.region_id.eq("A") & revenue.period.eq("2024-03-31") & revenue.revenue_code.eq(PIT))]
    result = revenue_component_history(missing, "period_flow", "A")
    current = result.loc[result.period.eq("2024-06-30")]
    assert current.loc[current.component_code.isin(["retained_pit", "other_own"]), "amount_rub"].isna().all()
    assert current.loc[current.component_code.eq("cit"), "amount_rub"].iloc[0] == 25
    assert detailed_balance_bridge(fiscal, missing, "A", "2024-06-30", "period_flow")[0].empty


def test_no_prior_year_preserves_levels(inputs):
    fiscal, revenue = inputs
    result = revenue_component_history(revenue, "YTD", "A")
    current = result.loc[result.period.eq("2023-06-30")]
    assert current.amount_rub.notna().all()
    assert current.change_yoy_rub.isna().all()
    assert current.contribution_to_revenue_growth_pp.isna().all()
    bridge, reason = detailed_balance_bridge(fiscal, revenue, "A", "2023-06-30")
    assert bridge.empty and "Prior-year" in reason


@pytest.mark.parametrize("field", ["source_sha256", "source_member", "reporting_cutoff"])
def test_same_totals_different_provenance_rejected(inputs, field):
    fiscal, revenue = inputs
    changed = revenue.copy()
    selected = changed.region_id.eq("A") & changed.period.eq("2024-03-31") & changed.revenue_code.eq("TOTAL_REVENUE")
    changed.loc[selected, field] = "different"
    with pytest.raises(ValueError, match="provenance"):
        detailed_balance_bridge(fiscal, changed, "A", "2024-06-30", "period_flow")


def test_mixed_component_vintages_rejected(inputs):
    _, revenue = inputs
    changed = revenue.copy()
    changed.loc[changed.region_id.eq("A") & changed.period.eq("2024-06-30") & changed.revenue_code.eq(PIT), "source_sha256"] = "other-vintage"
    with pytest.raises(ValueError, match="mixed source provenance"):
        revenue_component_history(changed)


def test_cross_source_total_mismatch_rejected(inputs):
    fiscal, revenue = inputs
    changed = fiscal.copy()
    current = changed.region_id.eq("A") & changed.period.eq("2024-06-30")
    changed.loc[current & changed.metric_code.isin(["revenue_total", "budget_balance"]), "actual_ytd_rub"] += 1
    with pytest.raises(ValueError, match="source totals"):
        detailed_balance_bridge(changed, revenue, "A", "2024-06-30")


@pytest.mark.parametrize("mutation", ["duplicate", "units", "scope", "parent_total"])
def test_invalid_inputs_rejected(inputs, mutation):
    _, revenue = inputs
    changed = revenue.copy()
    if mutation == "duplicate":
        changed = pd.concat([changed, changed.iloc[[0]]], ignore_index=True)
    elif mutation == "units":
        changed.loc[0, "units"] = "thousand RUB"
    elif mutation == "scope":
        changed.loc[0, "budget_level"] = "federal_budget"
    else:
        changed.loc[0, "actual_ytd_rub"] += 1
    with pytest.raises(ValueError):
        revenue_component_history(changed)


def test_zero_and_negative_prior_denominators(inputs):
    _, revenue = inputs
    changed = revenue.copy()
    at_prior = changed.region_id.eq("A") & changed.period.eq("2023-06-30")
    changed.loc[at_prior & changed.revenue_code.eq(PIT), "actual_ytd_rub"] = 0
    result = revenue_component_history(changed, "YTD", "A")
    assert pd.isna(result.loc[result.period.eq("2024-06-30") & result.component_code.eq("retained_pit"), "growth_yoy_pct"].iloc[0])
    changed.loc[at_prior & changed.revenue_code.eq(PIT), "actual_ytd_rub"] = -1
    result = revenue_component_history(changed, "YTD", "A")
    assert pd.isna(result.loc[result.period.eq("2024-06-30") & result.component_code.eq("retained_pit"), "growth_yoy_pct"].iloc[0])


def test_missing_value_and_unsupported_period(inputs):
    _, revenue = inputs
    changed = revenue.copy()
    changed.loc[changed.region_id.eq("A") & changed.period.eq("2024-06-30") & changed.revenue_code.eq(CIT), "actual_ytd_rub"] = np.nan
    result = revenue_component_history(changed, "YTD", "A")
    assert result.loc[result.period.eq("2024-06-30") & result.component_code.eq("cit"), "amount_rub"].isna().all()
    off_calendar = revenue.loc[revenue.period.eq("2024-06-30")].copy()
    off_calendar["period"] = pd.Timestamp("2024-05-31")
    result = revenue_component_history(off_calendar)
    assert result.amount_rub.isna().all()


def test_empty_region(inputs):
    _, revenue = inputs
    assert revenue_component_history(revenue, region_id="unavailable").empty


class CapturedUI:
    def __init__(self):
        self.charts, self.tables, self.downloads, self.messages = [], [], [], []

    def subheader(self, *args):
        pass

    def caption(self, *args):
        pass

    def info(self, text):
        self.messages.append(text)

    def altair_chart(self, chart, **kwargs):
        self.charts.append(chart.to_dict())

    def dataframe(self, frame, **kwargs):
        self.tables.append(frame)

    def expander(self, *args):
        from contextlib import nullcontext
        return nullcontext()

    def download_button(self, label, data, **kwargs):
        self.downloads.append((kwargs["file_name"], data))


@pytest.mark.parametrize("region, basis", [("A", "YTD"), ("B", "YTD"), ("A", "period_flow")])
def test_rendered_chart_table_and_downloads_share_filters(inputs, region, basis):
    import altair as alt
    from io import BytesIO
    from macro_rus.fiscal_diagnostics import render_revenue_diagnostics
    fiscal, revenue = inputs
    ui = CapturedUI()
    render_revenue_diagnostics(ui, alt, fiscal, revenue, region, "2024-06-30", basis)
    assert len(ui.charts) == 1 and len(ui.tables) == 1 and len(ui.downloads) == 2
    assert not ui.messages
    datasets = ui.charts[0]["datasets"].values()
    plotted = next(rows for rows in datasets if rows and "component_code" in rows[0])
    assert len(plotted) == 5
    assert {row["region_id"] for row in plotted} == {region}
    assert {row["amount_basis"] for row in plotted} == {basis}
    for name, content in ui.downloads:
        exported = pd.read_csv(BytesIO(content))
        assert exported.region_id.eq(region).all()
        assert exported.amount_basis.eq(basis).all()
        assert exported.period.eq("2024-06-30").all()
        assert "source_evidence" in exported
        assert region in name and basis in name
    assert ui.tables[0]["Change (RUB bn)"].str.match(r"[+-]").all()


def test_rendered_unavailable_period_is_not_zero_chart(inputs):
    import altair as alt
    from macro_rus.fiscal_diagnostics import render_revenue_diagnostics
    fiscal, revenue = inputs
    ui = CapturedUI()
    render_revenue_diagnostics(ui, alt, fiscal, revenue, "A", "2023-06-30", "YTD")
    assert not ui.charts and not ui.downloads
    assert "not treated as zero" in ui.messages[0]
