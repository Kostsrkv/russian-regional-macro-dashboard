"""Comparable actual budget flows and accounting decompositions.

All amounts are nominal RUB for consolidated regional (regional + municipal)
budgets. Federal budgets and territorial health-insurance funds are excluded.
Functions are mutually exclusive top-level spending functions; nested revenue
codes are never added together. Missing observations remain missing, including
missing Q1 endpoints needed to derive Q2. Negative reported adjustments survive.

Public helpers accept loaded fiscal observations and the loaded revenue bundle
(or a revenue frame already joined to its lineage). They return new frames and
never mutate their inputs. ``basis`` is ``YTD`` or ``period_flow``; the latter
means Q1 = Q1 YTD and Q2 = H1 YTD - Q1 YTD in the same year. Source evidence is
JSON for portable CSV exports, with explicit start/end endpoint fields on flows.
"""
from __future__ import annotations

import json
from typing import Mapping

import numpy as np
import pandas as pd

from .fiscal_view import FUNCTIONS, period_label
from .revenue_view import OWN, TRANSFERS

SCOPE = "consolidated_regional_budget"
BASES = ("YTD", "period_flow")
EVIDENCE_FIELDS = (
    "source_file", "source_sha256", "source_member", "reporting_cutoff",
    "source_url", "source_id", "source_vintage", "source_row_index",
    "actual_column_index",
)
_REQUIRED_EVIDENCE = {"source_file", "source_sha256", "source_member", "reporting_cutoff"}
BRIDGE_COLUMNS = [
    "region_id", "period", "prior_period", "budget_level", "amount_basis",
    "window", "step", "label", "role", "value_rub", "from_rub", "to_rub",
    "start_balance_rub", "end_balance_rub", "balance_change_rub",
    "revenue_change_rub", "expenditure_change_rub", "reconciliation_gap_rub",
    "source_evidence", "prior_source_evidence", "units",
]


def _validated(frame: pd.DataFrame, *, revenue: bool = False) -> pd.DataFrame:
    code = "revenue_code" if revenue else "metric_code"
    required = {"region_id", "period", code, "budget_level", "actual_ytd_rub"}
    if not revenue:
        required.add("kind")
    if not required.issubset(frame.columns):
        raise ValueError("Budget analysis is missing required observation fields")
    if not _REQUIRED_EVIDENCE.issubset(frame.columns):
        raise ValueError("Budget analysis requires source hash, member and cutoff lineage")
    work = frame.copy()
    work["period"] = pd.to_datetime(work.period, errors="raise")
    if work.period.isna().any() or not work.budget_level.eq(SCOPE).all():
        raise ValueError("Budget analysis requires the consolidated regional budget perimeter")
    if "units" in work and not work.units.eq("RUB").all():
        raise ValueError("Budget analysis requires RUB")
    if "amount_basis" in work and not work.amount_basis.eq("YTD").all():
        raise ValueError("Input observations must be cumulative YTD amounts")
    if not revenue and "frequency" in work and not work.frequency.eq("YTD").all():
        raise ValueError("Fiscal observations must be cumulative YTD amounts")
    work[code] = work[code].astype(str)
    keys = ["region_id", "period", "budget_level", code]
    if not revenue:
        keys.append("kind")
    if work.duplicated(keys).any():
        raise ValueError("Budget analysis requires one selected vintage at observation grain")
    work["actual_ytd_rub"] = pd.to_numeric(work.actual_ytd_rub, errors="raise")
    if np.isinf(work.actual_ytd_rub).any():
        raise ValueError("Infinite budget amounts are invalid")
    return work


def _evidence(row: pd.Series, prefix: str = "") -> dict:
    result = {}
    for field in ("period", *EVIDENCE_FIELDS):
        value = row.get(prefix + field)
        if value is not None and not pd.isna(value):
            if isinstance(value, pd.Timestamp):
                value = value.strftime("%Y-%m-%d")
            elif isinstance(value, np.generic):
                value = value.item()
            result[field] = value
    return result


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _derive_flows(work: pd.DataFrame, *, revenue: bool = False) -> pd.DataFrame:
    code = "revenue_code" if revenue else "metric_code"
    keys = ["region_id", "period", "budget_level", code]
    if not revenue:
        keys.append("kind")
    fields = ["actual_ytd_rub", *[f for f in EVIDENCE_FIELDS if f in work]]
    # Only Q1 is a permissible lower endpoint for H1. A previous row, another
    # year's Q1, or a nonadjacent snapshot never substitutes for it.
    starts = work.loc[work.period.dt.month.eq(3) & work.period.dt.is_quarter_end,
                      keys + fields].copy()
    starts["start_period"] = starts.period
    starts["period"] = starts.period + pd.offsets.QuarterEnd(1)
    starts = starts.rename(columns={f: "start_" + f for f in fields})
    result = work.merge(starts, on=keys, how="left", validate="one_to_one")
    for field in ("period", *EVIDENCE_FIELDS):
        if field in result:
            result["end_" + field] = result[field]
    first = result.period.dt.is_quarter_end & result.period.dt.month.eq(3)
    second = result.period.dt.is_quarter_end & result.period.dt.month.eq(6)
    complete = second & result.actual_ytd_rub.notna() & result.start_actual_ytd_rub.notna()
    result["quarter_flow_rub"] = np.nan
    result.loc[first, "quarter_flow_rub"] = result.loc[first, "actual_ytd_rub"]
    result.loc[complete, "quarter_flow_rub"] = (
        result.loc[complete, "actual_ytd_rub"] - result.loc[complete, "start_actual_ytd_rub"])
    result["window"] = np.select([first, second], ["Q1", "Q2"], default="unsupported")
    result["window_start"] = pd.to_datetime(result.period.dt.year.astype(str) + "-01-01")
    result.loc[second, "window_start"] = pd.to_datetime(result.loc[second, "period"].dt.year.astype(str) + "-04-01")
    result["window_end"] = result.period
    result["flow_status"] = np.select(
        [first & result.actual_ytd_rub.notna(), complete, first | second],
        ["Q1 equals YTD", "H1 minus Q1 YTD", "missing endpoint"], default="unsupported snapshot")
    result["flow_source_evidence"] = result.apply(
        lambda r: _json({"end": _evidence(r, "end_"),
                         "start": _evidence(r, "start_") if r.window == "Q2" else None}), axis=1)
    result["amount_basis"] = "period_flow"
    result["units"] = "RUB"
    return result.sort_values(keys).reset_index(drop=True)


def derive_fiscal_flows(fiscal: pd.DataFrame) -> pd.DataFrame:
    """Return Q1/Q2 signed flows, preserving every source row and both endpoints.

    ``quarter_flow_rub`` remains NaN when either amount is missing or no exact
    same-year Q1 exists. Endpoint hash/member/cutoff columns and JSON evidence
    record all contributors. Q1's beginning-of-year zero is mathematical, not a
    fabricated source observation; its ``start`` evidence is null.
    """
    return _derive_flows(_validated(fiscal))


def _observations(frame: pd.DataFrame, basis: str, *, revenue: bool = False) -> pd.DataFrame:
    if basis not in BASES:
        raise ValueError(f"Unsupported amount basis: {basis}")
    work = _validated(frame, revenue=revenue)
    if basis == "period_flow":
        work = _derive_flows(work, revenue=revenue)
        work["amount_rub"] = work.quarter_flow_rub
        work["source_evidence"] = work.flow_source_evidence
    else:
        work["amount_rub"] = work.actual_ytd_rub
        work["window"] = np.select(
            [work.period.dt.is_quarter_end & work.period.dt.month.eq(3),
             work.period.dt.is_quarter_end & work.period.dt.month.eq(6)],
            ["Q1", "H1"], default="unsupported")
        work["source_evidence"] = work.apply(lambda r: _json({"end": _evidence(r)}), axis=1)
    work["amount_basis"] = basis
    return work


def spending_function_history(fiscal: pd.DataFrame, basis: str = "YTD",
                             region_id: str | None = None) -> pd.DataFrame:
    """Return all 14 functions with levels, shares and same-window YoY changes.

    ``contribution_to_spending_change_rub`` is the signed function change; its
    sum equals total spending change when both endpoints have all 14 functions
    and reconcile. ``contribution_to_spending_growth_pp`` divides that change
    by prior total spending. No rate uses a zero/missing/nonpositive prior
    denominator. Share changes are percentage points, not percent growth.
    Each row retains evidence for the function and both total denominators.
    """
    work = _observations(fiscal, basis)
    if region_id is not None:
        work = work.loc[work.region_id.eq(region_id)].copy()
    keys = ["region_id", "period", "budget_level", "amount_basis", "window"]
    totals = work.loc[work.kind.eq("summary") & work.metric_code.eq("expenditure_total"),
                      keys + ["amount_rub", "source_evidence"]].rename(
        columns={"amount_rub": "total_spending_rub", "source_evidence": "total_source_evidence"})
    if work.empty:
        return pd.DataFrame()
    grid = work[keys].drop_duplicates().merge(pd.DataFrame({"metric_code": list(FUNCTIONS)}), how="cross")
    functions = work.loc[work.kind.eq("expenditure_function") & work.metric_code.isin(FUNCTIONS),
                         keys + ["metric_code", "amount_rub", "source_evidence"]]
    result = grid.merge(functions, on=keys + ["metric_code"], how="left", validate="one_to_one")
    result = result.merge(totals, on=keys, how="left", validate="many_to_one")
    result["share_pct"] = (result.amount_rub / result.total_spending_rub * 100).where(result.total_spending_rub.gt(0))
    prior_fields = ["amount_rub", "share_pct", "total_spending_rub", "source_evidence", "total_source_evidence"]
    prior = result[keys + ["metric_code"] + prior_fields].copy()
    prior["period"] = prior.period + pd.DateOffset(years=1)
    prior = prior.rename(columns={f: "prior_" + f for f in prior_fields})
    result = result.merge(prior, on=keys + ["metric_code"], how="left", validate="one_to_one")
    result["change_yoy_rub"] = result.amount_rub - result.prior_amount_rub
    result["growth_yoy_pct"] = (result.change_yoy_rub / result.prior_amount_rub * 100).where(result.prior_amount_rub.gt(0))
    result["share_change_pp"] = result.share_pct - result.prior_share_pct
    result["total_spending_change_rub"] = result.total_spending_rub - result.prior_total_spending_rub
    result["contribution_to_spending_change_rub"] = result.change_yoy_rub
    result["contribution_to_spending_growth_pp"] = (
        result.change_yoy_rub / result.prior_total_spending_rub * 100).where(result.prior_total_spending_rub.gt(0))
    result["function_name_en"] = result.metric_code.map(FUNCTIONS)
    grouped = result.groupby(keys, dropna=False)
    result["functions_complete"] = grouped.amount_rub.transform("count").eq(14)
    result["comparison_complete"] = (result.functions_complete & grouped.prior_amount_rub.transform("count").eq(14)
                                     & result.total_spending_rub.notna() & result.prior_total_spending_rub.notna())
    result["spending_reconciliation_gap_rub"] = (
        grouped.amount_rub.transform(lambda s: s.sum(min_count=14)) - result.total_spending_rub)
    result["change_reconciliation_gap_rub"] = (
        grouped.change_yoy_rub.transform(lambda s: s.sum(min_count=14)) - result.total_spending_change_rub)
    valid = result.functions_complete & result.total_spending_rub.notna()
    if result.loc[valid, "spending_reconciliation_gap_rub"].abs().gt(.02).any():
        raise ValueError("All 14 spending functions do not reconcile to total spending")
    if result.loc[result.comparison_complete, "change_reconciliation_gap_rub"].abs().gt(.02).any():
        raise ValueError("Function changes do not reconcile to total spending change")
    result["prior_period"] = result.period - pd.DateOffset(years=1)
    result["units"] = "RUB"
    return result.sort_values(keys + ["metric_code"]).reset_index(drop=True)


def budget_balance_bridge(fiscal: pd.DataFrame, region_id: str, period,
                          basis: str = "YTD") -> tuple[pd.DataFrame, str | None]:
    """Four-step bridge from prior-year same-window balance to current balance.

    Returns ``(empty frame, reason)`` when either window is unavailable. Complete
    inputs must satisfy balance = revenue - expenditure at each endpoint and
    Δbalance = Δrevenue - Δexpenditure, or ValueError is raised. Expenditure's
    signed bridge contribution is the negative of its actual spending change.
    """
    regional = fiscal.loc[fiscal.region_id.eq(region_id)].copy()
    work = _observations(regional, basis)
    period = pd.Timestamp(period)
    prior_period = period - pd.DateOffset(years=1)
    required = ["revenue_total", "expenditure_total", "budget_balance"]
    endpoints = []
    for date, label in ((prior_period, "Prior-year"), (period, "Current")):
        selected = work.loc[work.period.eq(date) & work.kind.eq("summary") & work.metric_code.isin(required)]
        if len(selected) != 3 or selected.amount_rub.isna().any() or selected.window.eq("unsupported").any():
            return pd.DataFrame(columns=BRIDGE_COLUMNS), f"{label} same-window actuals are unavailable; no balance bridge is calculated."
        values = selected.set_index("metric_code").amount_rub
        if abs(values.revenue_total - values.expenditure_total - values.budget_balance) > .02:
            raise ValueError(f"{label} balance does not equal revenue minus expenditure")
        endpoints.append((values, _json({r.metric_code: json.loads(r.source_evidence) for _, r in selected.iterrows()}), selected.iloc[0].window))
    start, end = endpoints[0][0], endpoints[1][0]
    delta_revenue = end.revenue_total - start.revenue_total
    delta_spending = end.expenditure_total - start.expenditure_total
    delta_balance = end.budget_balance - start.budget_balance
    gap = delta_balance - delta_revenue + delta_spending
    if abs(gap) > .02:
        raise ValueError("Balance bridge does not reconcile")
    middle = start.budget_balance + delta_revenue
    steps = [
        (0, "Prior balance", "anchor", start.budget_balance, 0, start.budget_balance),
        (1, "Revenue change", "change", delta_revenue, start.budget_balance, middle),
        (2, "Expenditure change", "change", -delta_spending, middle, end.budget_balance),
        (3, "Current balance", "anchor", end.budget_balance, 0, end.budget_balance),
    ]
    rows = [{"region_id": region_id, "period": period, "prior_period": prior_period,
             "budget_level": SCOPE, "amount_basis": basis, "window": endpoints[1][2],
             "step": step, "label": label, "role": role, "value_rub": value,
             "from_rub": lower, "to_rub": upper, "start_balance_rub": start.budget_balance,
             "end_balance_rub": end.budget_balance, "balance_change_rub": delta_balance,
             "revenue_change_rub": delta_revenue, "expenditure_change_rub": delta_spending,
             "reconciliation_gap_rub": gap, "source_evidence": endpoints[1][1],
             "prior_source_evidence": endpoints[0][1], "units": "RUB"}
            for step, label, role, value, lower, upper in steps]
    return pd.DataFrame(rows, columns=BRIDGE_COLUMNS), None


def _revenue_frame(revenue: pd.DataFrame | Mapping) -> pd.DataFrame:
    if isinstance(revenue, pd.DataFrame):
        return revenue.copy()
    budget = revenue["budget_execution"].copy()
    lineage = revenue["revenue_lineage"].copy()
    budget["period"] = pd.to_datetime(budget.period)
    lineage["period"] = pd.to_datetime(lineage.period)
    keys = ["region_id", "period", "revenue_code", "source_id", "source_vintage"]
    if lineage.duplicated(keys).any():
        raise ValueError("Duplicate revenue lineage")
    check = budget[keys + ["actual_ytd_rub"]].merge(
        lineage[keys + ["actual_ytd_rub"]], on=keys, how="left", validate="one_to_one", suffixes=("", "_lineage"))
    if check.actual_ytd_rub_lineage.isna().any() or (check.actual_ytd_rub - check.actual_ytd_rub_lineage).abs().gt(.02).any():
        raise ValueError("Revenue amounts disagree with source lineage")
    fields = [f for f in EVIDENCE_FIELDS if f in lineage and f not in budget]
    return budget.merge(lineage[keys + fields], on=keys, how="left", validate="one_to_one")


def transfer_history(revenue: pd.DataFrame | Mapping, basis: str = "YTD",
                     region_id: str | None = None) -> pd.DataFrame:
    """202 transfers / total revenue and own-source amounts, at one window grain.

    Code 202 means transfers from other budgets; it does not establish federal
    origin and is nested within non-repayable receipts. Own-source is code 100
    (tax and non-tax revenue), not total revenue minus 202. Each amount and its
    prior-year comparison carries its source evidence, including both Q2 ends.
    """
    frame = _revenue_frame(revenue)
    if region_id is not None:
        frame = frame.loc[frame.region_id.eq(region_id)].copy()
    work = _observations(frame, basis, revenue=True)
    keys = ["region_id", "period", "budget_level", "amount_basis", "window"]
    result = work[keys].drop_duplicates()
    for code, stem in (("TOTAL_REVENUE", "total_revenue"), (TRANSFERS, "transfers"), (OWN, "own_source")):
        rows = work.loc[work.revenue_code.eq(code), keys + ["amount_rub", "source_evidence"]].rename(
            columns={"amount_rub": stem + "_rub", "source_evidence": stem + "_source_evidence"})
        result = result.merge(rows, on=keys, how="left", validate="one_to_one")
    result["transfer_share_pct"] = (result.transfers_rub / result.total_revenue_rub * 100).where(result.total_revenue_rub.gt(0))
    prior_fields = [f for f in result if f not in keys]
    prior = result.copy()
    prior["period"] = prior.period + pd.DateOffset(years=1)
    prior = prior.rename(columns={f: "prior_" + f for f in prior_fields})
    result = result.merge(prior, on=keys, how="left", validate="one_to_one")
    result["transfer_share_change_pp"] = result.transfer_share_pct - result.prior_transfer_share_pct
    for stem in ("transfers", "own_source", "total_revenue"):
        result[stem + "_change_yoy_rub"] = result[stem + "_rub"] - result["prior_" + stem + "_rub"]
        result[stem + "_growth_yoy_pct"] = (
            result[stem + "_change_yoy_rub"] / result["prior_" + stem + "_rub"] * 100
        ).where(result["prior_" + stem + "_rub"].gt(0))
    result["prior_period"] = result.period - pd.DateOffset(years=1)
    result["units"] = "RUB"
    return result.sort_values(keys).reset_index(drop=True)


def render_fiscal_analysis(st, alt, fiscal: pd.DataFrame, revenue, region_id: str) -> None:
    """Dedicated Streamlit page; all figures and CSVs use the selected region.

    Period/basis controls affect summary, bridge and function detail; historical
    figures compare the selected window in each available year through the
    selected date. ``revenue`` should be the full loaded bundle for lineage.
    """
    regional = fiscal.loc[fiscal.region_id.eq(region_id)].copy()
    if regional.empty:
        st.info("Fiscal observations are unavailable for this region.")
        return
    st.caption("Research review dataset · publication does not establish independent verification.")
    controls = st.columns(2)
    period = controls[0].selectbox("Fiscal reporting period", sorted(pd.to_datetime(regional.period).unique(), reverse=True),
                                    format_func=period_label, key="fiscal_analysis_period")
    basis = controls[1].selectbox("Amount basis", BASES,
                                  format_func=lambda b: "Cumulative (Q1 / H1)" if b == "YTD" else "Quarter flow (Q1 / Q2)",
                                  key="fiscal_analysis_basis")
    work = _observations(regional, basis)
    selected = work.loc[work.period.eq(pd.Timestamp(period))]
    window = selected.iloc[0].window
    st.caption(f"{window} {pd.Timestamp(period).year} · nominal RUB · regional + municipal budgets, excluding territorial health-insurance funds")
    source_rows = selected[["source_file", "reporting_cutoff"]].rename(columns={"source_file": "file", "reporting_cutoff": "cutoff"})
    if basis == "period_flow" and window == "Q2":
        start_rows = selected[["start_source_file", "start_reporting_cutoff"]].rename(
            columns={"start_source_file": "file", "start_reporting_cutoff": "cutoff"})
        source_rows = pd.concat([source_rows, start_rows], ignore_index=True)
    source_rows = source_rows.dropna().drop_duplicates().sort_values("cutoff")
    st.caption("Source: Federal Treasury · " + "; ".join(f"{r.file} (cutoff {r.cutoff})" for r in source_rows.itertuples(index=False)))
    totals = selected.loc[selected.kind.eq("summary")].set_index("metric_code").amount_rub
    for column, label, code in zip(st.columns(3), ("Revenue", "Expenditure", "Budget balance"),
                                   ("revenue_total", "expenditure_total", "budget_balance")):
        value = totals.get(code, np.nan)
        column.metric(label, f"{value / 1e9:,.2f} bn RUB" if pd.notna(value) else "Unavailable")
    if basis == "period_flow":
        st.caption("Q1 equals cumulative Q1; Q2 equals cumulative H1 minus Q1 from the same year. Missing endpoints remain unavailable.")

    st.subheader("Change in budget balance")
    bridge, reason = budget_balance_bridge(regional, region_id, period, basis)
    if reason:
        st.info(reason)
    else:
        plot = bridge.copy()
        plot["From (RUB bn)"] = plot.from_rub / 1e9
        plot["To (RUB bn)"] = plot.to_rub / 1e9
        plot["Amount (RUB bn)"] = plot.value_rub / 1e9
        plot["label_y"] = plot[["From (RUB bn)", "To (RUB bn)"]].max(axis=1)
        plot["signed_label"] = plot["Amount (RUB bn)"].map(lambda value: f"{value:+.2f}")
        plot["color"] = np.select([plot.role.eq("anchor"), plot.value_rub.ge(0)], ["#677582", "#2455A4"], default="#C66A19")
        chart = alt.Chart(plot).mark_bar().encode(
            x=alt.X("label:N", sort=plot.label.tolist(), title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("From (RUB bn):Q", title="RUB bn", scale=alt.Scale(zero=True)), y2="To (RUB bn):Q",
            color=alt.Color("color:N", scale=None, legend=None),
            tooltip=["label", alt.Tooltip("Amount (RUB bn):Q", format="+.3f")])
        labels = alt.Chart(plot).mark_text(dy=-10, color="#182230").encode(
            x=alt.X("label:N", sort=plot.label.tolist()), y="label_y:Q", text="signed_label:N")
        zero = alt.Chart(pd.DataFrame({"zero": [0]})).mark_rule(color="#182230").encode(y="zero:Q")
        st.altair_chart((chart + labels + zero).properties(height=260), width="stretch")
        st.caption(f"Same {window} window in {pd.Timestamp(period).year - 1} and {pd.Timestamp(period).year}. Change in balance = change in revenue − change in expenditure; expenditure bars show the effect on balance.")
        st.caption("This is an accounting bridge. It does not establish causes or a revenue shortfall against plan.")
        with st.expander("Balance bridge values"):
            exact = plot[["label", "Amount (RUB bn)"]].rename(columns={"label": "Item"})
            exact["Amount (RUB bn)"] = exact["Amount (RUB bn)"].map(lambda value: f"{value:+,.3f}")
            st.dataframe(exact, hide_index=True, width="stretch")

    st.subheader("Spending functions and share changes")
    functions = spending_function_history(regional, basis, region_id)
    current = functions.loc[functions.period.eq(pd.Timestamp(period))].copy()
    plot = current.copy()
    plot["Share change (pp)"] = plot.share_change_pp
    chart = alt.Chart(plot).mark_bar(color="#2455A4").encode(
        y=alt.Y("function_name_en:N", title=None, sort=list(FUNCTIONS.values()), axis=alt.Axis(labelLimit=340)),
        x=alt.X("Share change (pp):Q", scale=alt.Scale(zero=True)),
        tooltip=["function_name_en", alt.Tooltip("Share change (pp):Q", format="+.2f")])
    if current.share_change_pp.notna().any():
        st.altair_chart(chart.properties(height=430), width="stretch")
    else:
        st.info("Prior-year spending shares are unavailable for this window.")
    table = current[["function_name_en", "amount_rub", "share_pct", "growth_yoy_pct", "share_change_pp", "contribution_to_spending_change_rub"]].copy()
    table[["amount_rub", "contribution_to_spending_change_rub"]] /= 1e9
    display = table.rename(columns={"function_name_en": "Function", "amount_rub": "Actual (RUB bn)",
        "share_pct": "Spending share (%)", "growth_yoy_pct": "Same-window YoY (%)", "share_change_pp": "Share change (pp)",
        "contribution_to_spending_change_rub": "Spending change (RUB bn)"})
    for field in display.columns[1:]:
        display[field] = display[field].map(lambda value: f"{value:,.2f}" if pd.notna(value) else "Unavailable")
    st.dataframe(display, hide_index=True, width="stretch")
    st.caption("All 14 top-level functions cover total spending. Signed function changes add to the total spending change when both years are complete. These are accounting contributions.")
    st.caption("Function 0200 is the regional-budget national-defence classification. It does not measure military contracts or recruitment payments in the region.")

    st.subheader("Transfers and own-source revenue history")
    transfers = transfer_history(revenue, basis, region_id)
    history = transfers.loc[transfers.period.dt.month.eq(pd.Timestamp(period).month) & transfers.period.le(pd.Timestamp(period))].copy()
    history["Year"] = history.period.dt.year.astype(str)
    history["Transfer share (%)"] = history.transfer_share_pct
    amounts = history.melt(id_vars=["Year"], value_vars=["transfers_rub", "own_source_rub"], var_name="Series", value_name="Amount")
    amounts["RUB bn"] = amounts.Amount / 1e9
    amounts["Series"] = amounts.Series.map({"transfers_rub": "Transfers from other budgets", "own_source_rub": "Tax and non-tax revenue"})
    chart = alt.Chart(amounts).mark_bar().encode(
        x=alt.X("Year:N", title=None, axis=alt.Axis(labelAngle=0)), xOffset="Series:N",
        y=alt.Y("RUB bn:Q", scale=alt.Scale(zero=True)),
        color=alt.Color("Series:N", scale=alt.Scale(domain=["Transfers from other budgets", "Tax and non-tax revenue"], range=["#C66A19", "#2455A4"])),
        tooltip=["Year", "Series", alt.Tooltip("RUB bn:Q", format=".3f")])
    st.altair_chart(chart.properties(height=250), width="stretch")
    share = alt.Chart(history).mark_bar(color="#C66A19").encode(
        x=alt.X("Year:N", title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("Transfer share (%):Q", scale=alt.Scale(zero=True)),
        tooltip=["Year", alt.Tooltip("Transfer share (%):Q", format=".2f")])
    st.altair_chart(share.properties(height=180), width="stretch")
    st.caption("Transfer share = code 202 transfers from other budgets ÷ total revenue. The source does not establish federal origin. Amounts and shares use separate scales; tax and non-tax revenue is code 100.")
    with st.expander("Transfer and own-source history values"):
        exact = history[["Year", "total_revenue_rub", "transfers_rub", "own_source_rub", "transfer_share_pct"]].copy()
        for field in ("total_revenue_rub", "transfers_rub", "own_source_rub"):
            exact[field] = (exact[field] / 1e9).map(lambda value: f"{value:,.3f}" if pd.notna(value) else "Unavailable")
        exact["transfer_share_pct"] = exact.transfer_share_pct.map(lambda value: f"{value:.2f}" if pd.notna(value) else "Unavailable")
        st.dataframe(exact.rename(columns={"total_revenue_rub": "Revenue (RUB bn)", "transfers_rub": "Transfers (RUB bn)",
            "own_source_rub": "Tax and non-tax revenue (RUB bn)", "transfer_share_pct": "Transfer share (%)"}), hide_index=True, width="stretch")
    with st.expander("Spending shares across the same window each year"):
        historical = functions.loc[functions.period.dt.month.eq(pd.Timestamp(period).month) & functions.period.le(pd.Timestamp(period))]
        shares = historical.pivot(index="function_name_en", columns="period", values="share_pct")
        shares.columns = shares.columns.strftime("%Y-%m-%d")
        st.dataframe(shares.map(lambda value: f"{value:.2f}" if pd.notna(value) else "Unavailable"), width="stretch")
    with st.expander("Source evidence and CSV exports"):
        st.dataframe(selected, hide_index=True, width="stretch")
        for label, data, filename in (
            ("Fiscal observations and endpoint evidence", selected, "observations"),
            ("Spending function history", functions.loc[functions.period.le(pd.Timestamp(period)) & functions.period.dt.month.eq(pd.Timestamp(period).month)], "spending_functions"),
            ("Balance bridge", bridge, "balance_bridge"),
            ("Transfer history", history, "transfers"),
        ):
            st.download_button(f"Download {label} (CSV)", data.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"fiscal_{region_id}_{pd.Timestamp(period):%Y-%m-%d}_{basis}_{filename}.csv", mime="text/csv")
