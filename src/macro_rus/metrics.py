"""Transparent, testable dashboard calculations."""

from __future__ import annotations

import numpy as np
import pandas as pd


def safe_growth(current: pd.Series, previous: pd.Series) -> pd.Series:
    """Percentage growth with non-positive or missing denominators suppressed."""

    previous = pd.to_numeric(previous, errors="coerce")
    current = pd.to_numeric(current, errors="coerce")
    return ((current / previous) - 1.0).where(previous > 0) * 100.0


def add_ytd_flows(
    frame: pd.DataFrame,
    *,
    value_column: str,
    group_columns: list[str],
    date_column: str = "period",
    output_column: str = "period_flow_rub",
) -> pd.DataFrame:
    """Derive observed-period flows from cumulative YTD releases.

    A first-quarter (or January monthly) YTD observation equals its period
    flow. Later flows are produced only when the immediately preceding source
    period exists. Negative deltas are preserved because corrections and
    refunds can be economically real; callers should flag rather than delete
    them. Missing periods are never interpolated.
    """

    result = frame.copy()
    result[date_column] = pd.to_datetime(result[date_column], errors="coerce")
    result[value_column] = pd.to_numeric(result[value_column], errors="coerce")
    result["_year"] = result[date_column].dt.year
    frequency = result.get("frequency", pd.Series("quarterly", index=result.index))
    result["_slot_ordinal"] = np.where(
        frequency.eq("monthly"),
        result[date_column].dt.year * 12 + result[date_column].dt.month,
        result[date_column].dt.year * 4 + result[date_column].dt.quarter,
    )
    sort_columns = [*group_columns, "_year", date_column]
    result = result.sort_values(sort_columns, kind="stable")
    groups = result.groupby([*group_columns, "_year"], dropna=False)
    prior = groups[
        value_column
    ].shift(1)
    prior_slot = groups["_slot_ordinal"].shift(1)
    adjacent = result["_slot_ordinal"].sub(prior_slot).eq(1)
    result[output_column] = (result[value_column] - prior).where(adjacent)
    first_slot = (
        (frequency.eq("monthly") & result[date_column].dt.month.eq(1))
        | (frequency.ne("monthly") & result[date_column].dt.quarter.eq(1))
    )
    result.loc[first_slot, output_column] = result.loc[first_slot, value_column]
    return result.drop(columns=["_year", "_slot_ordinal"])


def add_same_period_yoy(
    frame: pd.DataFrame,
    *,
    value_column: str,
    group_columns: list[str],
    date_column: str = "period",
    output_column: str = "yoy_pct",
) -> pd.DataFrame:
    """Add year-on-year growth after matching calendar quarter or month."""

    result = frame.copy()
    result[date_column] = pd.to_datetime(result[date_column], errors="coerce")
    result["_year"] = result[date_column].dt.year
    result["_slot"] = np.where(
        result.get("frequency", "quarterly").eq("monthly"),
        result[date_column].dt.month,
        result[date_column].dt.quarter,
    )
    keys = [*group_columns, "_slot"]
    result = result.sort_values([*keys, "_year"], kind="stable")
    previous = result.groupby(keys, dropna=False)[value_column].shift(1)
    previous_year = result.groupby(keys, dropna=False)["_year"].shift(1)
    result[output_column] = safe_growth(result[value_column], previous).where(
        result["_year"] - previous_year == 1
    )
    return result.drop(columns=["_year", "_slot"])


def industry_contributions(
    current: pd.DataFrame,
    previous: pd.DataFrame,
    *,
    industry_column: str = "okved_section",
    value_column: str = "pit_ytd_rub",
) -> pd.DataFrame:
    """Return additive industry contributions to aggregate PIT growth.

    Contributions are percentage points relative to the previous period's
    total. Their sum equals aggregate growth when both frames cover the same
    industry perimeter.
    """

    left = current.groupby(industry_column, dropna=False)[value_column].sum()
    right = previous.groupby(industry_column, dropna=False)[value_column].sum()
    aligned = pd.concat({"current": left, "previous": right}, axis=1).fillna(0)
    denominator = aligned["previous"].sum()
    aligned["change_rub"] = aligned["current"] - aligned["previous"]
    aligned["contribution_pp"] = (
        aligned["change_rub"] / denominator * 100.0
        if denominator > 0
        else np.nan
    )
    return aligned.reset_index()


def fiscal_execution(frame: pd.DataFrame) -> pd.DataFrame:
    """Calculate descriptive execution, without labelling it a shortfall."""

    result = frame.copy()
    actual = pd.to_numeric(result["actual_ytd_rub"], errors="coerce")
    revised = pd.to_numeric(result["revised_plan_rub"], errors="coerce")
    approved = pd.to_numeric(result["approved_plan_rub"], errors="coerce")
    plan = revised.combine_first(approved)
    result["plan_used_rub"] = plan
    result["execution_pct"] = (actual / plan * 100.0).where(plan > 0)
    result["remaining_to_plan_rub"] = (plan - actual).where(plan.notna())
    return result
