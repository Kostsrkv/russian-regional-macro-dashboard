"""Opt-in local review of reconciled Treasury fiscal observations."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .provenance import sha256_file

FUNCTIONS = dict(zip(
    [f"{i:02d}00" for i in range(1, 15)],
    ["General public services", "National defence", "Security and law enforcement",
     "Economic affairs", "Housing and utilities", "Environmental protection",
     "Education", "Culture and cinematography", "Health", "Social policy",
     "Physical culture and sport", "Mass media", "Debt service",
     "General-purpose interbudgetary transfers"],
))


def load_candidate(root: Path) -> tuple[pd.DataFrame, dict]:
    """Fail closed on malformed or unreconciled actuals; never fill missing values."""
    manifest = json.loads((root / "manifest.json").read_text())
    observations_file = manifest.get("observations_file", "fiscal_observations.csv")
    if observations_file not in {"fiscal_observations.csv", "fiscal_observations.csv.gz"}:
        raise ValueError("Unrecognized fiscal observations file.")
    if (manifest.get("observations_sha256")
            and sha256_file(root / observations_file) != manifest["observations_sha256"]):
        raise ValueError("Fiscal observations checksum mismatch.")
    data = pd.read_csv(root / observations_file, dtype={"metric_code": str})
    required = {"region_id", "period", "kind", "metric_code", "actual_ytd_rub",
                "units", "frequency", "budget_level", "source_file", "source_sha256",
                "source_member", "reporting_cutoff", "name_ru"}
    if not required.issubset(data.columns):
        raise ValueError("Fiscal candidate is missing required fields.")
    if manifest.get("status") != "candidate_not_promoted" or manifest.get("blocking_failures") != 0:
        raise ValueError("Fiscal candidate has not passed its blocking checks.")
    if len(data) != manifest.get("rows") or data.duplicated(["region_id", "period", "kind", "metric_code"]).any():
        raise ValueError("Fiscal candidate row count or observation grain is invalid.")
    for column, value in {"units": "RUB", "frequency": "YTD", "budget_level": "consolidated_regional_budget"}.items():
        if not data[column].eq(value).all():
            raise ValueError(f"Unexpected fiscal {column}.")
    data["period"] = pd.to_datetime(data["period"], errors="raise")
    if data["period"].isna().any() or not np.isfinite(data["actual_ytd_rub"]).all():
        raise ValueError("Missing or non-finite fiscal values.")
    sources = {s["source_file"]: s["sha256"] for s in manifest["sources"] if s["selected_html_crc_passed"]}
    if not data["source_file"].map(sources).eq(data["source_sha256"]).all():
        raise ValueError("Fiscal provenance does not match the candidate manifest.")
    for _, group in data.groupby(["region_id", "period"]):
        totals = group[group.kind.eq("summary")].set_index("metric_code")["actual_ytd_rub"]
        expected = {"revenue_total", "expenditure_total", "budget_balance", "financing_total"}
        if not expected.issubset(totals.index):
            raise ValueError("Missing fiscal summary totals.")
        gaps = [totals.revenue_total - totals.expenditure_total - totals.budget_balance,
                totals.budget_balance + totals.financing_total,
                group.loc[group.kind.eq("expenditure_function"), "actual_ytd_rub"].sum(min_count=1) - totals.expenditure_total]
        if any(not np.isfinite(gap) or abs(gap) > .02 for gap in gaps):
            raise ValueError("Fiscal actuals no longer reconcile; rebuild the candidate.")
    return data, manifest


def period_label(period) -> str:
    date = pd.Timestamp(period)
    return f"Jan–{date:%b %Y} (cumulative)"


def regional_snapshot(data, region_id, period):
    return data.loc[data.region_id.eq(region_id) & data.period.eq(pd.Timestamp(period))].copy()


def render_fiscal_preview(st, alt, root: Path, region_id: str) -> None:
    st.subheader("Spending & financing")
    st.info("Research review dataset. Actuals only; budget-plan discrepancies remain under review.")
    try:
        data, manifest = load_candidate(root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        st.error(f"Fiscal preview unavailable: {exc}")
        return
    regional = data[data.region_id.eq(region_id)]
    if regional.empty:
        st.info("No fiscal candidate observations for this region.")
        return
    period = st.selectbox("Reporting period", sorted(regional.period.unique(), reverse=True), format_func=period_label)
    selected = regional_snapshot(data, region_id, period)
    totals = selected[selected.kind.eq("summary")].set_index("metric_code").actual_ytd_rub
    st.caption(f"{period_label(period)} · nominal RUB · regional + municipal budgets, excluding territorial health-insurance funds")
    for column, label, code in zip(st.columns(3), ["Revenue", "Expenditure", "Budget balance"],
                                   ["revenue_total", "expenditure_total", "budget_balance"]):
        column.metric(label, f"{totals[code] / 1e9:,.2f} bn RUB")
    st.caption("Balance = revenue minus expenditure. A negative balance is a deficit, not by itself a revenue shortfall against plan.")

    st.subheader("Balance in the same period each year")
    history = regional[regional.metric_code.eq("budget_balance") & regional.period.dt.month.eq(pd.Timestamp(period).month) & regional.period.le(pd.Timestamp(period))].copy()
    history["Year"] = history.period.dt.year.astype(str)
    history["RUB bn"] = history.actual_ytd_rub / 1e9
    chart = alt.Chart(history).mark_bar().encode(
        x=alt.X("Year:N", axis=alt.Axis(labelAngle=0)), y=alt.Y("RUB bn:Q", scale=alt.Scale(zero=True)),
        color=alt.condition(alt.datum["RUB bn"] < 0, alt.value("#C66A19"), alt.value("#2455A4")),
        tooltip=["Year", alt.Tooltip("RUB bn:Q", format="+.2f")])
    zero = alt.Chart(pd.DataFrame({"zero": [0]})).mark_rule(color="#182230").encode(y="zero:Q")
    st.altair_chart((chart + zero).properties(height=240), width="stretch")
    st.dataframe(history[["Year", "RUB bn"]], hide_index=True, width="stretch")

    st.subheader("Where spending went")
    spending = selected[selected.kind.eq("expenditure_function")].copy()
    spending["Function"] = spending.metric_code.map(FUNCTIONS).fillna(spending.name_ru)
    spending["RUB bn"] = spending.actual_ytd_rub / 1e9
    spending["Share (%)"] = spending.actual_ytd_rub / totals.expenditure_total * 100 if totals.expenditure_total != 0 else np.nan
    chart = alt.Chart(spending).mark_bar(color="#2455A4").encode(
        y=alt.Y("Function:N", sort="-x", title=None, axis=alt.Axis(labelLimit=310)),
        x=alt.X("RUB bn:Q", scale=alt.Scale(zero=True)),
        tooltip=["Function", "name_ru", alt.Tooltip("RUB bn:Q", format=".3f"), alt.Tooltip("Share (%):Q", format=".1f")])
    st.altair_chart(chart.properties(height=440), width="stretch")
    st.caption("These are regional-budget functions, not all public spending in the region. Federal defence expenditure and health-insurance funds are outside this perimeter.")
    with st.expander("Spending table — English and Russian"):
        st.dataframe(spending[["Function", "name_ru", "RUB bn", "Share (%)"]], hide_index=True, width="stretch")

    st.subheader("How the balance was financed")
    finance = selected[selected.metric_code.isin(["internal_financing", "external_financing", "cash_balance_change"])].copy()
    finance["Item"] = finance.metric_code.map({"internal_financing": "Internal financing (net)", "external_financing": "External financing (net)", "cash_balance_change": "Change in cash balances (financing sign)"})
    finance["RUB bn"] = finance.actual_ytd_rub / 1e9
    st.dataframe(finance[["Item", "name_ru", "RUB bn"]], hide_index=True, width="stretch")
    st.caption("Signed flows, not debt stocks or closing cash holdings. Unreported categories are omitted, not assumed zero. Financing offsets the budget balance.")
    with st.expander("Source records and limitations"):
        plan = selected[selected.kind.eq("summary")].set_index("metric_code").plan_at_cutoff_rub
        plan_gap = plan.revenue_total - plan.expenditure_total - plan.budget_balance
        if abs(plan_gap) > .02:
            st.warning("For this period, the reported plan balance does not reconcile with plan revenue minus expenditure. Snapshot assignments remain in the source download; no plan-based shortfall is calculated.")
        st.caption("Snapshot plan figures do not establish original-budget or amendment histories.")
        st.write("Cutoffs come from archive filenames; publication and retrieval dates are not established here. Some 2026 archives have errors in ancillary members; the selected HTML reports passed CRC checks.")
        st.dataframe(selected, hide_index=True, width="stretch")
        st.caption(f"Candidate: {manifest['region_periods']} region-periods; {manifest['blocking_failures']} blocking failures at extraction.")
    st.caption("Source: Federal Treasury · " + ", ".join(selected.source_file.unique()))
    st.download_button("Download selected source-backed observations (CSV)", selected.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"fiscal_{region_id}_{pd.Timestamp(period):%Y-%m-%d}.csv", mime="text/csv")
