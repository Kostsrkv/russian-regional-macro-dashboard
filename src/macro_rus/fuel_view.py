"""Region-first, source-linked fuel prices for explicit local research review."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .fuel_prices import CORE_GRADES, OPTIONAL_GRADES, fuel_history, latest_fuel_prices

GRADES = CORE_GRADES + OPTIONAL_GRADES
COLORS = {"AI-92": "#2455A4", "AI-95": "#C66A19", "Diesel": "#576475",
          "AI-98 and above": "#715494"}
DASHES = {"AI-92": [1, 0], "AI-95": [8, 3], "Diesel": [2, 3],
          "AI-98 and above": [8, 3, 2, 3]}
REVIEW_LABEL = "Large monthly change — explanation not verified"


def scoped_fuel_history(frame: pd.DataFrame, region_id: str, grades: list[str],
                        reporting_month: Any) -> pd.DataFrame:
    """Single scope used by the line chart, evidence tables and CSV export."""
    # The loader has validated the whole candidate. Restrict repeated display
    # calculations to this region, rather than revalidating every other region.
    history = fuel_history(frame.loc[frame.region_id.eq(region_id)], region_id, grades)
    if history.empty:
        return history
    return history.loc[history.period.le(pd.Timestamp(reporting_month))].copy()


def fuel_chart_rows(history: pd.DataFrame) -> pd.DataFrame:
    """Break lines at missing values or months rather than bridging a gap."""
    # Do not serialize unused comparison metadata into the chart. Full lineage
    # stays in the download; this also avoids all-NaT comparison-column coercion
    # in pandas' plotting-frame concatenation for the earliest reporting month.
    columns = ["region_id", "period", "period_end", "fuel_grade", "price_rub_per_litre",
               "mom_change_pct", "yoy_change_pct", "mom_review_flag", "units",
               "source_file", "source_sha256", "source_sheet", "source_cell"]
    plot = history[columns].sort_values(["fuel_grade", "period"]).copy()
    segments = []
    for _, group in plot.groupby("fuel_grade", sort=False):
        valid = (group.price_rub_per_litre.notna()
                 & np.isfinite(group.price_rub_per_litre)
                 & group.price_rub_per_litre.gt(0))
        months = group.period.dt.year * 12 + group.period.dt.month
        breaks = months.diff().ne(1) | ~valid.shift(1, fill_value=False) | ~valid
        group = group.assign(line_segment=breaks.cumsum(), review_message=np.where(
            group.mom_review_flag, REVIEW_LABEL, "No >10% monthly-change flag"))
        segments.append(group.loc[valid])
    return pd.concat(segments, ignore_index=True) if segments else plot.assign(line_segment=[])


def fuel_date_ticks(start: Any, end: Any, max_ticks: int) -> list[pd.Timestamp]:
    """Calendar-spaced labels with room for both exact reporting endpoints."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if pd.isna(start) or pd.isna(end) or end < start or max_ticks < 2:
        raise ValueError("Fuel date ticks require ordered dates and at least two labels")
    if start == end:
        return [start]
    # Endpoint labels face inward; allow a little less than one full slot while
    # still leaving enough room for horizontal 'Mon YYYY' text at 12px.
    minimum_gap = (end - start) / (max_ticks - 1) * 0.8
    months = pd.date_range(start.to_period("M").start_time,
                           end.to_period("M").start_time, freq="MS")
    for interval in (1, 2, 3, 6, 12, 24, 36, 48, 60, 120):
        anchors = [month + pd.offsets.MonthEnd(0) for month in months
                   if (month.year * 12 + month.month - 1) % interval == 0]
        ticks = [start, *[date for date in anchors
                         if start + minimum_gap <= date <= end - minimum_gap], end]
        if (len(ticks) <= max_ticks
                and all(right - left >= minimum_gap for left, right in zip(ticks, ticks[1:]))):
            return ticks
    return [start, end]


def _responsive_fuel_ticks(alt: Any, start: Any, end: Any) -> Any:
    """Vega reselects the label budget from the actual plot width on resize."""
    choices = []
    for budget in range(2, 13):
        # Vega's local datetime constructor matches the original temporal scale.
        # UTC epoch literals could put the latest tick just outside that domain
        # in a non-UTC browser and silently hide the endpoint label.
        values = [f"datetime({date.year}, {date.month - 1}, {date.day})"
                  for date in fuel_date_ticks(start, end, budget)]
        choices.append("[" + ", ".join(values) + "]")
    expression = choices[-1]
    for budget in reversed(range(2, 12)):
        expression = f"width < {budget * 100} ? {choices[budget - 2]} : ({expression})"
    return alt.ExprRef(expr=expression)


def fuel_chart(alt: Any, history: pd.DataFrame) -> Any:
    plot = fuel_chart_rows(history)
    grades = [grade for grade in GRADES if grade in set(plot.fuel_grade)]
    start, end = history.period_end.min(), history.period_end.max()
    ticks = _responsive_fuel_ticks(alt, start, end)
    encoding = dict(
        x=alt.X("period_end:T", title="End of reporting month",
                axis=alt.Axis(format="%b %Y", values=ticks, labelAngle=0,
                              labelOverlap=False, labelFlush=True, labelBound=False)),
        y=alt.Y("price_rub_per_litre:Q", title="Price (RUB/litre)",
                scale=alt.Scale(zero=False)),
        color=alt.Color("fuel_grade:N", title="Fuel grade",
                        scale=alt.Scale(domain=grades, range=[COLORS[g] for g in grades]),
                        legend=alt.Legend(orient="top")),
        tooltip=[alt.Tooltip("period_end:T", title="Observation date", format="%d %b %Y"),
                 alt.Tooltip("fuel_grade:N", title="Fuel grade"),
                 alt.Tooltip("price_rub_per_litre:Q", title="RUB/litre", format=".2f"),
                 alt.Tooltip("mom_change_pct:Q", title="MoM (%)", format="+.2f"),
                 alt.Tooltip("yoy_change_pct:Q", title="YoY (%)", format="+.2f"),
                 alt.Tooltip("review_message:N", title="Review status"),
                 alt.Tooltip("source_file:N", title="Original workbook"),
                 alt.Tooltip("source_sheet:N", title="Sheet"),
                 alt.Tooltip("source_cell:N", title="Cell")],
    )
    base = alt.Chart(plot).encode(**encoding)
    lines = base.mark_line(strokeWidth=2).encode(
        detail=alt.Detail("line_segment:N"),
        strokeDash=alt.StrokeDash("fuel_grade:N", title="Fuel grade",
                                 scale=alt.Scale(domain=grades, range=[DASHES[g] for g in grades]),
                                 legend=alt.Legend(orient="top")))
    points = base.mark_point(size=16, filled=True)
    flags = (alt.Chart(plot.loc[plot.mom_review_flag]).mark_point(
        shape="diamond", size=105, filled=False, color="#182230", strokeWidth=2)
        .encode(x=encoding["x"], y=encoding["y"], tooltip=encoding["tooltip"]))
    return (lines + points + flags).properties(
        height=360, description="Regional end-of-month prices by fuel grade. "
        "Line patterns distinguish grades; open diamonds indicate monthly changes above 10% in magnitude. "
        "Missing months are not connected.").configure_axis(
            labelFontSize=12, titleFontSize=13, gridColor="#D9DEE7").configure_legend(labelFontSize=12)


def _number(value: Any, *, signed: bool = False) -> str:
    if value is None or pd.isna(value) or not np.isfinite(value):
        return "Unavailable"
    return f"{value:+.2f}%" if signed else f"{value:.2f}"


def fuel_display_table(history: pd.DataFrame) -> pd.DataFrame:
    """Accessible table: explicit unavailable states, not literal NaN or zeros."""
    table = pd.DataFrame({
        "Observation date": history.period_end.dt.strftime("%d %b %Y"),
        "Fuel grade": history.fuel_grade,
        "Price (RUB/litre)": history.price_rub_per_litre.map(_number),
        "MoM (%)": history.mom_change_pct.map(lambda value: _number(value, signed=True)),
        "YoY (%)": history.yoy_change_pct.map(lambda value: _number(value, signed=True)),
        "Review": np.where(history.mom_review_flag, REVIEW_LABEL, "No >10% monthly-change flag"),
        "Availability": history.availability.str.replace("_", " "),
        "Source workbook": history.source_file,
        "Source sheet": history.source_sheet,
        "Source cell": history.source_cell,
    })
    table.loc[history.price_rub_per_litre.isna(), "Review"] = "Price unavailable"
    return table.reset_index(drop=True)


def fuel_csv(history: pd.DataFrame) -> bytes:
    """Numeric, UTF-8 CSV with empty missing fields and comparison lineage."""
    return history.to_csv(index=False, na_rep="", date_format="%Y-%m-%d").encode("utf-8-sig")


def render_fuel_sources(st: Any, history: pd.DataFrame, manifest: dict,
                        *, key_prefix: str = "fuel") -> None:
    st.subheader("Fuel source evidence")
    st.caption("Source: supplied Rosstat-labelled average consumer-price workbooks. "
               "Observation basis: end of reporting month · units: RUB/litre. "
               "Original publication and retrieval dates/URLs were not recorded; source authenticity "
               "has not been independently confirmed. Local audit date is not source freshness.")
    files = set(history.source_file)
    source_rows = [{"Workbook": item["filename"], "SHA-256": item["sha256"]}
                   for item in manifest["sources"] if item["filename"] in files]
    st.dataframe(pd.DataFrame(source_rows), hide_index=True, width="stretch")
    st.caption("Every exported observation includes the original workbook, SHA-256, sheet and cell. "
               "Change calculations also include the matched prior observation's date, price and source cell. "
               "Raw source files are not included in the dashboard.")
    with st.expander("Fuel metric definitions and audit scope"):
        st.write("MoM = 100 × (current price / exact preceding calendar-month price − 1). "
                 "YoY = 100 × (current price / same calendar-month price one year earlier − 1). "
                 "Changes are unavailable if either required observation is missing or invalid. "
                 "Absolute MoM above 10% is a transparent review screen, not a statistical anomaly or shortage alert.")
        st.write("These are reported spatial-average end-of-month prices, not daily pump quotes, "
                 "a time-average across the month, expenditure totals, or an official CPI series. "
                 "All-petrol aggregates are kept outside the grade view to avoid double counting. "
                 "Only the existing 78 eligible regional identifiers are included; no national mean is calculated.")
        st.code(f"Candidate file: {manifest['dataset']}\nComputed SHA-256: {manifest['dataset_sha256']}", language=None)
        if manifest.get("distribution_mode") == "cloud_processed_fuel_v1":
            st.caption("The published snapshot pins the processed dataset hash. This verifies snapshot "
                       "integrity, not independent source authenticity or the explanation of price movements.")
        else:
            st.caption("The computed candidate hash identifies this read. The earlier extraction audit "
                       "did not persist an immutable Parquet hash anchor. Checks compare source manifests, "
                       "schema, grain and audited coverage; raw-cell checks are separate evidence.")
    if not history.empty:
        region = str(history.region_id.iloc[0])
        cutoff = history.period.max().strftime("%Y-%m")
        st.download_button("Download selected fuel history and source evidence (CSV)", fuel_csv(history),
                           file_name=f"fuel_prices_{region}_through_{cutoff}.csv", mime="text/csv",
                           key=f"{key_prefix}_download_{region}")


def render_fuel_preview(st: Any, alt: Any, frame: pd.DataFrame, manifest: dict,
                        region_id: str) -> None:
    st.subheader("Fuel prices")
    context = ("Published research preview" if manifest.get("distribution_mode") == "cloud_processed_fuel_v1"
               else "Local research preview")
    st.caption(f"{context} · extraction checked; economic explanation and independent source authenticity not verified.")
    regional = frame.loc[frame.region_id.eq(region_id)]
    if regional.empty:
        st.info("No eligible regional fuel observations are available. No replacement geography is used.")
        return
    months = sorted(regional.period.unique(), reverse=True)
    cutoff = st.selectbox("Fuel reporting month", months,
                         format_func=lambda value: pd.Timestamp(value).strftime("%B %Y") + " · end of month",
                         key="fuel_reporting_month")
    grades = st.multiselect("Fuel grades", GRADES, default=list(CORE_GRADES), key="fuel_grades")
    if not grades:
        st.info("Select at least one fuel grade to see its prices and matching download.")
        return
    grades = [grade for grade in GRADES if grade in grades]
    history = scoped_fuel_history(frame, region_id, grades, cutoff)
    snapshot = latest_fuel_prices(regional, region_id, period=cutoff, grades=grades)
    end = pd.Timestamp(cutoff) + pd.offsets.MonthEnd(0)
    latest = pd.Timestamp(months[0]) + pd.offsets.MonthEnd(0)
    used = history.loc[history.period.eq(pd.Timestamp(cutoff)), "source_file"].unique()
    st.caption(f"Region: {regional.geography_name_ru.iloc[0]} · "
               f"Selected observation date: {end:%d %b %Y} · latest available: {latest:%d %b %Y} · "
               f"RUB/litre · source vintage: {', '.join(used)}")
    st.info("Fuel-price changes are not official inflation (CPI) and do not establish fuel shortages.")
    for column, row in zip(st.columns(len(grades)), snapshot.to_dict("records")):
        with column:
            yoy = row["yoy_change_pct"]
            st.metric(row["fuel_grade"], _number(row["price_rub_per_litre"]),
                      delta=None if pd.isna(yoy) else f"{yoy:+.2f}% YoY", delta_color="off")
            st.caption(f"RUB/litre · {end:%b %Y}\n\nMoM: {_number(row['mom_change_pct'], signed=True)}")
            st.caption(f"YoY vs {(end - pd.DateOffset(years=1)):%b %Y}; "
                       f"MoM vs {(end - pd.DateOffset(months=1)):%b %Y}.")
    flagged = history.loc[history.mom_review_flag]
    if not flagged.empty:
        st.warning(f"{len(flagged)} observations in the selected history have an absolute monthly change above 10%. "
                   "Explanation not verified. The flags remain visible even if the selected month itself is unflagged.")
    st.subheader("Price history")
    if fuel_chart_rows(history).empty:
        st.info("Selected prices are unavailable. No zero prices or older-month substitutes are plotted.")
    else:
        st.altair_chart(fuel_chart(alt, history), width="stretch")
        st.caption("Line patterns distinguish fuel grades; open diamonds mark large monthly changes. "
                   "Missing observations are not connected. The vertical axis follows the observed price range and does not start at zero.")
    with st.expander("Accessible history table", expanded=False):
        st.dataframe(fuel_display_table(history), hide_index=True, width="stretch")
    if not flagged.empty:
        with st.expander("Large-change review: current and preceding-month evidence"):
            evidence = fuel_display_table(flagged)
            evidence["Previous observation date"] = flagged.previous_month_period_end.dt.strftime("%d %b %Y").to_numpy()
            evidence["Previous price (RUB/litre)"] = flagged.previous_month_price_rub_per_litre.map(_number).to_numpy()
            evidence["Previous workbook"] = flagged.previous_month_source_file.to_numpy()
            evidence["Previous sheet"] = flagged.previous_month_source_sheet.to_numpy()
            evidence["Previous cell"] = flagged.previous_month_source_cell.to_numpy()
            st.dataframe(evidence, hide_index=True, width="stretch")
    render_fuel_sources(st, history, manifest)
