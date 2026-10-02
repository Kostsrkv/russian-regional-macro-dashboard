"""Source-native, same-window industrial PIT / production comparisons.

PIT is nominal taxable-income receipts, not physical output. Calendar alignment
does not establish economic causality or make the measures interchangeable.
"""
from __future__ import annotations

from pathlib import PurePosixPath

import numpy as np
import pandas as pd

SECTIONS = {
    "B": "Mining and quarrying",
    "C": "Manufacturing",
    "D": "Electricity, gas and steam",
    "E": "Water, sewerage and waste",
}
GRAIN = ["region_id", "period", "okved_section"]


def _numeric(frame, column):
    raw = frame[column]
    numeric = pd.to_numeric(raw, errors="coerce")
    if (raw.notna() & numeric.isna()).any() or not np.isfinite(numeric.dropna()).all():
        raise ValueError(f"Invalid numeric {column}")
    frame[column] = numeric


def matched_pit_production(pit, cumulative, sources=None, region_id=None, period=None):
    """Return all B–E cells at PIT cutoffs, including absent sector observations.

    Uses reported PIT YTD, never Q4 flows or monthly-production growth averages.
    Upstream candidate loaders select vintages; duplicate selected grains fail
    closed here rather than being summed or filled from an older release.
    """
    if pit is None or cumulative is None or pit.empty:
        return pd.DataFrame()
    required_pit = set(GRAIN + ["pit_ytd_rub", "source_id", "source_vintage"])
    required_production = set(GRAIN + ["index_value", "index_measure", "window_start", "frequency"])
    if not required_pit.issubset(pit.columns) or not required_production.issubset(cumulative.columns):
        raise ValueError("Missing same-window PIT or cumulative production fields")
    p = pit.copy()
    c = cumulative.copy()
    for frame in (p, c):
        frame["period"] = pd.to_datetime(frame.period, errors="raise")
        if frame.period.isna().any() or not frame.period.dt.is_month_end.all():
            raise ValueError("Comparison periods must be valid month-end dates")
        if frame[GRAIN].isna().any().any():
            raise ValueError("Missing comparison grain")
    c["window_start"] = pd.to_datetime(c.window_start, errors="raise")
    start = pd.to_datetime(c.period.dt.year.astype(str) + "-01-01")
    if not c.index_measure.eq("same_period_previous_year_pct").all() or not c.frequency.eq("YTD").all() or not c.window_start.eq(start).all():
        raise ValueError("Production must be source-native January-to-cutoff YTD")
    _numeric(p, "pit_ytd_rub")
    _numeric(c, "index_value")
    if c.index_value.dropna().lt(0).any():
        raise ValueError("Negative production index")
    # Only standalone section observations belong in this B–E perimeter.
    p = p.loc[p.okved_section.isin(SECTIONS)].copy()
    c = c.loc[c.okved_section.isin(SECTIONS)].copy()
    if "okved_raw" in p and not p.okved_raw.eq(p.okved_section).all():
        raise ValueError("PIT section detail must not be added to its parent")
    for frame in (p, c):
        if frame.duplicated(GRAIN).any():
            raise ValueError("Comparison requires unique selected-vintage grain")
    population = pit[["region_id", "period"]].drop_duplicates().copy()
    population["period"] = pd.to_datetime(population.period, errors="raise")
    if region_id is not None:
        population = population.loc[population.region_id.eq(region_id)]
    if period is not None:
        population = population.loc[population.period.eq(pd.Timestamp(period))]
    source_map = {}
    if sources is not None:
        if "source_id" not in sources or sources.source_id.duplicated().any():
            raise ValueError("Source identifiers must be unique")
        source_map = sources.set_index("source_id").to_dict("index")
        if not set(p.source_id.dropna()).issubset(source_map):
            raise ValueError("Orphan PIT source identifier")
    p_index = p.set_index(GRAIN)
    c_index = c.set_index(GRAIN)

    def receipt_lineage(row, prefix):
        result = {f"{prefix}_{key}": None for key in ("source_id", "source_vintage", "source_file", "source_sha256", "source_url", "revision_status", "quality_status")}
        if row is None:
            return result
        result[f"{prefix}_source_id"] = row.source_id
        result[f"{prefix}_source_vintage"] = row.source_vintage
        result[f"{prefix}_revision_status"] = row.get("revision_status")
        result[f"{prefix}_quality_status"] = row.get("quality_status")
        source = source_map.get(row.source_id, {})
        local = str(source.get("local_path", ""))
        result[f"{prefix}_source_file"] = PurePosixPath(local.replace("\\", "/")).name if local else None
        result[f"{prefix}_source_sha256"] = source.get("sha256") or None
        result[f"{prefix}_source_url"] = source.get("source_url") or None
        return result

    def lookup(frame, key):
        return frame.loc[key] if key in frame.index else None

    def release_context(region, date, observation):
        if observation is not None:
            return observation
        # An absent section can still be traced to the release in which it is
        # absent. This supplies file metadata only, never another section's tax.
        release = p.loc[p.region_id.eq(region) & p.period.eq(date)]
        if release.empty:
            return None
        if len(release[["source_id", "source_vintage"]].drop_duplicates()) != 1:
            raise ValueError("Ambiguous regional PIT release for missing section")
        return release.iloc[0]

    rows = []
    for current in population.sort_values(["region_id", "period"]).itertuples(index=False):
        end = pd.Timestamp(current.period)
        previous = end - pd.DateOffset(years=1) + pd.offsets.MonthEnd(0)
        for section, name in SECTIONS.items():
            now = lookup(p_index, (current.region_id, end, section))
            before = lookup(p_index, (current.region_id, previous, section))
            production = lookup(c_index, (current.region_id, end, section))
            value = now.pit_ytd_rub if now is not None else np.nan
            prior = before.pit_ytd_rub if before is not None else np.nan
            index = production.index_value if production is not None else np.nan
            change = value - prior if pd.notna(value) and pd.notna(prior) else np.nan
            growth = 100 * change / prior if pd.notna(change) and prior > 0 else np.nan
            issues = []
            if pd.isna(value):
                issues.append("current_pit_unavailable")
            if pd.isna(prior):
                issues.append("prior_pit_unavailable")
            elif prior <= 0:
                issues.append("nonpositive_prior_pit_growth_withheld")
            if pd.isna(index):
                issues.append("production_unavailable")
            row = dict(region_id=current.region_id, period=end,
                window_start=pd.Timestamp(end.year, 1, 1), prior_period=previous,
                frequency="YTD", units_pit="RUB", units_production="previous same period = 100",
                okved_section=section, industry=name,
                pit_current_rub=value, pit_prior_rub=prior, pit_change_rub=change,
                pit_yoy_pct=growth, index_value=index,
                production_yoy_pct=index - 100 if pd.notna(index) else np.nan,
                index_measure="same_period_previous_year_pct",
                availability=";".join(issues) if issues else "matched",
                quality_status="candidate_not_promoted")
            row.update(receipt_lineage(release_context(current.region_id, end, now), "pit_current"))
            row.update(receipt_lineage(release_context(current.region_id, previous, before), "pit_prior"))
            for key in ("source_id", "source_file", "source_sha256", "source_vintage", "source_url", "source_sheet", "source_cell", "index_base", "availability", "revision_state"):
                row[f"production_{key}"] = production.get(key) if production is not None else None
            rows.append(row)
    return pd.DataFrame(rows)
