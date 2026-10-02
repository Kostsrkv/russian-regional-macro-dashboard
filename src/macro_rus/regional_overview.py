"""Alphabetical eligible-region lookup, not a ranking or national aggregate."""
from __future__ import annotations

import pandas as pd

from .fiscal_analysis import transfer_history


def regional_status_table(fiscal, revenue, cumulative, period):
    """Keep a shared YTD cutoff across every region and every displayed measure."""
    date = pd.Timestamp(period)
    transfers = transfer_history(revenue, basis="YTD")
    transfers = transfers.loc[transfers.period.eq(date)].copy()
    regions = fiscal[["region_id", "region_name_ru"]].drop_duplicates()
    if regions.region_id.duplicated().any():
        raise ValueError("Regional overview has ambiguous names")
    current = fiscal.loc[pd.to_datetime(fiscal.period).eq(date) & fiscal.kind.eq("summary")
        & fiscal.metric_code.eq("budget_balance")].copy()
    if current.region_id.duplicated().any():
        raise ValueError("Regional overview requires one balance per region")
    if not current.budget_level.eq("consolidated_regional_budget").all():
        raise ValueError("Regional overview requires the consolidated regional scope")
    balance = current[["region_id", "actual_ytd_rub", "source_file", "source_sha256", "source_member", "reporting_cutoff"]].rename(
        columns={"actual_ytd_rub": "budget_balance_rub", **{key: "balance_" + key for key in ["source_file", "source_sha256", "source_member", "reporting_cutoff"]}})
    production = cumulative.loc[pd.to_datetime(cumulative.period).eq(date) & cumulative.okved_section.eq("TOTAL")].copy()
    if production.region_id.duplicated().any():
        raise ValueError("Regional overview requires one selected production vintage")
    if not production.index_measure.eq("same_period_previous_year_pct").all() or not production.frequency.eq("YTD").all():
        raise ValueError("Regional overview requires source-native YTD production")
    if not pd.to_datetime(production.window_start).eq(pd.Timestamp(date.year, 1, 1)).all():
        raise ValueError("Regional overview production windows must start on 1 January")
    fields = ["region_id", "index_value", "source_file", "source_sha256", "source_sheet", "source_cell", "source_vintage", "index_base", "availability"]
    production = production[fields].rename(columns={key: "production_"+key for key in fields if key != "region_id"})
    result = regions.merge(transfers, on="region_id", how="left", validate="one_to_one")
    result = result.merge(balance, on="region_id", how="left", validate="one_to_one")
    result = result.merge(production, on="region_id", how="left", validate="one_to_one")
    result["period"] = date
    result["window_start"] = pd.Timestamp(date.year, 1, 1)
    result["production_growth_yoy_pct"] = result.production_index_value - 100
    result["amount_basis"] = "YTD"
    result["quality_status"] = "candidate_not_promoted"
    return result.sort_values("region_name_ru").reset_index(drop=True)


def render_regional_overview(st, fiscal, revenue, cumulative):
    st.subheader("Eligible-region overview")
    st.caption("Optional lookup · alphabetical order · no rankings, national totals or geographic map")
    periods = sorted(pd.to_datetime(fiscal.period).unique(), reverse=True)
    selected = st.selectbox("Overview reporting period", periods,
        format_func=lambda p: f"January–{pd.Timestamp(p):%B %Y}")
    try:
        data = regional_status_table(fiscal, revenue, cumulative, selected)
    except (ValueError, KeyError, TypeError) as exc:
        st.error(f"Regional lookup withheld: {exc}")
        return
    st.caption(f"{len(data)} eligible regions · same January-to-cutoff window · nominal RUB · research review dataset")
    st.caption("Treasury: consolidated regional + municipal budgets, excluding territorial health-insurance funds. Rosstat: source-native cumulative headline production, same period previous year = 100.")
    shown = data[["region_name_ru", "own_source_rub", "own_source_growth_yoy_pct", "transfer_share_pct", "budget_balance_rub", "production_growth_yoy_pct"]].copy()
    for name in ["own_source_rub", "budget_balance_rub"]:
        shown[name] = shown[name].map(lambda n: f"{n/1e9:,.2f}" if pd.notna(n) else "Unavailable")
    for name in ["own_source_growth_yoy_pct", "transfer_share_pct", "production_growth_yoy_pct"]:
        shown[name] = shown[name].map(lambda n: f"{n:+.1f}" if pd.notna(n) and name != "transfer_share_pct" else f"{n:.1f}" if pd.notna(n) else "Unavailable")
    shown = shown.rename(columns={"region_name_ru": "Region", "own_source_rub": "Tax + non-tax revenue (RUB bn)",
        "own_source_growth_yoy_pct": "Revenue YoY (%)", "transfer_share_pct": "Transfers / revenue (%)",
        "budget_balance_rub": "Budget balance (RUB bn)", "production_growth_yoy_pct": "Production YoY (%)"})
    st.dataframe(shown, hide_index=True, width="stretch", height=560)
    st.info("Absolute amounts reflect region size; this table does not judge unlike economies as directly comparable. Transfer receipts are from other budgets, not proven federal-only. Aggregate industry PIT growth is omitted because latest mining coverage changed.")
    with st.expander("Sources and coverage"):
        st.write("Treasury source files: " + ", ".join(sorted(data.balance_source_file.dropna().unique())))
        st.write("Production source files: " + ", ".join(sorted(data.production_source_file.dropna().unique())))
        st.write("Production vintages: " + ", ".join(sorted(data.production_source_vintage.dropna().unique())))
        st.caption("Missing values stay unavailable. Each row in the download retains source hashes, member/cell lineage, index basis and prior-year evidence. Latest available source dates vary outside this common window.")
    st.download_button("Download eligible-region overview evidence (CSV)", data.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"regional_overview_{pd.Timestamp(selected):%Y%m%d}.csv", mime="text/csv")
