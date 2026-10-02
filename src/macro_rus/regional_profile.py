"""Compact, descriptive regional profiles from existing reviewed datasets.

No source acquisition, national aggregation, inflation adjustment, prediction or
causal attribution. Each series retains its own observed reporting dates.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .revenue_view import PIT, CIT, OWN, TRANSFERS, NON_REPAYABLE, revenue_indicators
from .fiscal_view import FUNCTIONS, period_label

MONTHLY_YOY = "same_month_previous_year_pct"


def production_history(frame):
    """Published monthly headline only; retain missing months as gaps."""
    columns = ["period", "index_value", "growth_yoy_pct", "source_id", "source_vintage", "segment"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    work = frame.loc[frame.okved_section.eq("TOTAL") & frame.index_measure.eq(MONTHLY_YOY)].copy()
    if "frequency" in work:
        work = work.loc[work.frequency.eq("monthly")]
    if work.empty:
        return pd.DataFrame(columns=columns)
    work["period"] = pd.to_datetime(work.period, errors="raise")
    if work.period.isna().any() or work.duplicated("period").any():
        raise ValueError("Monthly headline production requires a unique dated observation.")
    work["index_value"] = pd.to_numeric(work.index_value, errors="coerce").where(lambda s: np.isfinite(s))
    # No averaging sector indices or monthly YoY rates into quarterly output.
    work["growth_yoy_pct"] = work.index_value - 100
    if not work.growth_yoy_pct.notna().any():
        return pd.DataFrame(columns=columns)
    end = work.loc[work.growth_yoy_pct.notna(), "period"].max()
    work = work.loc[work.period.le(end)].set_index("period").sort_index()
    calendar = pd.date_range(work.index.min(), end, freq="ME")
    work = work.reindex(calendar).rename_axis("period").reset_index()
    # Separate line paths prevent Vega from bridging unobserved months.
    present = work.growth_yoy_pct.notna()
    work["segment"] = present.ne(present.shift(fill_value=False)).cumsum()
    return work


def budget_history(frame):
    """Normalize published parent concepts without summing overlapping rows."""
    from .dashboard import classify_revenue

    if frame is None or frame.empty:
        return pd.DataFrame()
    work = frame.copy()
    if "budget_level" in work:
        scope = work.budget_level.fillna("").str.lower().str.contains("consolidat|консолид", regex=True)
        work = work.loc[scope].copy()
    if work.empty:
        return pd.DataFrame()
    mapping = {"Total revenue": "TOTAL_REVENUE", "Own-source revenue": OWN,
               "PIT": PIT, "CIT": CIT, "Non-repayable receipts": NON_REPAYABLE,
               "Transfers from other budgets": TRANSFERS}
    work["revenue_code"] = [mapping.get(classify_revenue(row)) for row in work.to_dict("records")]
    work = work.dropna(subset=["revenue_code"])
    if work.empty:
        return pd.DataFrame()
    return revenue_indicators(work)


def fiscal_history(frame):
    """Actual summary/function rows, with like-period prior-year changes."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    work = frame.loc[frame.budget_level.eq("consolidated_regional_budget")].copy()
    if work.empty:
        return work
    work["period"] = pd.to_datetime(work.period, errors="raise")
    keys = ["region_id", "period", "kind", "metric_code"]
    if work.duplicated(keys).any():
        raise ValueError("Fiscal profile requires one selected vintage per observation.")
    prior = work[keys + ["actual_ytd_rub"]].copy()
    prior["period"] += pd.DateOffset(years=1)
    prior = prior.rename(columns={"actual_ytd_rub": "prior_year_ytd_rub"})
    work = work.merge(prior, on=keys, how="left", validate="one_to_one")
    work["growth_yoy_pct"] = ((work.actual_ytd_rub / work.prior_year_ytd_rub - 1) * 100).where(work.prior_year_ytd_rub.gt(0))
    return work


def same_period_history(frame, period):
    if frame.empty or period is None:
        return frame.copy()
    cutoff = pd.Timestamp(period)
    return frame.loc[frame.period.dt.month.eq(cutoff.month) & frame.period.le(cutoff)].copy()


def fiscal_comparison(revenues, fiscal, period):
    """Own-source revenue and expenditure in a common cumulative window."""
    columns = ["period", "Year", "Measure", "RUB bn"]
    if period is None:
        return pd.DataFrame(columns=columns)
    r = same_period_history(revenues, period)
    f = same_period_history(fiscal, period)
    # Separate fiscal and revenue adapters must agree before being shown together.
    if not r.empty and not f.empty:
        check = r.loc[r.revenue_code.eq("TOTAL_REVENUE"), ["period", "actual_ytd_rub"]].merge(
            f.loc[f.metric_code.eq("revenue_total"), ["period", "actual_ytd_rub"]],
            on="period", suffixes=("_revenue", "_fiscal"), validate="one_to_one")
        if (check.actual_ytd_rub_revenue - check.actual_ytd_rub_fiscal).abs().gt(.02).any():
            raise ValueError("Revenue and spending source totals disagree for the selected reporting window.")
    pieces = []
    if not r.empty:
        own = r.loc[r.revenue_code.eq(OWN), ["period", "actual_ytd_rub"]].copy()
        own["Measure"] = "Own-source revenue"
        pieces.append(own)
    if not f.empty:
        exp = f.loc[f.metric_code.eq("expenditure_total"), ["period", "actual_ytd_rub"]].copy()
        exp["Measure"] = "Expenditure"
        pieces.append(exp)
    if not pieces:
        return pd.DataFrame(columns=columns)
    result = pd.concat(pieces, ignore_index=True)
    result["Year"] = result.period.dt.year.astype(str)
    result["RUB bn"] = result.actual_ytd_rub / 1e9
    return result


def transfer_share(revenues, period):
    """Intergovernmental transfers / published total, not all gratuitous receipts."""
    if revenues.empty or period is None:
        return None
    selected = revenues.loc[revenues.period.eq(period)].set_index("revenue_code")
    if not {"TOTAL_REVENUE", TRANSFERS}.issubset(selected.index):
        return None
    total = selected.loc["TOTAL_REVENUE", "actual_ytd_rub"]
    transfer = selected.loc[TRANSFERS, "actual_ytd_rub"]
    return float(transfer / total * 100) if pd.notna(total) and total > 0 and pd.notna(transfer) else None


def profile_notes(production, revenues, fiscal, drivers, period):
    """Short factual context; no scores, thresholds or causal explanations."""
    notes = []
    if not production.empty:
        recent = production.tail(3)
        if len(recent) == 3 and recent.growth_yoy_pct.notna().all():
            below = int(recent.growth_yoy_pct.lt(0).sum())
            description = "above its year-earlier level in each of the" if recent.growth_yoy_pct.gt(0).all() else f"below its year-earlier level in {below} of the"
            notes.append(f"Industrial production was {description} latest three months ({recent.period.min():%b}–{recent.period.max():%b %Y}).")
    if not revenues.empty and period is not None:
        current = revenues.loc[revenues.period.eq(period)].set_index("revenue_code")
        own = current.loc[OWN, "growth_yoy_pct"] if OWN in current.index else np.nan
        exp = fiscal.loc[fiscal.period.eq(period) & fiscal.metric_code.eq("expenditure_total")] if not fiscal.empty else pd.DataFrame()
        spending = exp.growth_yoy_pct.iloc[0] if not exp.empty else np.nan
        if pd.notna(own) and pd.notna(spending):
            notes.append(f"Own-source revenue changed {own:+.1f}% and expenditure {spending:+.1f}% versus the same cumulative period last year; both are nominal.")
        current_share = transfer_share(revenues, period)
        prior_share = transfer_share(revenues, pd.Timestamp(period) - pd.DateOffset(years=1))
        if current_share is not None and prior_share is not None:
            notes.append(f"The intergovernmental transfer share changed {current_share - prior_share:+.1f} percentage points versus the same period last year.")
    if not drivers.empty and len(notes) < 3:
        comparable = drivers.dropna(subset=["change_rub"])
        if not comparable.empty:
            notes.append(f"PIT receipts fell in {int(comparable.change_rub.lt(0).sum())} of {len(comparable)} industries with a same-period comparison. This excludes unreported industries.")
    return notes[:3]


def profile_evidence(production, revenues, fiscal, pit, period):
    """Long-form export of exactly the series/windows used on the profile."""
    pieces = []
    for name, frame, indicator, value, units, basis in (
        ("industrial_production", production, "okved_section", "index_value", "index, previous-year month = 100", "monthly"),
        ("budget_revenue", same_period_history(revenues, period), "revenue_code", "actual_ytd_rub", "RUB", "YTD"),
        ("fiscal_observations", same_period_history(fiscal, period), "metric_code", "actual_ytd_rub", "RUB", "YTD"),
        ("industry_pit", pit, "okved_section", "pit_flow_rub", "RUB", "observed period flow"),
    ):
        if frame is None or frame.empty:
            continue
        selected = frame.copy()
        selected["dataset"] = name
        selected["indicator"] = selected[indicator]
        selected["value"] = selected[value] if value in selected else np.nan
        selected["units"] = units
        selected["amount_basis"] = basis
        wanted = ["dataset", "region_id", "period", "indicator", "value", "units", "amount_basis",
                  "source_id", "source_vintage", "source_file", "source_sha256", "source_member",
                  "index_measure", "quality_status", "growth_yoy_pct", "prior_year_ytd_rub"]
        pieces.append(selected[[c for c in wanted if c in selected]])
    return pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()


def render_regional_profile(st, alt, bundle, region_id):
    from .dashboard import (filter_region, pit_summary, industry_snapshot, source_summary,
                            _format_rub, _format_pct, _format_delta, BLUE, ORANGE, INK)

    pit_frame = filter_region(bundle.get("pit_receipts"), region_id)
    production = production_history(filter_region(bundle.get("industrial_production"), region_id))
    revenues = budget_history(filter_region(bundle.get("budget_execution"), region_id))
    fiscal = fiscal_history(filter_region(bundle.get("fiscal_observations"), region_id))
    dates = sorted(set(revenues.period) | set(fiscal.period) if not revenues.empty and not fiscal.empty
                   else set(revenues.period) if not revenues.empty else set(fiscal.period) if not fiscal.empty else set(), reverse=True)
    period = st.selectbox("Fiscal profile period", dates, format_func=period_label) if dates else None
    st.caption("The fiscal selector changes budget panels only. Economic indicators retain their own latest dates. Monetary figures are nominal; sources update at different times.")
    summary = pit_summary(pit_frame)
    observed = production.loc[production.growth_yoy_pct.notna()] if not production.empty else production
    output = observed.iloc[-1] if not observed.empty else None
    budget = revenues.loc[revenues.period.eq(period)].set_index("revenue_code") if not revenues.empty else pd.DataFrame()
    retained = budget.loc[PIT] if PIT in budget.index else None
    cards = st.columns(4)
    pit_period = (f"Jan–Mar {summary['period']:%Y}" if summary["period"] is not None and pd.Timestamp(summary["period"]).month == 3
                  else pd.Timestamp(summary["period"]).strftime("%b %Y") if summary["period"] is not None else "unavailable")
    cards[0].metric("Reported industry PIT", _format_rub(summary["value"]),
                    _format_delta(summary["yoy_pct"], "same-period YoY"), delta_color="off",
                    help="Sum of reported industries, not a reconciled regional PIT total. Not a direct output or welfare measure.")
    cards[0].caption(pit_period)
    cards[1].metric("Industrial production",
                    _format_pct(output.growth_yoy_pct if output is not None else None),
                    help="Monthly output change versus the same month last year. Published headline index minus 100, not month-on-month growth.")
    cards[1].caption(f"{output.period:%b %Y} · same-month YoY" if output is not None else "Period unavailable")
    cards[2].metric("Retained PIT",
                    _format_rub(retained.actual_ytd_rub if retained is not None else None),
                    _format_delta(retained.growth_yoy_pct if retained is not None else None, "same-period YoY"), delta_color="off")
    cards[2].caption(period_label(period) if period is not None else "Period unavailable")
    cards[3].metric("Transfer share",
                    _format_pct(transfer_share(revenues, period)),
                    help="Transfers from other budgets (202) / total revenue. This excludes other non-repayable receipts.")
    cards[3].caption(period_label(period) if period is not None else "Period unavailable")
    if summary["coverage_changed"] or summary["missing_sections"]:
        st.warning("Industry PIT is incomplete: missing sections " + (", ".join(summary["missing_sections"]) or "or changed coverage") + ". The level sums reported industries only; aggregate YoY is withheld when coverage changes.")
    st.caption("Industry PIT (FNS) and retained budget PIT (Treasury) have different scopes. Production is a monthly YoY change; budget figures are cumulative.")

    columns = st.columns(2)
    shown_production = production.copy()
    with columns[0]:
        st.subheader("Industrial production trend")
        if production.empty:
            st.info("A published monthly headline YoY series is unavailable. Sector indices are not averaged into a substitute.")
        else:
            shown_production = production.loc[production.period.gt(output.period - pd.DateOffset(months=24))].copy()
            endpoints = [shown_production.period.min(), shown_production.period.max()]
            ticks = sorted(set(endpoints + [d for d in shown_production.period if d.month == 1]))
            tick_values = [alt.DateTime(year=d.year, month=d.month, date=d.day) for d in ticks]
            line = alt.Chart(shown_production).mark_line(color=BLUE, point=True).encode(
                x=alt.X("period:T", title=None, axis=alt.Axis(format="%b %Y", labelAngle=0, values=tick_values)),
                y=alt.Y("growth_yoy_pct:Q", title="Same-month YoY (%)", scale=alt.Scale(zero=True)),
                detail="segment:N", tooltip=[alt.Tooltip("period:T", title="Month", format="%b %Y"),
                    alt.Tooltip("growth_yoy_pct:Q", title="YoY (%)", format="+.1f")])
            zero = alt.Chart(pd.DataFrame({"zero": [0]})).mark_rule(color=INK).encode(y=alt.Y("zero:Q", title="Same-month YoY (%)"))
            st.altair_chart((line + zero).properties(height=260, description="Monthly production growth versus the same month last year; gaps remain gaps."), width="stretch")
            st.caption(f"{shown_production.period.min():%b %Y}–{shown_production.period.max():%b %Y} · published headline, same month last year = 100")
    with columns[1]:
        st.subheader("Revenue and spending history")
        fiscal_for_notes = fiscal
        try:
            comparison = fiscal_comparison(revenues, fiscal, period)
        except ValueError as exc:
            comparison = pd.DataFrame()
            fiscal_for_notes = pd.DataFrame()
            st.warning(str(exc) + " Comparison withheld.")
        if comparison.empty:
            st.info("Same-period fiscal history is unavailable.")
        else:
            chart = alt.Chart(comparison).mark_bar().encode(
                x=alt.X("Year:N", title=None, axis=alt.Axis(labelAngle=0)), xOffset="Measure:N",
                y=alt.Y("RUB bn:Q", scale=alt.Scale(zero=True)),
                color=alt.Color("Measure:N", scale=alt.Scale(domain=["Own-source revenue", "Expenditure"], range=[BLUE, ORANGE]), legend=alt.Legend(title=None, orient="top")),
                tooltip=["Year", "Measure", alt.Tooltip("RUB bn:Q", format=".2f")])
            st.altair_chart(chart.properties(height=260, description="Own-source revenue and expenditure in the same cumulative reporting period each year."), width="stretch")
            st.caption(f"Jan–{pd.Timestamp(period):%b} (cumulative) in each displayed year · RUB bn · regional + municipal budgets, excluding health-insurance funds")
            st.caption("Own-source revenue excludes transfers. The gap between these bars is not the budget balance.")
            if fiscal.empty:
                st.caption("Expenditure has not been integrated for this profile; only own-source revenue is shown.")

    drivers = industry_snapshot(pit_frame)
    st.subheader("Main changes")
    notes = profile_notes(production, revenues, fiscal_for_notes, drivers, period)
    for note in notes:
        st.write("• " + note)
    selected_fiscal = fiscal.loc[fiscal.period.eq(period)] if not fiscal.empty else fiscal
    if not selected_fiscal.empty:
        balance = selected_fiscal.loc[selected_fiscal.metric_code.eq("budget_balance")]
        if not balance.empty:
            value = balance.actual_ytd_rub.iloc[0]
            label = "surplus" if value > 0 else "deficit" if value < 0 else "balanced"
            st.write(f"Budget balance: {_format_rub(value)} ({label}) for {period_label(period)}.")
            st.caption("Balance = total revenue minus expenditure. A deficit alone does not establish a revenue shortfall against plan.")
        spending = selected_fiscal.loc[selected_fiscal.kind.eq("expenditure_function")].copy()
        total = selected_fiscal.loc[selected_fiscal.metric_code.eq("expenditure_total"), "actual_ytd_rub"]
        if not spending.empty and not total.empty:
            spending["Spending function"] = spending.metric_code.map(FUNCTIONS).fillna(spending.name_ru)
            spending["RUB bn"] = spending.actual_ytd_rub / 1e9
            spending["Share (%)"] = (spending.actual_ytd_rub / total.iloc[0] * 100) if total.iloc[0] > 0 else np.nan
            with st.expander("Where spending went — detailed table"):
                st.dataframe(spending[["Spending function", "name_ru", "RUB bn", "Share (%)"]].sort_values("RUB bn", ascending=False).round(2), hide_index=True, width="stretch")
                st.caption("Regional-budget functions only; federal expenditure and territorial health-insurance funds are outside this perimeter.")

    if not drivers.empty:
        matched = drivers.dropna(subset=["change_rub"])
        if not matched.empty:
            st.subheader("Largest changes in reported industry PIT")
            leading = matched.reindex(matched.change_rub.abs().sort_values(ascending=False).index).head(6).copy()
            leading["Change (RUB bn)"] = leading.change_rub / 1e9
            leading["Direction"] = np.where(leading.change_rub.lt(0), "Decrease", "Increase")
            chart = alt.Chart(leading).mark_bar().encode(
                y=alt.Y("industry:N", sort="-x", title=None, axis=alt.Axis(labelLimit=340)),
                x=alt.X("Change (RUB bn):Q", scale=alt.Scale(zero=True)),
                color=alt.Color("Direction:N", scale=alt.Scale(domain=["Increase", "Decrease"], range=[BLUE, ORANGE]), legend=alt.Legend(title=None, orient="top")),
                tooltip=[alt.Tooltip("industry:N", title="Industry"), alt.Tooltip("industry_ru:N", title="Russian name"),
                    "okved_section", "Direction", alt.Tooltip("Change (RUB bn):Q", format="+.2f")])
            zero = alt.Chart(pd.DataFrame({"zero": [0]})).mark_rule(color=INK).encode(x=alt.X("zero:Q", title="Change (RUB bn)"))
            st.altair_chart((chart + zero).properties(height=260), width="stretch")
            st.caption(f"{pit_period} versus the same period one year earlier · nominal changes in comparable reported sections only, not a decomposition of total regional PIT")

    with st.expander("Profile tables, sources and limitations"):
        metadata = source_summary(bundle, region_id, ("pit_receipts", "industrial_production", "budget_execution"))
        st.write("Sources: " + metadata["sources"])
        st.write("Latest source vintages: " + metadata["vintages"])
        if not budget.empty and "source_vintage" in budget:
            st.write("Selected fiscal revenue vintages: " + ", ".join(budget.source_vintage.unique()))
        if not production.empty:
            st.dataframe(shown_production[["period", "index_value", "growth_yoy_pct", "source_vintage"]].round(2), hide_index=True, width="stretch")
        if not comparison.empty:
            st.dataframe(comparison[["Year", "Measure", "RUB bn"]].round(2), hide_index=True, width="stretch")
        if not drivers.empty:
            st.dataframe(drivers.round(2), hide_index=True, width="stretch")
        if not selected_fiscal.empty:
            st.write("Fiscal source files: " + ", ".join(selected_fiscal.source_file.unique()))
            st.write("Fiscal source hashes: " + ", ".join(selected_fiscal.source_sha256.unique()))
        if not budget.empty and "plan_components_reconciled" in budget and not budget.plan_components_reconciled.all():
            st.warning("Revenue plan components do not reconcile for this period. The profile uses actual receipts, not plan-execution percentages.")
        st.caption("No CPI adjustment, per-capita measure, debt-stock estimate or causal sanctions attribution. Original and amended budgets are not established. See the specialist pages for full source details.")
    source_pit = pit_frame
    if pit_frame is not None and summary["period"] is not None:
        source_pit = pit_frame.loc[pit_frame.period.isin([summary["period"], summary["period"] - pd.DateOffset(years=1)])].copy()
    # Calendar gaps remain blank but keep a region key in the exported window.
    shown_production["region_id"] = region_id
    if not shown_production.empty:
        shown_production["okved_section"] = "TOTAL"
        shown_production["index_measure"] = MONTHLY_YOY
    evidence = profile_evidence(shown_production, revenues, fiscal, source_pit, period)
    st.download_button("Download profile evidence (CSV)", evidence.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"regional_profile_{region_id}.csv", mime="text/csv")
    st.caption("Observed changes are descriptive. They do not establish sanctions effects or another cause.")
