"""Social-sector budget functions, not an estimate of all welfare spending."""
from __future__ import annotations

import numpy as np
import pandas as pd

FUNCTIONS = {"1000": "Social policy", "0700": "Education", "0900": "Health"}


def social_history(frame: pd.DataFrame, region_id: str, period) -> pd.DataFrame:
    """Use three disjoint parent functions and exactly the same YTD window."""
    if frame is None or frame.empty or period is None:
        return pd.DataFrame()
    data = frame.loc[frame.region_id.eq(region_id)].copy()
    if not data.budget_level.eq("consolidated_regional_budget").all():
        raise ValueError("Social spending requires consolidated regional budgets.")
    data["period"] = pd.to_datetime(data.period, errors="raise")
    if data.duplicated(["period", "kind", "metric_code"]).any():
        raise ValueError("Social spending observation grain must be unique.")
    cutoff = pd.Timestamp(period)
    data = data.loc[data.period.dt.month.eq(cutoff.month) & data.period.dt.day.eq(cutoff.day) & data.period.le(cutoff)]
    dates = sorted(set(data.period) | {cutoff})
    grid = pd.MultiIndex.from_product([dates, FUNCTIONS], names=["period", "metric_code"]).to_frame(index=False)
    selected = data.loc[data.kind.eq("expenditure_function") & data.metric_code.isin(FUNCTIONS)]
    result = grid.merge(selected, on=["period", "metric_code"], how="left", validate="one_to_one")
    totals = data.loc[data.kind.eq("summary") & data.metric_code.eq("expenditure_total"), ["period", "actual_ytd_rub"]].rename(columns={"actual_ytd_rub": "total_expenditure_rub"})
    result = result.merge(totals, on="period", how="left", validate="many_to_one")
    result["region_id"] = region_id
    result["function"] = result.metric_code.map(FUNCTIONS)
    result["share_pct"] = result.actual_ytd_rub / result.total_expenditure_rub.where(result.total_expenditure_rub.gt(0)) * 100
    prior = result[["period", "metric_code", "actual_ytd_rub", "share_pct"]].copy()
    prior["period"] += pd.DateOffset(years=1)
    prior = prior.rename(columns={"actual_ytd_rub": "prior_year_actual_rub", "share_pct": "prior_year_share_pct"})
    result = result.merge(prior, on=["period", "metric_code"], how="left", validate="one_to_one")
    result["growth_yoy_pct"] = (result.actual_ytd_rub / result.prior_year_actual_rub.where(result.prior_year_actual_rub.gt(0)) - 1) * 100
    result["share_change_pp"] = result.share_pct - result.prior_year_share_pct
    result["RUB bn"] = result.actual_ytd_rub / 1e9
    result["Year"] = result.period.dt.year.astype(str)
    result["availability"] = np.where(result.actual_ytd_rub.notna(), "reported", "unavailable")
    return result.sort_values(["period", "metric_code"]).reset_index(drop=True)


def render_social_spending(st, alt, frame, region_id):
    from .fiscal_view import period_label
    from .dashboard import _format_rub, _format_pct, _format_delta

    st.subheader("Social-sector spending")
    st.info("Research review dataset · actual expenditure, not budget allocations or all public social spending.")
    regional = frame.loc[frame.region_id.eq(region_id)]
    if regional.empty:
        st.info("No Treasury observations for this region.")
        return
    period = st.selectbox("Social-spending period", sorted(pd.to_datetime(regional.period).unique(), reverse=True), format_func=period_label)
    try:
        history = social_history(frame, region_id, period)
    except (ValueError, KeyError) as exc:
        st.error(f"Social-spending view withheld: {exc}")
        return
    current = history.loc[history.period.eq(period)].set_index("metric_code")
    for col, (code, label) in zip(st.columns(3), FUNCTIONS.items()):
        row = current.loc[code]
        col.metric(label, _format_rub(row.actual_ytd_rub), _format_delta(row.growth_yoy_pct, "nominal same-period YoY"), delta_color="off")
        col.caption(f"{_format_pct(row.share_pct)} of total expenditure · function {code}")
    st.caption(f"{period_label(period)} · nominal RUB · regional + municipal budgets. Federal expenditure and territorial health-insurance funds are excluded.")
    st.subheader("Same reporting window across years")
    chart = alt.Chart(history).mark_bar().encode(
        x=alt.X("Year:N", title=None, axis=alt.Axis(labelAngle=0)),
        xOffset=alt.XOffset("function:N", sort=list(FUNCTIONS.values())),
        y=alt.Y("RUB bn:Q", scale=alt.Scale(zero=True)),
        color=alt.Color("function:N", title=None, scale=alt.Scale(domain=list(FUNCTIONS.values()), range=["#2455A4", "#C66A19", "#68783B"]), legend=alt.Legend(orient="top")),
        tooltip=["Year", alt.Tooltip("function:N", title="Function"), alt.Tooltip("RUB bn:Q", format=".3f"), alt.Tooltip("share_pct:Q", title="Expenditure share (%)", format=".1f"), "availability:N"])
    st.altair_chart(chart.properties(height=300, description="Three separate budget functions, compared in the same cumulative window. Missing observations are not zero."), width="stretch")
    st.caption("Bar order is social policy, education, health in every year. Q1 is compared only with Q1; first half only with first half. No inflation adjustment is available yet.")
    display = history[["Year", "function", "metric_code", "name_ru", "RUB bn", "share_pct", "growth_yoy_pct", "share_change_pp", "availability"]].rename(columns={"function": "Function", "name_ru": "Russian name", "share_pct": "Share (%)", "growth_yoy_pct": "Nominal YoY (%)", "share_change_pp": "Share change (pp)"})
    for column in ["RUB bn", "Share (%)", "Nominal YoY (%)", "Share change (pp)"]:
        display[column] = display[column].map(lambda value: f"{value:,.2f}" if pd.notna(value) else "Unavailable")
    display["Russian name"] = display["Russian name"].fillna("Unavailable")
    st.dataframe(display, hide_index=True, width="stretch")
    st.caption("Social policy (1000) is a budget classification, not total social protection in the region. Education and health are kept separate. These figures do not identify military recruitment payments or military contracts.")
    with st.expander("Sources and interpretation limits"):
        st.write("Source: Federal Treasury · " + ", ".join(history.source_file.dropna().unique()))
        st.write("Reporting cutoffs: " + ", ".join(history.reporting_cutoff.dropna().astype(str).unique()))
        st.caption("Archive cutoffs are known; publication/retrieval dates are not established. Source hashes and selected HTML members are included in the download. Plan reconciliation warnings do not affect these actual-expenditure calculations; no plan-based shortfall is inferred.")
    st.download_button("Download social-spending evidence (CSV)", history.to_csv(index=False).encode("utf-8-sig"), file_name=f"social_spending_{region_id}_{pd.Timestamp(period):%Y-%m-%d}.csv", mime="text/csv")
