"""Non-overlapping revenue components and their effect on actual balances.

This is an accounting decomposition, not causal attribution or a plan-based
shortfall model. Amounts are nominal RUB on the existing consolidated regional
budget perimeter. Exact-window comparisons and lineage come from fiscal_analysis.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .fiscal_analysis import _json, _observations, _revenue_frame, budget_balance_bridge
from .revenue_view import CIT, NON_REPAYABLE, OWN, PIT, TRANSFERS

COMPONENTS = {
    "retained_pit": ("Retained PIT", (PIT,), (1,)),
    "cit": ("Corporate income tax", (CIT,), (1,)),
    "other_own": ("Other tax and non-tax revenue", (OWN, PIT, CIT), (1, -1, -1)),
    "transfers": ("Transfers from other budgets", (TRANSFERS,), (1,)),
    "other_non_repayable": ("Other non-repayable receipts (net)", (NON_REPAYABLE, TRANSFERS), (1, -1)),
}
REQUIRED_CODES = {"TOTAL_REVENUE", OWN, PIT, CIT, NON_REPAYABLE, TRANSFERS}
KEYS = ["region_id", "period", "budget_level", "amount_basis", "window"]
TOLERANCE_RUB = .02


def revenue_component_history(revenue, basis: str = "YTD",
                              region_id: str | None = None) -> pd.DataFrame:
    """Five disjoint components with same-window YoY contributions.

    Other own-source revenue is code 100 minus retained PIT and CIT. Other
    non-repayable receipts are code 200 minus code 202. Neither parent code is
    added to its children. A residual is not a new published classification.
    Missing required inputs remain missing, including an unavailable transfer
    parent: no zero or residual substitute is invented for it.
    """
    frame = _revenue_frame(revenue)
    if region_id is not None:
        frame = frame.loc[frame.region_id.eq(region_id)].copy()
    work = _observations(frame, basis, revenue=True)
    rows = []
    for key, group in work.groupby(KEYS, sort=True, dropna=False):
        lookup = group.set_index("revenue_code")
        if "TOTAL_REVENUE" in lookup.index:
            reference = json.loads(lookup.loc["TOTAL_REVENUE", "source_evidence"])
            for code in REQUIRED_CODES.intersection(lookup.index):
                if pd.isna(lookup.loc[code, "amount_rub"]):
                    continue
                evidence = json.loads(lookup.loc[code, "source_evidence"])
                for endpoint in ("end", "start"):
                    actual, expected = evidence.get(endpoint), reference.get(endpoint)
                    if (actual is None) != (expected is None):
                        raise ValueError("Revenue component endpoints disagree")
                    if actual is not None and any(actual.get(field) != expected.get(field) for field in
                        ("period", "source_file", "source_sha256", "source_member", "reporting_cutoff")):
                        raise ValueError("Revenue components have mixed source provenance")
        total = lookup.amount_rub.get("TOTAL_REVENUE", np.nan)
        parent_own = lookup.amount_rub.get(OWN, np.nan)
        parent_other = lookup.amount_rub.get(NON_REPAYABLE, np.nan)
        if all(pd.notna(v) for v in (total, parent_own, parent_other)):
            if abs(total - parent_own - parent_other) > TOLERANCE_RUB:
                raise ValueError("Revenue parent codes do not reconcile to total revenue")
        values = []
        for code, (label, inputs, coefficients) in COMPONENTS.items():
            amounts = [lookup.amount_rub.get(item, np.nan) for item in inputs]
            supported = key[-1] != "unsupported"
            complete = supported and all(pd.notna(value) for value in amounts)
            amount = sum(c * a for c, a in zip(coefficients, amounts)) if complete else np.nan
            evidence = {item: json.loads(lookup.loc[item, "source_evidence"])
                        if item in lookup.index else None for item in inputs}
            total_evidence = (json.loads(lookup.loc["TOTAL_REVENUE", "source_evidence"])
                              if "TOTAL_REVENUE" in lookup.index else None)
            values.append(amount)
            rows.append({**dict(zip(KEYS, key)), "component_code": code,
                         "component_name": label, "amount_rub": amount,
                         "total_revenue_rub": total,
                         "availability": "available" if complete else "unavailable",
                         "component_basis": "published_parent" if len(inputs) == 1 else "derived_residual",
                         "source_codes": _json(list(inputs)),
                         "source_coefficients": _json(list(coefficients)),
                         "source_evidence": _json(evidence),
                         "total_source_evidence": _json(total_evidence),
                         "units": "RUB", "quality_status": "candidate_not_promoted"})
        if pd.notna(total) and all(pd.notna(v) for v in values):
            if abs(sum(values) - total) > TOLERANCE_RUB:
                raise ValueError("Disjoint revenue components do not reconcile")
    columns = KEYS + ["component_code", "component_name", "amount_rub", "total_revenue_rub",
                      "availability", "component_basis", "source_codes", "source_coefficients",
                      "source_evidence", "total_source_evidence", "units", "quality_status"]
    result = pd.DataFrame(rows, columns=columns)
    if result.empty:
        for name in ("prior_amount_rub", "prior_total_revenue_rub", "change_yoy_rub",
                     "growth_yoy_pct", "share_pct", "contribution_to_revenue_growth_pp",
                     "total_revenue_change_rub", "change_reconciliation_gap_rub"):
            result[name] = pd.Series(dtype="float64")
        for name in ("prior_source_evidence", "prior_total_source_evidence", "prior_availability"):
            result[name] = pd.Series(dtype="object")
        for name in ("period", "prior_period"):
            result[name] = pd.Series(dtype="datetime64[ns]")
        for name in ("negative_amount_flag", "comparison_complete"):
            result[name] = pd.Series(dtype="bool")
        return result
    join_keys = KEYS + ["component_code"]
    prior_fields = ["amount_rub", "total_revenue_rub", "source_evidence", "total_source_evidence", "availability"]
    prior = result[join_keys + prior_fields].copy()
    prior["period"] = prior.period + pd.DateOffset(years=1)
    prior = prior.rename(columns={field: "prior_" + field for field in prior_fields})
    result = result.merge(prior, on=join_keys, how="left", validate="one_to_one")
    result["prior_period"] = result.period - pd.DateOffset(years=1)
    result["change_yoy_rub"] = result.amount_rub - result.prior_amount_rub
    result["growth_yoy_pct"] = (result.change_yoy_rub / result.prior_amount_rub * 100).where(result.prior_amount_rub.gt(0))
    result["share_pct"] = (result.amount_rub / result.total_revenue_rub * 100).where(result.total_revenue_rub.gt(0))
    result["contribution_to_revenue_growth_pp"] = (result.change_yoy_rub / result.prior_total_revenue_rub * 100).where(result.prior_total_revenue_rub.gt(0))
    result["total_revenue_change_rub"] = result.total_revenue_rub - result.prior_total_revenue_rub
    result["negative_amount_flag"] = result.amount_rub.lt(0)
    grouped = result.groupby(KEYS, dropna=False)
    result["comparison_complete"] = grouped.change_yoy_rub.transform("count").eq(len(COMPONENTS))
    result["change_reconciliation_gap_rub"] = grouped.change_yoy_rub.transform(
        lambda values: values.sum(min_count=len(COMPONENTS))) - result.total_revenue_change_rub
    if result.loc[result.comparison_complete, "change_reconciliation_gap_rub"].abs().gt(TOLERANCE_RUB).any():
        raise ValueError("Revenue contributions do not reconcile to the change in total revenue")
    result["_order"] = result.component_code.map({name: i for i, name in enumerate(COMPONENTS)})
    return result.sort_values(KEYS + ["_order"]).drop(columns="_order").reset_index(drop=True)


def detailed_balance_bridge(fiscal: pd.DataFrame, revenue, region_id: str,
                            period, basis: str = "YTD") -> tuple[pd.DataFrame, str | None]:
    """Explain Δbalance with five revenue changes and −Δexpenditure.

    The two source surfaces must agree on revenue totals and the exact end
    provenance for both years. Q2 additionally checks both Q1 start endpoints.
    An incomplete component withholds the detailed bridge, not the available
    published component levels. Raw source rows and research status stay intact.
    """
    simple, reason = budget_balance_bridge(fiscal, region_id, period, basis)
    if reason:
        return pd.DataFrame(), reason
    date = pd.Timestamp(period)
    components = revenue_component_history(revenue, basis, region_id)
    current = components.loc[components.period.eq(date)].copy()
    if len(current) != len(COMPONENTS) or not current.comparison_complete.all():
        return pd.DataFrame(), "A same-window revenue component is unavailable; the detailed balance bridge is withheld."
    summary = _observations(fiscal.loc[fiscal.region_id.eq(region_id)], basis)
    summary = summary.loc[summary.kind.eq("summary") & summary.metric_code.eq("revenue_total")].set_index("period")
    for selected_date, prefix in ((date, ""), (date - pd.DateOffset(years=1), "prior_")):
        reported = summary.loc[selected_date]
        source_row = current.iloc[0]
        if abs(reported.amount_rub - source_row[prefix + "total_revenue_rub"]) > TOLERANCE_RUB:
            raise ValueError("Fiscal and revenue source totals disagree")
        fiscal_evidence = json.loads(reported.source_evidence)
        revenue_evidence = json.loads(source_row[prefix + "total_source_evidence"])
        for endpoint in ("end", "start"):
            f, r = fiscal_evidence.get(endpoint), revenue_evidence.get(endpoint)
            if (f is None) != (r is None):
                raise ValueError("Fiscal and revenue comparison endpoints disagree")
            if f is not None and any(f.get(field) != r.get(field) for field in
                                     ("period", "source_file", "source_sha256", "source_member", "reporting_cutoff")):
                raise ValueError("Fiscal and revenue provenance disagree")
    context = simple.iloc[0].to_dict()
    position = context["start_balance_rub"]
    rows = []
    for step, row in enumerate(current.itertuples(index=False), start=1):
        end = position + row.change_yoy_rub
        rows.append({**context, "step": step, "label": row.component_name, "role": "change",
                     "value_rub": row.change_yoy_rub, "from_rub": position, "to_rub": end,
                     "component_code": row.component_code,
                     "current_amount_rub": row.amount_rub, "prior_amount_rub": row.prior_amount_rub,
                     "source_evidence": row.source_evidence, "prior_source_evidence": row.prior_source_evidence})
        position = end
    final_position = position - context["expenditure_change_rub"]
    if abs(final_position - context["end_balance_rub"]) > TOLERANCE_RUB:
        raise ValueError("Detailed balance bridge does not reconcile")
    spending = simple.loc[simple.label.eq("Expenditure change")].iloc[0].to_dict()
    rows.append({**spending, "step": 6, "from_rub": position, "to_rub": final_position,
                 "component_code": "expenditure", "current_amount_rub": np.nan, "prior_amount_rub": np.nan})
    anchors = [
        {**simple.iloc[0].to_dict(), "component_code": "prior_balance", "current_amount_rub": np.nan, "prior_amount_rub": np.nan},
        {**simple.iloc[-1].to_dict(), "step": 7, "component_code": "current_balance", "current_amount_rub": np.nan, "prior_amount_rub": np.nan},
    ]
    result = pd.DataFrame([anchors[0], *rows, anchors[1]])
    result["quality_status"] = "candidate_not_promoted"
    return result, None


def render_revenue_diagnostics(st, alt, fiscal, revenue, region_id, period, basis):
    """One focused driver chart; exact values and provenance stay inspectable."""
    history = revenue_component_history(revenue, basis, region_id)
    current = history.loc[history.period.eq(pd.Timestamp(period))].copy()
    st.subheader("What changed in revenue?")
    if current.empty or not current.comparison_complete.all():
        st.info("The same-window revenue decomposition is unavailable; missing inputs are not treated as zero.")
        return
    # Reconcile the revenue surface against the actual-balance surface before
    # displaying or exporting driver claims; this also validates Q2 endpoints.
    detailed, reason = detailed_balance_bridge(fiscal, revenue, region_id, period, basis)
    if reason:
        st.info(reason)
        return
    window = current.window.iloc[0]
    year = pd.Timestamp(period).year
    st.caption(f"{window} {year} versus {window} {year - 1} · nominal change in RUB bn")
    plot = current.assign(change_bn=current.change_yoy_rub / 1e9)
    bars = alt.Chart(plot).mark_bar(color="#2455A4").encode(
        y=alt.Y("component_name:N", sort=list(row[0] for row in COMPONENTS.values()), title=None,
                axis=alt.Axis(labelLimit=260)),
        x=alt.X("change_bn:Q", title=None, scale=alt.Scale(zero=True)),
        tooltip=[alt.Tooltip("component_name:N", title="Component"),
                 alt.Tooltip("change_bn:Q", title="Change (RUB bn)", format="+.3f"),
                 alt.Tooltip("contribution_to_revenue_growth_pp:Q", title="Contribution to revenue growth (pp)", format="+.2f")])
    zero = alt.Chart(pd.DataFrame({"zero": [0]})).mark_rule(color="#475569", strokeWidth=1).encode(x="zero:Q")
    st.altair_chart((bars + zero).properties(height=270), width="stretch")
    st.caption("Five non-overlapping components sum to the change in total revenue. The two ‘other’ components are explicit residuals, not additional published tax categories.")
    st.caption("Nominal accounting contributions, not causal explanations or plan-based shortfalls. Transfers are from other budgets, not proven federal-only.")
    with st.expander("Revenue contribution values and source evidence"):
        shown = current[["component_name", "amount_rub", "prior_amount_rub", "change_yoy_rub", "contribution_to_revenue_growth_pp"]].copy()
        for name in ("amount_rub", "prior_amount_rub", "change_yoy_rub"):
            shown[name] = shown[name].map(lambda v: f"{v / 1e9:+,.3f}" if pd.notna(v) else "Unavailable")
        shown["contribution_to_revenue_growth_pp"] = shown.contribution_to_revenue_growth_pp.map(
            lambda v: f"{v:+,.2f}" if pd.notna(v) else "Unavailable")
        st.dataframe(shown.rename(columns={"component_name": "Component", "amount_rub": "Actual (RUB bn)",
            "prior_amount_rub": "Prior year (RUB bn)", "change_yoy_rub": "Change (RUB bn)",
            "contribution_to_revenue_growth_pp": "Revenue-growth contribution (pp)"}), hide_index=True, width="stretch")
        st.caption("Signed negative receipts and residuals remain visible; percentage growth is withheld for a nonpositive prior denominator.")
        for title, frame, name in (("Revenue contributions", current, "revenue_contributions"),
                                   ("Detailed balance bridge", detailed, "detailed_balance_bridge")):
            st.download_button(f"Download {title} (CSV)", frame.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"fiscal_{region_id}_{pd.Timestamp(period):%Y-%m-%d}_{basis}_{name}.csv", mime="text/csv")
