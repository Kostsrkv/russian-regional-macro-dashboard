"""Review candidate loading and descriptive regional revenue calculations."""
from pathlib import Path
import json

import numpy as np
import pandas as pd

from .provenance import sha256_file
from .treasury_revenue import CORE_CODES, REVENUE_METRICS
from .validation import validate_tables

PIT = "10102000010000110"
CIT = "10101000000000110"
OWN = "10000000000000000"
NON_REPAYABLE = "20000000000000000"
TRANSFERS = "20200000000000000"


def revenue_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Same-period YoY and genuine quarterly flows; keep signed adjustments."""
    work = frame.copy()
    work["period"] = pd.to_datetime(work.period, errors="raise")
    if work.duplicated(["region_id", "period", "revenue_code", "budget_level"]).any():
        raise ValueError("Revenue calculation requires one selected vintage")
    keys = ["region_id", "period", "revenue_code", "budget_level"]
    prior = work[keys + ["actual_ytd_rub"]].copy()
    prior["period"] = prior.period + pd.DateOffset(years=1)
    prior = prior.rename(columns={"actual_ytd_rub": "prior_year_ytd_rub"})
    work = work.merge(prior, on=keys, how="left", validate="one_to_one")
    work["change_yoy_rub"] = work.actual_ytd_rub - work.prior_year_ytd_rub
    work["growth_yoy_pct"] = ((work.actual_ytd_rub / work.prior_year_ytd_rub - 1) * 100).where(work.prior_year_ytd_rub.gt(0))
    previous = frame[keys + ["actual_ytd_rub"]].copy()
    previous["period"] = pd.to_datetime(previous.period) + pd.offsets.QuarterEnd(1)
    previous = previous.rename(columns={"actual_ytd_rub": "prior_quarter_ytd_rub"})
    work = work.merge(previous, on=keys, how="left", validate="one_to_one")
    quarter_end = work.period.dt.is_quarter_end
    first = quarter_end & work.period.dt.month.eq(3)
    adjacent = quarter_end & ~first & work.prior_quarter_ytd_rub.notna()
    work["quarter_flow_rub"] = np.nan
    work.loc[first, "quarter_flow_rub"] = work.loc[first, "actual_ytd_rub"]
    work.loc[adjacent, "quarter_flow_rub"] = work.loc[adjacent, "actual_ytd_rub"] - work.loc[adjacent, "prior_quarter_ytd_rub"]
    work["flow_status"] = np.select([first, adjacent], ["Q1 equals YTD", "adjacent YTD difference"], default="missing adjacent snapshot")
    work["snapshot_execution_pct"] = (work.actual_ytd_rub / work.approved_plan_rub * 100).where(work.approved_plan_rub.gt(0))
    if "plan_components_reconciled" in work:
        work.loc[~work.plan_components_reconciled.eq(True), "snapshot_execution_pct"] = np.nan
    work["amount_basis"] = "YTD"
    work["units"] = "RUB"
    return work


def load_revenue_candidate(root: Path):
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("status") != "candidate_not_promoted" or manifest.get("blocking_failures") != 0:
        raise ValueError("Revenue candidate has not passed its extraction checks")
    lineage_file = manifest.get("lineage_file", "lineage.csv")
    if lineage_file not in {"lineage.csv", "lineage.csv.gz"}:
        raise ValueError("Unrecognized revenue lineage file")
    for filename in ("budget_execution.parquet", "sources.csv", lineage_file):
        if sha256_file(root / filename) != manifest["derived_hashes"][filename]:
            raise ValueError(f"Revenue candidate hash mismatch: {filename}")
    budget = pd.read_parquet(root / "budget_execution.parquet")
    sources = pd.read_csv(root / "sources.csv", keep_default_na=False)
    if not validate_tables({"budget_execution": budget, "sources": sources}).is_valid:
        raise ValueError("Revenue candidate failed canonical validation")
    if len(budget) != manifest["rows"] or budget.region_id.nunique() != manifest["eligible_regions"]:
        raise ValueError("Revenue candidate coverage mismatch")
    if not budget.source_id.isin(sources.source_id).all() or not budget.quality_status.eq("provisional").all():
        raise ValueError("Revenue source linkage or review status is invalid")
    if not budget.budget_level.eq("consolidated_regional_budget").all():
        raise ValueError("Revenue perimeter changed")
    for column in ("actual_ytd_rub", "approved_plan_rub"):
        if not np.isfinite(budget[column]).all():
            raise ValueError("Non-finite revenue measure")
    for _, group in budget.groupby(["region_id", "period"]):
        if not CORE_CODES.issubset(group.revenue_code):
            raise ValueError("Revenue core metric missing")
        totals = group.set_index("revenue_code")
        if abs(totals.loc["TOTAL_REVENUE", "actual_ytd_rub"] - totals.loc[OWN, "actual_ytd_rub"] - totals.loc[NON_REPAYABLE, "actual_ytd_rub"]) > .02:
            raise ValueError("Revenue actual components no longer reconcile")
        plan = totals.approved_plan_rub
        reconciled = abs(plan["TOTAL_REVENUE"] - plan[OWN] - plan[NON_REPAYABLE]) <= .02
        if not group.plan_components_reconciled.eq(reconciled).all():
            raise ValueError("Revenue plan warning flag differs from source amounts")
    lineage = pd.read_csv(root / lineage_file, dtype={"revenue_code": str})
    lineage["period"] = pd.to_datetime(lineage.period)
    registered = {s["source_file"]: s["sha256"] for s in manifest["sources"] if s["selected_html_crc_passed"]}
    if len(lineage) != len(budget) or not lineage.source_file.map(registered).eq(lineage.source_sha256).all():
        raise ValueError("Revenue lineage mismatch")
    return {"budget_execution": budget, "sources": sources, "revenue_lineage": lineage}, manifest


def render_revenue(st, alt, frame, region_id):
    regional = frame.loc[frame.region_id.eq(region_id)].copy()
    if regional.empty:
        st.info("Revenue observations are unavailable for this region.")
        return
    data = revenue_indicators(regional)
    period = st.selectbox("Revenue reporting period", sorted(data.period.unique(), reverse=True),
                          format_func=lambda p: f"Jan–{pd.Timestamp(p):%b %Y} (cumulative)")
    selected = data.loc[data.period.eq(period)].copy()
    values = selected.set_index("revenue_code")
    st.caption("Nominal RUB · regional + municipal budgets, excluding territorial health-insurance funds")
    st.caption("Selected source vintage: " + ", ".join(selected.source_vintage.unique()))
    for column, code in zip(st.columns(3), (PIT, CIT, OWN)):
        row = values.loc[code]
        growth = row.growth_yoy_pct
        column.metric(REVENUE_METRICS[code], f"{row.actual_ytd_rub / 1e9:,.2f} bn RUB",
                      delta=f"{growth:+.1f}% same-period YoY" if pd.notna(growth) else None, delta_color="off")
    total = values.loc["TOTAL_REVENUE", "actual_ytd_rub"]
    if TRANSFERS in values.index and total > 0:
        share = values.loc[TRANSFERS, "actual_ytd_rub"] / total * 100
        st.write(f"Transfers from other budgets: {share:.1f}% of total revenue in the selected period.")
    st.caption("Retained PIT is budget revenue. Industry PIT is a separate FNS measure; their scopes and latest dates can differ.")

    st.subheader("Revenue history in the same period each year")
    code = st.selectbox("Revenue indicator", list(REVENUE_METRICS), format_func=REVENUE_METRICS.get)
    history = data.loc[data.revenue_code.eq(code) & data.period.dt.month.eq(pd.Timestamp(period).month)
                       & data.period.le(period)].copy()
    if history.empty:
        st.info("This revenue category is not reported in the available history.")
    else:
        history["Year"] = history.period.dt.year.astype(str)
        history["RUB bn"] = history.actual_ytd_rub / 1e9
        chart = alt.Chart(history).mark_bar(color="#2455A4").encode(
            x=alt.X("Year:N", axis=alt.Axis(labelAngle=0), title=None),
            y=alt.Y("RUB bn:Q", scale=alt.Scale(zero=True)),
            tooltip=["Year", alt.Tooltip("RUB bn:Q", format=".2f")]).properties(height=250)
        st.altair_chart(chart, width="stretch")
        with st.expander("History table"):
            st.dataframe(history[["Year", "RUB bn", "growth_yoy_pct"]].rename(columns={"growth_yoy_pct": "Same-period YoY (%)"}).round(2), hide_index=True, width="stretch")

    st.subheader("Revenue composition")
    # Only two mutually exclusive parent rows. Taxes/transfers are nested detail.
    composition = selected.loc[selected.revenue_code.isin([OWN, NON_REPAYABLE])].copy()
    composition["RUB bn"] = composition.actual_ytd_rub / 1e9
    chart = alt.Chart(composition).mark_bar(color="#2455A4").encode(
        y=alt.Y("revenue_name_en:N", title=None, axis=alt.Axis(labelLimit=300)),
        x=alt.X("RUB bn:Q", scale=alt.Scale(zero=True)),
        tooltip=["revenue_name_en", alt.Tooltip("RUB bn:Q", format=".2f")]).properties(height=130)
    st.altair_chart(chart, width="stretch")
    st.caption("These two parent categories reconcile to total revenue. Transfers from other budgets are part of non-repayable receipts; other receipts and repayments are included there too.")
    detail = selected[["revenue_name_en", "actual_ytd_rub", "change_yoy_rub", "growth_yoy_pct"]].copy()
    detail["actual_ytd_rub"] /= 1e9
    detail["change_yoy_rub"] /= 1e9
    st.dataframe(detail.rename(columns={"revenue_name_en": "Revenue", "actual_ytd_rub": "Actual YTD (RUB bn)",
        "change_yoy_rub": "YoY change (RUB bn)", "growth_yoy_pct": "Same-period YoY (%)"}).round(2), hide_index=True, width="stretch")
    with st.expander("Execution against the reported snapshot plan"):
        st.caption("The plan is the approved assignment reported at this cutoff. Original budgets and amendments are not distinguished. Execution percentages alone do not establish a revenue shortfall.")
        if "plan_components_reconciled" in selected and not selected.plan_components_reconciled.all():
            st.warning("Published revenue plan components do not reconcile to the reported total. Execution percentages are withheld for this region and period; source plan amounts are retained for review.")
        plan = selected[["revenue_name_en", "approved_plan_rub", "snapshot_execution_pct"]].copy()
        plan["approved_plan_rub"] /= 1e9
        st.dataframe(plan.rename(columns={"revenue_name_en": "Revenue", "approved_plan_rub": "Snapshot plan (RUB bn)",
                                         "snapshot_execution_pct": "Execution (%)"}).round(2), hide_index=True, width="stretch")
    st.download_button("Download revenue history and calculated indicators (CSV)", data.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"revenue_{region_id}.csv", mime="text/csv")
