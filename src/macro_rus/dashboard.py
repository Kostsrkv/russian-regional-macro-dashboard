"""Region-first Streamlit interface for promoted canonical datasets.

The module deliberately keeps analytical transforms small and transparent. It
does not read raw source files: only a promoted vintage can reach the UI.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from macro_rus.contracts import TABLE_FILES


BLUE = "#2455A4"
ORANGE = "#C66A19"
INK = "#182230"
MUTED = "#5D6978"
GRID = "#D9DEE7"
LIGHT_BLUE = "#DCE7F7"

INDUSTRIAL_SECTION_NAMES = {
    "B": "Mining and quarrying",
    "C": "Manufacturing",
    "D": "Electricity, gas and steam",
    "E": "Water, sewerage and waste",
}

DATASET_LABELS = {
    "pit_receipts": "PIT",
    "industrial_production": "Industrial production",
    "budget_execution": "Fiscal execution",
}


@dataclass
class DataBundle:
    """Canonical promoted tables plus readable load failures."""

    root: Path
    tables: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def get(self, table_name: str) -> Any | None:
        return self.tables.get(table_name)

    @property
    def has_analytical_data(self) -> bool:
        return any(
            name in self.tables
            for name in ("pit_receipts", "industrial_production", "budget_execution")
        )


def promoted_signature(root: str | Path) -> tuple[tuple[str, int, int], ...]:
    """Return a cache key that changes whenever a promoted file changes."""

    root_path = Path(root)
    signature: list[tuple[str, int, int]] = []
    for filename in sorted(TABLE_FILES.values()):
        path = root_path / filename
        if path.exists():
            stat = path.stat()
            signature.append((filename, stat.st_mtime_ns, stat.st_size))
    manifest = root_path / "manifest.json"
    if manifest.exists():
        stat = manifest.stat()
        signature.append((manifest.name, stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def load_tables(data_dir: str | Path) -> DataBundle:
    """Read all available canonical Parquet tables without fabricating data."""

    import pandas as pd

    root_path = Path(data_dir)
    bundle = DataBundle(root=root_path)
    if not root_path.exists():
        bundle.errors["promoted_dataset"] = f"Promoted dataset directory not found: {root_path}"
        return bundle

    for table_name, filename in TABLE_FILES.items():
        path = root_path / filename
        if not path.exists():
            continue
        try:
            frame = pd.read_parquet(path)
            for date_column in ("period", "reporting_cutoff", "coverage_start", "coverage_end"):
                if date_column in frame.columns:
                    frame[date_column] = pd.to_datetime(frame[date_column], errors="coerce")
            bundle.tables[table_name] = frame
        except Exception as exc:  # visible in the UI, not silently swallowed
            bundle.errors[table_name] = f"{type(exc).__name__}: {exc}"
    return bundle


def load_promoted_data(root: str | Path) -> DataBundle:
    """Backward-compatible descriptive alias for :func:`load_tables`."""

    return load_tables(root)


def available_regions(bundle: DataBundle) -> list[dict[str, str]]:
    """Build a stable region list from the dimension or analytical tables."""

    candidates: dict[str, dict[str, str]] = {}
    table_order = (
        "regions",
        "pit_receipts",
        "industrial_production",
        "budget_execution",
    )
    for table_name in table_order:
        frame = bundle.get(table_name)
        if frame is None or frame.empty or "region_id" not in frame.columns:
            continue
        for row in frame.to_dict("records"):
            region_id = _clean_text(row.get("region_id"))
            if not region_id:
                continue
            current = candidates.setdefault(
                region_id,
                {
                    "region_id": region_id,
                    "region_name_en": "",
                    "region_name_ru": "",
                    "economic_profile": "",
                },
            )
            for key in ("region_name_en", "region_name_ru", "economic_profile"):
                value = _clean_text(row.get(key))
                if value and not current[key]:
                    current[key] = value
    return sorted(
        candidates.values(),
        key=lambda item: (item["region_name_en"] or item["region_name_ru"] or item["region_id"]).casefold(),
    )


def filter_region(frame: Any | None, region_id: str) -> Any | None:
    if frame is None or "region_id" not in frame.columns:
        return None
    return frame.loc[frame["region_id"].astype(str) == str(region_id)].copy()


def latest_period(frame: Any | None) -> Any | None:
    if frame is None or frame.empty or "period" not in frame.columns:
        return None
    values = frame["period"].dropna()
    return None if values.empty else values.max()


def pit_summary(frame: Any | None) -> dict[str, Any]:
    """Calculate a latest PIT flow and an honest same-period YoY change."""

    import pandas as pd

    empty = {"period": None, "value": None, "yoy_pct": None, "measure": "pit_flow_rub"}
    if frame is None or frame.empty or "period" not in frame.columns:
        return empty
    measure = "pit_flow_rub" if "pit_flow_rub" in frame.columns else "pit_ytd_rub"
    if measure not in frame.columns:
        return empty
    work = _exclude_pit_total_rows(frame.copy())
    work[measure] = pd.to_numeric(work[measure], errors="coerce")
    work = work.dropna(subset=["period", measure])
    if work.empty:
        return empty
    period_totals = work.groupby("period", dropna=False)[measure].sum(min_count=1).sort_index()
    period = period_totals.index[-1]
    value = period_totals.iloc[-1]
    prior_period = period - pd.DateOffset(years=1)
    prior = period_totals.get(prior_period)
    yoy = None
    if prior is not None and pd.notna(prior) and prior != 0:
        yoy = (value / prior - 1) * 100
    return {"period": period, "value": float(value), "yoy_pct": yoy, "measure": measure}


def industry_snapshot(frame: Any | None) -> Any:
    """Return latest industry PIT levels, shares, and YoY contributions."""

    import pandas as pd

    columns = [
        "industry",
        "industry_ru",
        "okved_section",
        "pit_rub",
        "share_pct",
        "change_rub",
        "yoy_pct",
    ]
    if frame is None or frame.empty or "period" not in frame.columns:
        return pd.DataFrame(columns=columns)
    measure = "pit_flow_rub" if "pit_flow_rub" in frame.columns else "pit_ytd_rub"
    if measure not in frame.columns:
        return pd.DataFrame(columns=columns)
    work = _exclude_pit_total_rows(frame.copy())
    work[measure] = pd.to_numeric(work[measure], errors="coerce")
    work = work.dropna(subset=["period", measure])
    if work.empty:
        return pd.DataFrame(columns=columns)
    work["industry"] = _series_first_nonempty(
        work,
        ("industry_name_en", "industry_name_ru", "okved_raw", "okved_section"),
        "Unmapped",
    )
    work["industry_ru"] = _series_first_nonempty(work, ("industry_name_ru",), "")
    work["okved_section"] = _series_first_nonempty(work, ("okved_section",), "Unmapped")
    grouping = ["period", "industry", "industry_ru", "okved_section"]
    totals = work.groupby(grouping, dropna=False)[measure].sum(min_count=1).reset_index()
    current_period = totals["period"].max()
    current = totals.loc[totals["period"] == current_period].copy()
    current = current.rename(columns={measure: "pit_rub"})
    denominator = current["pit_rub"].sum(min_count=1)
    current["share_pct"] = current["pit_rub"] / denominator * 100 if denominator else pd.NA
    prior_period = current_period - pd.DateOffset(years=1)
    prior = totals.loc[totals["period"] == prior_period, ["okved_section", measure]].rename(
        columns={measure: "prior_rub"}
    )
    current = current.merge(prior, on="okved_section", how="left")
    current["change_rub"] = current["pit_rub"] - current["prior_rub"]
    current["yoy_pct"] = (
        (current["pit_rub"] / current["prior_rub"] - 1) * 100
    ).where(current["prior_rub"] > 0)
    return current[columns].sort_values("pit_rub", ascending=False, na_position="last").reset_index(drop=True)


def industrial_snapshot(frame: Any | None) -> dict[str, Any]:
    """Select an explicitly published headline and preserve sector detail."""

    import pandas as pd

    result: dict[str, Any] = {"period": None, "headline": None, "measure": "", "sectors": pd.DataFrame()}
    if frame is None or frame.empty or "period" not in frame.columns or "index_value" not in frame.columns:
        return result
    work = frame.copy()
    work["index_value"] = pd.to_numeric(work["index_value"], errors="coerce")
    work = work.dropna(subset=["period", "index_value"])
    if work.empty:
        return result
    period = work["period"].max()
    latest = work.loc[work["period"] == period].copy()
    sections = latest.get("okved_section")
    if sections is None:
        section_text = pd.Series("", index=latest.index)
    else:
        section_text = sections.fillna("").astype(str).str.strip().str.upper()
    aggregate_tokens = {"", "TOTAL", "ALL", "B-E", "B–E", "B—E"}
    headline_rows = latest.loc[section_text.isin(aggregate_tokens)]
    headline = None if headline_rows.empty else float(headline_rows.iloc[0]["index_value"])
    sectors = latest.loc[section_text.isin({"B", "C", "D", "E"})].copy()
    sectors["industry"] = _series_first_nonempty(
        sectors,
        ("industry_name_en", "industry_name_ru", "okved_section"),
        "Unmapped",
    )
    result.update(
        {
            "period": period,
            "headline": headline,
            "measure": _first_text(latest, "index_measure") or "published index",
            "sectors": sectors[["industry", "okved_section", "index_value"]]
            .sort_values("okved_section")
            .reset_index(drop=True),
        }
    )
    return result


def cross_check_snapshot(pit_frame: Any | None, industrial_frame: Any | None) -> dict[str, Any]:
    """Align industry PIT with the production observation at the PIT cutoff.

    PIT is a quarterly flow while the available Rosstat series is a monthly
    same-month-prior-year index. The function aligns their cutoff date but
    preserves those different comparison windows for explicit UI labelling.
    """

    import pandas as pd

    empty = {
        "pit_period": None,
        "pit_prior_period": None,
        "production_period": None,
        "production_measure": "",
        "data": pd.DataFrame(),
    }
    if pit_frame is None or industrial_frame is None or pit_frame.empty or industrial_frame.empty:
        return empty

    measure = "pit_flow_rub" if "pit_flow_rub" in pit_frame.columns else "pit_ytd_rub"
    if measure not in pit_frame.columns:
        return empty
    pit_work = _exclude_pit_total_rows(pit_frame.copy())
    pit_work[measure] = pd.to_numeric(pit_work[measure], errors="coerce")
    pit_work["period"] = pd.to_datetime(pit_work["period"], errors="coerce")
    pit_work = pit_work.dropna(subset=["period", measure])
    if pit_work.empty:
        return empty
    pit_period = pit_work["period"].max()
    pit_snapshot = industry_snapshot(pit_work)

    production = industrial_frame.copy()
    production["period"] = pd.to_datetime(production["period"], errors="coerce")
    production["index_value"] = pd.to_numeric(production["index_value"], errors="coerce")
    production = production.loc[
        production["period"].eq(pit_period)
        & production["okved_section"].isin(["B", "C", "D", "E"])
    ].dropna(subset=["index_value"])
    if production.empty:
        return {
            **empty,
            "pit_period": pit_period,
            "pit_prior_period": pit_period - pd.DateOffset(years=1),
        }

    production = production.sort_values(["okved_section", "source_vintage"], kind="stable")
    production = production.drop_duplicates("okved_section", keep="last")
    production["industry"] = _series_first_nonempty(
        production,
        ("industry_name_en", "industry_name_ru", "okved_section"),
        "Unmapped",
    )
    pit_sector = (
        pit_snapshot.loc[pit_snapshot["okved_section"].isin(["B", "C", "D", "E"])]
        .groupby("okved_section", as_index=False)[["pit_rub", "change_rub", "yoy_pct"]]
        .sum(min_count=1)
        .rename(
            columns={
                "pit_rub": "pit_current_rub",
                "change_rub": "pit_change_rub",
                "yoy_pct": "pit_yoy_pct",
            }
        )
    )
    joined = production[
        ["industry", "okved_section", "index_measure", "index_value"]
    ].merge(pit_sector, on="okved_section", how="outer")
    joined["industry"] = joined["industry"].fillna(
        joined["okved_section"].map(INDUSTRIAL_SECTION_NAMES)
    )
    joined = joined.sort_values("okved_section", kind="stable").reset_index(drop=True)
    return {
        "pit_period": pit_period,
        "pit_prior_period": pit_period - pd.DateOffset(years=1),
        "production_period": pit_period,
        "production_measure": _first_text(production, "index_measure"),
        "data": joined,
    }


def classify_revenue(row: Mapping[str, Any]) -> str | None:
    """Map only the small set of fiscal rows used by the dashboard."""

    code = "".join(character for character in _clean_text(row.get("revenue_code")) if character.isdigit())
    names = " ".join(
        _clean_text(row.get(column)).casefold()
        for column in ("revenue_name_en", "revenue_name_ru")
    )
    if code.endswith("10102000010000110") or "personal income" in names or "налог на доходы физических лиц" in names:
        return "PIT"
    if code.endswith("10101000000000110") or "corporate income" in names or "налог на прибыль" in names:
        return "CIT"
    if code.endswith("10000000000000000") or "tax and non-tax" in names or "налоговые и неналоговые" in names:
        return "Own-source revenue"
    if code.endswith("20000000000000000") or "gratuitous" in names or "transfers" in names or "безвозмездные поступления" in names:
        return "Transfers"
    if (
        code.endswith("00000000000000000")
        or "total revenue" in names
        or "доходы бюджета - всего" in names
        or names.strip() in {"доходы", "revenue"}
    ):
        return "Total revenue"
    return None


def fiscal_snapshot(frame: Any | None) -> Any:
    """Return the latest cumulative actual-versus-plan values by fiscal concept."""

    import pandas as pd

    columns = ["metric", "actual_ytd_rub", "plan_rub", "execution_pct", "plan_basis"]
    required = {"period", "actual_ytd_rub"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame(columns=columns)
    work = frame.copy()
    if "budget_level" in work.columns:
        scope = work["budget_level"].fillna("").astype(str).str.casefold()
        consolidated = scope.str.contains("consolidat|консолид", regex=True)
        if consolidated.any():
            work = work.loc[consolidated].copy()
    work = work.loc[work["period"] == work["period"].max()].copy()
    work["metric"] = [classify_revenue(row) for row in work.to_dict("records")]
    work = work.dropna(subset=["metric"])
    if work.empty:
        return pd.DataFrame(columns=columns)
    for column in ("actual_ytd_rub", "approved_plan_rub", "revised_plan_rub"):
        if column in work.columns:
            work[column] = pd.to_numeric(work[column], errors="coerce")
    revised = work.get("revised_plan_rub", pd.Series(index=work.index, dtype="float64"))
    approved = work.get("approved_plan_rub", pd.Series(index=work.index, dtype="float64"))
    work["plan_rub"] = revised.where(revised.notna(), approved)
    work["plan_basis"] = revised.notna().map({True: "Revised plan", False: "Reported approved plan"})
    result = (
        work.groupby("metric", as_index=False)
        .agg(
            actual_ytd_rub=("actual_ytd_rub", "sum"),
            plan_rub=("plan_rub", "sum"),
            plan_basis=("plan_basis", "first"),
        )
    )
    result["execution_pct"] = result["actual_ytd_rub"] / result["plan_rub"] * 100
    order = {name: index for index, name in enumerate(("Total revenue", "Own-source revenue", "PIT", "CIT", "Transfers"))}
    result["_order"] = result["metric"].map(order).fillna(99)
    return result.sort_values("_order").drop(columns="_order").reset_index(drop=True)[columns]


def source_summary(bundle: DataBundle, region_id: str, table_names: Iterable[str]) -> dict[str, str]:
    """Build compact metadata that is visible on every analytical page."""

    period_labels: list[str] = []
    vintages: set[str] = set()
    source_ids: set[str] = set()
    for table_name in table_names:
        frame = filter_region(bundle.get(table_name), region_id)
        if frame is None or frame.empty:
            continue
        period = latest_period(frame)
        if period is not None:
            period_labels.append(
                f"{DATASET_LABELS.get(table_name, table_name)}: {period.strftime('%d %b %Y')}"
            )
            latest = frame.loc[frame["period"].eq(period)] if "period" in frame.columns else frame
        else:
            latest = frame
        if "source_vintage" in latest.columns:
            vintages.update(_nonempty_strings(latest["source_vintage"].tolist()))
        if "source_id" in latest.columns:
            source_ids.update(_nonempty_strings(latest["source_id"].tolist()))
    publishers: set[str] = set()
    sources = bundle.get("sources")
    units: set[str] = set()
    if sources is not None and not sources.empty and source_ids and "source_id" in sources.columns:
        selected = sources.loc[sources["source_id"].astype(str).isin(source_ids)]
        if "publisher" in selected.columns:
            publishers.update(_nonempty_strings(selected["publisher"].tolist()))
        if "units" in selected.columns:
            units.update(_nonempty_strings(selected["units"].tolist()))
    return {
        "periods": " · ".join(period_labels) or "Not available",
        "sources": ", ".join(sorted(publishers or source_ids)) or "Not available",
        "vintages": ", ".join(sorted(vintages)) or "Not available",
        "units": ", ".join(sorted(units)) or _default_units(table_names),
    }


def render_dashboard(data_root: str | Path) -> None:
    """Render the full local Streamlit dashboard."""

    import altair as alt
    import pandas as pd
    import streamlit as st

    _apply_accessible_styles(st)

    @st.cache_data(show_spinner=False)
    def _cached_load(root_string: str, signature: tuple[tuple[str, int, int], ...]) -> DataBundle:
        del signature
        return load_tables(root_string)

    root_path = Path(data_root)
    bundle = _cached_load(str(root_path), promoted_signature(root_path))

    st.title("Russian Regional Macro")
    st.caption("A region-first view of PIT receipts, industrial production, and fiscal execution")

    if not bundle.has_analytical_data:
        _render_empty_promoted_state(st, bundle)
        return

    regions = available_regions(bundle)
    if not regions:
        st.error("The promoted files contain no usable region identifier. No values are shown.")
        return

    labels = {
        item["region_id"]: item["region_name_en"] or item["region_name_ru"] or item["region_id"]
        for item in regions
    }
    with st.sidebar:
        st.header("Region")
        selected_region = st.selectbox(
            "Select a region",
            options=[item["region_id"] for item in regions],
            format_func=lambda value: labels[value],
        )
        selected = next(item for item in regions if item["region_id"] == selected_region)
        if selected.get("region_name_ru"):
            st.caption(selected["region_name_ru"])
        page = st.radio(
            "View",
            (
                "Macro overview",
                "Industry PIT",
                "Fiscal execution",
                "PIT–production check",
                "Optional comparison",
                "Data & methodology",
            ),
        )
        st.divider()
        st.caption("Comparison is optional; the selected region remains the analytical unit.")

    if bundle.errors:
        with st.expander("Data loading warnings", expanded=False):
            for table_name, message in bundle.errors.items():
                st.warning(f"{table_name}: {message}")

    st.header(labels[selected_region])
    if selected.get("economic_profile"):
        st.write(selected["economic_profile"])

    if page == "Macro overview":
        _render_overview(st, alt, bundle, selected_region)
    elif page == "Industry PIT":
        _render_industry(st, alt, bundle, selected_region)
    elif page == "Fiscal execution":
        _render_fiscal(st, alt, bundle, selected_region)
    elif page == "PIT–production check":
        _render_cross_check(st, alt, bundle, selected_region)
    elif page == "Optional comparison":
        _render_comparison(st, alt, bundle, selected_region, regions, labels)
    else:
        _render_methodology(st, bundle, selected_region)


def _render_overview(st: Any, alt: Any, bundle: DataBundle, region_id: str) -> None:
    metadata = source_summary(
        bundle,
        region_id,
        ("pit_receipts", "industrial_production", "budget_execution"),
    )
    _render_metadata(st, metadata)

    pit = pit_summary(filter_region(bundle.get("pit_receipts"), region_id))
    industrial = industrial_snapshot(filter_region(bundle.get("industrial_production"), region_id))
    regional_budget = filter_region(bundle.get("budget_execution"), region_id)
    fiscal = fiscal_snapshot(regional_budget)
    pit_fiscal = _metric_row(fiscal, "PIT")
    budget_period = latest_period(regional_budget)

    columns = st.columns(3)
    columns[0].metric(
        f"PIT receipts · {_format_period(pit['period'])}",
        _format_rub(pit["value"]),
        _format_delta(pit["yoy_pct"], "YoY") if pit["yoy_pct"] is not None else None,
        help="Formal taxable-income receipts. This is not a direct output or welfare measure.",
    )
    columns[1].metric(
        f"Industrial output index · {_format_period(industrial['period'])}",
        _format_index(industrial["headline"]),
        help="Published headline only; sector indices are not averaged to create a substitute.",
    )
    columns[2].metric(
        f"PIT plan execution · {_format_period(budget_period)}",
        _format_pct(pit_fiscal.get("execution_pct") if pit_fiscal else None),
        help="Cumulative receipts as a share of the reported annual plan. Not a shortfall verdict.",
    )

    st.subheader("What changed? Industries driving PIT movement")
    industry = industry_snapshot(filter_region(bundle.get("pit_receipts"), region_id))
    drivers = industry.dropna(subset=["change_rub"]).copy() if not industry.empty else industry
    if drivers.empty:
        st.info("A same-period prior-year PIT observation is not available, so contributions cannot yet be calculated.")
    else:
        drivers = drivers.reindex(drivers["change_rub"].abs().sort_values(ascending=False).index).head(10)
        chart = _signed_bar_chart(
            alt,
            drivers,
            category="industry",
            value="change_rub",
            title="Industry contribution to PIT change",
            subtitle="RUB; latest period versus the same period one year earlier",
        )
        st.altair_chart(chart, width="stretch")
        with st.expander("Accessible data table"):
            table = drivers[["industry", "industry_ru", "okved_section", "change_rub"]].copy()
            st.dataframe(table, hide_index=True, width="stretch")

    st.info(
        "These indicators describe momentum and fiscal execution. They do not, by themselves, identify sanctions or any other cause."
    )


def _render_industry(st: Any, alt: Any, bundle: DataBundle, region_id: str) -> None:
    metadata = source_summary(bundle, region_id, ("pit_receipts",))
    _render_metadata(st, metadata)
    frame = filter_region(bundle.get("pit_receipts"), region_id)
    snapshot = industry_snapshot(frame)
    if snapshot.empty:
        _render_missing_table(st, "PIT by industry", "pit_receipts.parquet")
        return

    st.subheader("Industry structure")
    top = snapshot.head(12).copy()
    chart = (
        alt.Chart(top, title=alt.Title("PIT by industry", subtitle="Latest complete period; RUB"))
        .mark_bar(color=BLUE, stroke="#163B75")
        .encode(
            y=alt.Y("industry:N", sort="-x", title=None),
            x=alt.X("pit_rub:Q", title="PIT receipts (RUB)", scale=alt.Scale(zero=True)),
            tooltip=[
                alt.Tooltip("industry:N", title="Industry"),
                alt.Tooltip("industry_ru:N", title="Russian label"),
                alt.Tooltip("okved_section:N", title="OKVED2"),
                alt.Tooltip("pit_rub:Q", title="PIT (RUB)", format=",.0f"),
                alt.Tooltip("share_pct:Q", title="Share", format=".1f"),
            ],
        )
        .properties(height=max(260, 28 * len(top)), description="Horizontal bars show PIT receipts by industry.")
    )
    st.altair_chart(chart, width="stretch")
    table = snapshot.copy()
    table = table.rename(
        columns={
            "industry": "Industry (English)",
            "industry_ru": "Industry (Russian)",
            "okved_section": "OKVED2 section",
            "pit_rub": "PIT (RUB)",
            "share_pct": "Share (%)",
            "change_rub": "Same-period YoY change (RUB)",
        }
    )
    st.dataframe(table, hide_index=True, width="stretch")
    st.caption("Unmapped industries remain visible. Missing and suppressed observations are never rendered as zero.")


def _render_fiscal(st: Any, alt: Any, bundle: DataBundle, region_id: str) -> None:
    metadata = source_summary(bundle, region_id, ("budget_execution",))
    _render_metadata(st, metadata)
    frame = filter_region(bundle.get("budget_execution"), region_id)
    snapshot = fiscal_snapshot(frame)
    if snapshot.empty:
        _render_missing_table(st, "Fiscal execution", "budget_execution.parquet")
        return

    st.subheader("Actual revenue versus annual plan")
    plot = snapshot.dropna(subset=["execution_pct"]).copy()
    chart = (
        alt.Chart(plot, title=alt.Title("Revenue execution", subtitle="Cumulative actual as % of reported annual plan"))
        .mark_bar(color=BLUE, stroke="#163B75")
        .encode(
            y=alt.Y("metric:N", sort=None, title=None),
            x=alt.X("execution_pct:Q", title="Execution (%)", scale=alt.Scale(zero=True)),
            tooltip=[
                alt.Tooltip("metric:N", title="Revenue"),
                alt.Tooltip("actual_ytd_rub:Q", title="Actual YTD (RUB)", format=",.0f"),
                alt.Tooltip("plan_rub:Q", title="Annual plan (RUB)", format=",.0f"),
                alt.Tooltip("execution_pct:Q", title="Execution", format=".1f"),
                alt.Tooltip("plan_basis:N", title="Plan basis"),
            ],
        )
        .properties(height=max(220, 44 * len(plot)), description="Bars show cumulative execution of annual revenue plans.")
    )
    st.altair_chart(chart, width="stretch")
    display = snapshot.rename(
        columns={
            "metric": "Revenue",
            "actual_ytd_rub": "Actual YTD (RUB)",
            "plan_rub": "Annual plan (RUB)",
            "execution_pct": "Execution (%)",
            "plan_basis": "Plan basis",
        }
    )
    st.dataframe(display, hide_index=True, width="stretch")
    st.warning(
        "Execution below a simple calendar fraction is not automatically a shortfall. Revenue seasonality and original-versus-revised plan history are required for that judgment."
    )


def _render_cross_check(st: Any, alt: Any, bundle: DataBundle, region_id: str) -> None:
    metadata = source_summary(bundle, region_id, ("pit_receipts", "industrial_production"))
    _render_metadata(st, metadata)
    snapshot = cross_check_snapshot(
        filter_region(bundle.get("pit_receipts"), region_id),
        filter_region(bundle.get("industrial_production"), region_id),
    )
    joined = snapshot["data"]
    if joined.empty:
        if snapshot["pit_period"] is not None:
            st.info(
                "No Rosstat monthly production observation matches the latest PIT cutoff "
                f"({_format_period(snapshot['pit_period'])}). The cross-check is withheld rather than "
                "combining unmatched periods."
            )
        else:
            st.info("Both mapped PIT and industrial-production sections B–E are required for this cross-check.")
        return

    st.subheader("PIT and production signals by matching industrial section")
    st.caption(
        f"PIT: quarter ending {_format_period(snapshot['pit_period'])} versus the same quarter one year earlier. "
        f"Production: {_format_period(snapshot['production_period'])} versus the same month one year earlier. "
        "The measures are kept separate and are not combined into a score."
    )

    production_plot = joined.dropna(subset=["index_value"]).copy()
    if not production_plot.empty:
        production_plot["value_label"] = production_plot["index_value"].map(lambda value: f"{value:.1f}")
        lower = min(85.0, float(production_plot["index_value"].min()) - 4)
        upper = max(105.0, float(production_plot["index_value"].max()) + 4)
        base = alt.Chart(production_plot).encode(
            y=alt.Y(
                "industry:N",
                sort=list(INDUSTRIAL_SECTION_NAMES.values()),
                title=None,
                axis=alt.Axis(labelLimit=220, labelOverlap=False),
            ),
            x=alt.X(
                "index_value:Q",
                title="Production index (same month previous year = 100)",
                scale=alt.Scale(domain=[lower, upper], zero=False),
                axis=alt.Axis(tickCount=7),
            ),
            tooltip=[
                alt.Tooltip("industry:N", title="Industry"),
                alt.Tooltip("okved_section:N", title="OKVED2"),
                alt.Tooltip("index_value:Q", title="Production index", format=".1f"),
            ],
        )
        reference = (
            alt.Chart({"values": [{"benchmark": 100}]})
            .mark_rule(color=INK, strokeDash=[5, 4], strokeWidth=1.5)
            .encode(x="benchmark:Q")
        )
        points = base.mark_point(filled=True, color=BLUE, size=110, stroke="#163B75")
        labels = base.mark_text(align="left", dx=8, color=INK).encode(text="value_label:N")
        chart = (reference + points + labels).properties(
            height=220,
            title=alt.Title(
                "Industrial production",
                subtitle=f"{_format_period(snapshot['production_period'])}; 100 = unchanged from the same month one year earlier",
            ),
            description="A dot plot compares production indices with the no-change benchmark of 100.",
        )
        st.altair_chart(chart, width="stretch")

    pit_plot = joined.dropna(subset=["pit_yoy_pct"]).copy()
    missing_pit_sections = joined.loc[joined["pit_yoy_pct"].isna(), "okved_section"].dropna().tolist()
    if missing_pit_sections and not pit_plot.empty:
        st.warning(
            "PIT comparison is unavailable for OKVED2 "
            f"{', '.join(missing_pit_sections)} in the current FNS release. "
            "Those observations remain explicitly missing and are omitted from the PIT chart."
        )
    if pit_plot.empty:
        st.info(
            "PIT year-on-year comparison is unavailable because a matching prior-year FNS observation "
            "has not been ingested. Missing values are not displayed as zero."
        )
    else:
        pit_plot["direction"] = pit_plot["pit_yoy_pct"].map(
            lambda value: "Increase" if value >= 0 else "Decrease"
        )
        pit_plot["value_label"] = pit_plot["pit_yoy_pct"].map(lambda value: f"{value:+.1f}%")
        base = alt.Chart(pit_plot).encode(
            y=alt.Y(
                "industry:N",
                sort=list(INDUSTRIAL_SECTION_NAMES.values()),
                title=None,
                axis=alt.Axis(labelLimit=220, labelOverlap=False),
            ),
            x=alt.X("pit_yoy_pct:Q", title="Nominal PIT growth (%)", axis=alt.Axis(tickCount=7)),
            tooltip=[
                alt.Tooltip("industry:N", title="Industry"),
                alt.Tooltip("okved_section:N", title="OKVED2"),
                alt.Tooltip("pit_yoy_pct:Q", title="Nominal PIT growth", format="+.1f"),
                alt.Tooltip("pit_change_rub:Q", title="PIT change (RUB)", format="+,.0f"),
            ],
        )
        bars = base.mark_bar(stroke=INK).encode(
            color=alt.Color(
                "direction:N",
                scale=alt.Scale(domain=["Increase", "Decrease"], range=[BLUE, ORANGE]),
                legend=None,
            )
        )
        zero = alt.Chart({"values": [{"zero": 0}]}).mark_rule(color=INK).encode(x="zero:Q")
        labels = base.mark_text(align="left", dx=5, color=INK).encode(text="value_label:N")
        chart = (bars + zero + labels).properties(
            height=220,
            title=alt.Title(
                "Industry-attributed PIT receipts",
                subtitle=f"Nominal quarterly flow; {_format_period(snapshot['pit_period'])} versus {_format_period(snapshot['pit_prior_period'])}",
            ),
            description="Signed horizontal bars show nominal PIT growth by industrial section.",
        )
        st.altair_chart(chart, width="stretch")

    display = joined[["industry", "okved_section", "index_value", "pit_yoy_pct", "pit_change_rub"]].copy()
    display["index_value"] = display["index_value"].map(_format_index)
    display["pit_yoy_pct"] = display["pit_yoy_pct"].map(_format_pct)
    display["pit_change_rub"] = display["pit_change_rub"].map(_format_rub)
    display = display.rename(
        columns={
            "industry": "Industry",
            "okved_section": "OKVED2 section",
            "index_value": "Production index",
            "pit_yoy_pct": "Nominal PIT YoY",
            "pit_change_rub": "PIT change",
        }
    )
    with st.expander("Accessible data table"):
        st.dataframe(display, hide_index=True, width="stretch")
    st.info(
        "Divergence is not necessarily contradictory: wage inflation, bonuses, labour scarcity, tax timing, and classification changes can move PIT independently of physical output."
    )


def _render_comparison(
    st: Any,
    alt: Any,
    bundle: DataBundle,
    region_id: str,
    regions: list[dict[str, str]],
    labels: Mapping[str, str],
) -> None:
    import pandas as pd

    st.subheader("Optional comparison")
    st.caption("This view is secondary to each region's own history and does not rank regions.")
    other_ids = [item["region_id"] for item in regions if item["region_id"] != region_id]
    if not other_ids:
        st.info("Add another promoted region to enable comparison.")
        return
    comparator = st.selectbox(
        "Compare with one other region",
        options=other_ids,
        format_func=lambda value: labels[value],
    )
    rows = []
    for candidate in (region_id, comparator):
        pit = pit_summary(filter_region(bundle.get("pit_receipts"), candidate))
        industrial = industrial_snapshot(filter_region(bundle.get("industrial_production"), candidate))
        fiscal = fiscal_snapshot(filter_region(bundle.get("budget_execution"), candidate))
        fiscal_pit = _metric_row(fiscal, "PIT")
        rows.append(
            {
                "Region": labels[candidate],
                "PIT YoY (%)": pit["yoy_pct"],
                "Industrial production index": industrial["headline"],
                "PIT plan execution (%)": fiscal_pit.get("execution_pct") if fiscal_pit else None,
            }
        )
    comparison = pd.DataFrame(rows)
    st.dataframe(comparison, hide_index=True, width="stretch")
    st.caption("Different industry structures and tax bases limit direct cross-region interpretation.")


def _render_methodology(st: Any, bundle: DataBundle, region_id: str) -> None:
    st.subheader("Data quality and methodology")
    st.markdown(
        """
**Interpretation boundary.** PIT is formal taxable-income revenue associated with employers reported in a region and industry. It is not a direct measure of output, employment, productivity, profitability, or household welfare.

**Three separate questions.** The dashboard distinguishes economic momentum, statistical anomalies, and fiscal shortfalls. A fiscal shortfall requires actual retained revenue to be assessed against an appropriate approved or revised budget path.

**Policy and statistical breaks.** Interpretation should account for the 2021 PIT change, the 2023 Unified Tax Account, the 2024 payment-schedule change, the 2025 five-bracket reform, revisions, OKVED changes, and major employer reclassification.

**Causal restraint.** Sanctions may be included only as separately cited context. The dashboard never attributes an observed movement to sanctions automatically.
"""
    )
    quality = filter_region(bundle.get("quality_events"), region_id)
    if quality is None or quality.empty:
        st.info("No region-specific quality events are recorded in the promoted vintage.")
    else:
        preferred = [
            column
            for column in ("severity", "status", "table_name", "period", "message", "source_vintage")
            if column in quality.columns
        ]
        st.dataframe(quality[preferred], hide_index=True, width="stretch")

    st.subheader("Source register")
    used_ids: set[str] = set()
    for table_name in ("pit_receipts", "industrial_production", "budget_execution"):
        frame = filter_region(bundle.get(table_name), region_id)
        if frame is not None and "source_id" in frame.columns:
            used_ids.update(_nonempty_strings(frame["source_id"].tolist()))
    sources = bundle.get("sources")
    if sources is None or sources.empty:
        st.info("The promoted vintage does not yet include a source register.")
    else:
        selected = sources
        if used_ids and "source_id" in sources.columns:
            selected = sources.loc[sources["source_id"].astype(str).isin(used_ids)]
        columns = [
            column
            for column in (
                "publisher",
                "dataset_name",
                "reporting_cutoff",
                "publication_date",
                "units",
                "sha256",
                "notes",
            )
            if column in selected.columns
        ]
        st.dataframe(selected[columns], hide_index=True, width="stretch")


def _signed_bar_chart(alt: Any, frame: Any, category: str, value: str, title: str, subtitle: str) -> Any:
    plot = frame.copy()
    plot["direction"] = plot[value].apply(lambda item: "Increase" if item >= 0 else "Decrease")
    base = alt.Chart(plot, title=alt.Title(title, subtitle=subtitle)).encode(
        y=alt.Y(f"{category}:N", sort="-x", title=None),
        x=alt.X(f"{value}:Q", title="Change (RUB)"),
        tooltip=[
            alt.Tooltip(f"{category}:N", title="Industry"),
            alt.Tooltip(f"{value}:Q", title="Change (RUB)", format="+,.0f"),
            alt.Tooltip("direction:N", title="Direction"),
        ],
    )
    bars = base.mark_bar(stroke=INK).encode(
        color=alt.Color(
            "direction:N",
            scale=alt.Scale(domain=["Increase", "Decrease"], range=[BLUE, ORANGE]),
            legend=alt.Legend(orient="top", title=None),
        )
    )
    zero = alt.Chart({"values": [{}]}).mark_rule(color=INK, strokeWidth=1).encode(x=alt.datum(0))
    return (bars + zero).properties(
        height=max(260, 30 * len(plot)),
        description="Signed bars show industry contributions; labels and tooltips also state direction.",
    )


def _render_metadata(st: Any, metadata: Mapping[str, str]) -> None:
    st.caption(
        f"Latest source observations: **{metadata['periods']}** · Units: **{metadata['units']}**  \n"
        f"Source: **{metadata['sources']}** · Vintage: **{metadata['vintages']}**"
    )


def _render_empty_promoted_state(st: Any, bundle: DataBundle) -> None:
    st.info(
        "The dashboard shell is ready, but no promoted analytical dataset is available. "
        "Run the ingestion and promotion workflow; the app will then read the canonical Parquet files automatically."
    )
    st.code(str(bundle.root), language=None)
    if bundle.errors:
        for table_name, message in bundle.errors.items():
            st.warning(f"{table_name}: {message}")
    st.subheader("Expected promoted files")
    st.table(
        [
            {"Dataset": "PIT by industry", "File": TABLE_FILES["pit_receipts"]},
            {"Dataset": "Industrial production", "File": TABLE_FILES["industrial_production"]},
            {"Dataset": "Fiscal execution", "File": TABLE_FILES["budget_execution"]},
        ]
    )


def _render_missing_table(st: Any, label: str, filename: str) -> None:
    st.info(f"{label} is not available for this region in the promoted vintage. Expected source: {filename}.")


def _apply_accessible_styles(st: Any) -> None:
    st.markdown(
        f"""
<style>
:root {{ color-scheme: light; }}
.stApp {{ color: {INK}; background: #FFFFFF; }}
[data-testid="stMetric"] {{ border: 1px solid {GRID}; padding: 0.9rem; border-radius: 0.35rem; }}
[data-testid="stSidebar"] {{ border-right: 1px solid {GRID}; }}
:focus-visible {{ outline: 3px solid {ORANGE} !important; outline-offset: 2px !important; }}
.stCaption {{ color: {MUTED}; }}
@media (prefers-reduced-motion: reduce) {{ * {{ animation: none !important; transition: none !important; }} }}
</style>
""",
        unsafe_allow_html=True,
    )


def _metric_row(frame: Any, metric: str) -> dict[str, Any] | None:
    if frame is None or frame.empty or "metric" not in frame.columns:
        return None
    selected = frame.loc[frame["metric"] == metric]
    return None if selected.empty else selected.iloc[0].to_dict()


def _series_first_nonempty(frame: Any, columns: Iterable[str], default: str) -> Any:
    import pandas as pd

    result = pd.Series("", index=frame.index, dtype="object")
    for column in columns:
        if column not in frame.columns:
            continue
        candidate = frame[column].fillna("").astype(str).str.strip()
        result = result.where(result.astype(str).str.len() > 0, candidate)
    return result.where(result.astype(str).str.len() > 0, default)


def _exclude_pit_total_rows(frame: Any) -> Any:
    """Defensively remove explicit totals if an upstream vintage violates the contract."""

    import pandas as pd

    is_total = pd.Series(False, index=frame.index)
    for column in ("okved_section", "okved_raw"):
        if column in frame.columns:
            text = frame[column].fillna("").astype(str).str.strip().str.upper()
            is_total |= text.isin({"TOTAL", "ALL", "B-E", "B–E", "B—E", "ВСЕГО"})
    for column in ("industry_name_en", "industry_name_ru"):
        if column in frame.columns:
            text = frame[column].fillna("").astype(str).str.strip().str.casefold()
            is_total |= text.isin({"total", "all industries", "всего", "итого"})
    return frame.loc[~is_total].copy()


def _first_text(frame: Any, column: str) -> str:
    if column not in frame.columns:
        return ""
    values = _nonempty_strings(frame[column].tolist())
    return values[0] if values else ""


def _nonempty_strings(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _clean_text(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"nan", "nat", "none", "<na>"} else text


def _default_units(table_names: Iterable[str]) -> str:
    names = set(table_names)
    units: list[str] = []
    if names & {"pit_receipts", "budget_execution"}:
        units.append("RUB")
    if "industrial_production" in names:
        units.append("published index")
    return ", ".join(units) or "See source register"


def _format_rub(value: Any) -> str:
    if value is None:
        return "Not available"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "Not available"
    if not math.isfinite(number):
        return "Not available"
    absolute = abs(number)
    if absolute >= 1_000_000_000:
        return f"RUB {number / 1_000_000_000:,.1f}bn"
    if absolute >= 1_000_000:
        return f"RUB {number / 1_000_000:,.1f}m"
    return f"RUB {number:,.0f}"


def _format_period(value: Any) -> str:
    """Compact period label for mixed-frequency overview cards."""

    import pandas as pd

    if value is None or pd.isna(value):
        return "period unavailable"
    return pd.Timestamp(value).strftime("%b %Y")


def _format_pct(value: Any) -> str:
    if value is None:
        return "Not available"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "Not available"
    return f"{number:.1f}%" if math.isfinite(number) else "Not available"


def _format_delta(value: Any, suffix: str) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return f"{number:+.1f}% {suffix}" if math.isfinite(number) else None


def _format_index(value: Any) -> str:
    if value is None:
        return "Not available"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "Not available"
    return f"{number:.1f}" if math.isfinite(number) else "Not available"
