"""Regional end-of-month retail fuel prices, not CPI or today's pump quotes.

The audited candidate keeps month-start keys for joins. ``period_end`` is the
actual observation label: prices stated at the end of that reporting month.
No interpolation, city averaging, national aggregation or vintage promotion
is performed here. Large positive source values are retained for review.
"""
from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path

import numpy as np
import pandas as pd

from .provenance import sha256_file

DATASET = "fuel_prices_eligible_regions_candidate.parquet"
CONCORDANCE = "outputs/regional_concordance_2026-09-29_v2/region_concordance.csv"
GRAIN = ["region_id", "period", "fuel_grade"]
MEASURE = "average_consumer_price_at_end_of_month"
CORE_GRADES = ("AI-92", "AI-95", "Diesel")
OPTIONAL_GRADES = ("AI-98 and above",)
PRODUCT_GRADES = {
    "7802": "AI-92", "7803": "AI-95", "7804": "Diesel",
    "7806": "AI-98 and above", "7800": "All petrol",
}
DEFAULT_GRADES = CORE_GRADES + OPTIONAL_GRADES
LINEAGE = ["source_file", "source_sha256", "source_sheet", "source_cell"]
REQUIRED_COLUMNS = set(GRAIN + LINEAGE + [
    "source_geography_code", "geography_name_ru", "geography_scope",
    "eligible_for_existing_dashboard", "product_code", "product_name_ru",
    "price_rub_per_litre", "measurement_basis", "quality_flag",
])


def validate_fuel_prices(
    frame: pd.DataFrame, *, eligible_region_ids: Collection[str] | None = None,
    sources: list[dict] | None = None, strict_core: bool = False,
) -> pd.DataFrame:
    """Validate identifiers and semantics; withhold invalid prices, never zero-fill.

    A missing/invalid optional price remains a source observation with a null
    analytical price. ``strict_core`` additionally rejects bad core prices, as
    the currently audited local candidate has complete positive core coverage.
    This helper does not claim that a source extraction equals a price audit.
    """
    if not REQUIRED_COLUMNS.issubset(frame.columns):
        raise ValueError("Missing fuel candidate columns")
    data = frame.copy()
    if data.empty:
        raise ValueError("Empty fuel candidate")
    if data[GRAIN].isna().any().any():
        raise ValueError("Missing fuel observation key")
    try:
        data["period"] = pd.to_datetime(data.period, errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid fuel reporting month") from exc
    if (data.period.dt.tz is not None or data.period.isna().any()
            or not data.period.dt.is_month_start.all()
            or not data.period.eq(data.period.dt.normalize()).all()):
        raise ValueError("Fuel reporting keys must be timezone-naive month starts")
    if data.duplicated(GRAIN).any():
        raise ValueError("Duplicate regional month grade fuel observations")
    if not data.geography_scope.eq("region").all():
        raise ValueError("Fuel candidate contains city or aggregate scope")
    if (not data.eligible_for_existing_dashboard.map(
            lambda value: isinstance(value, (bool, np.bool_)) and bool(value)).all()
            # Excel stores some OKATO keys as numbers, dropping one leading 0.
            # Retain that original representation; do not remap by position.
            or not data.source_geography_code.astype(str).str.fullmatch(r"\d{2,3}00000000").all()
            or data.groupby("region_id").source_geography_code.nunique().gt(1).any()):
        raise ValueError("Fuel regional scope or eligibility is inconsistent")
    if eligible_region_ids is not None:
        ids = list(eligible_region_ids)
        if not ids or len(ids) != len(set(ids)) or set(data.region_id) != set(ids):
            raise ValueError("Fuel region IDs disagree with eligible concordance")
    if not data.measurement_basis.eq(MEASURE).all():
        raise ValueError("Fuel price observation basis is not end-of-month prices")
    if not data.product_code.map(PRODUCT_GRADES).eq(data.fuel_grade).all():
        raise ValueError("Fuel product code and grade disagree")
    if (not data.source_sha256.astype(str).str.fullmatch(r"[0-9a-f]{64}").all()
            or not data.source_cell.astype(str).str.fullmatch(r"[A-Z]+[1-9]\d*").all()
            or not data.source_file.map(lambda name: isinstance(name, str)
                and Path(name).name == name and name.endswith(".xlsx")).all()):
        raise ValueError("Invalid fuel source lineage")
    expected_sheet = data.period.dt.strftime("%m(%Y)")
    if not data.source_sheet.eq(expected_sheet).all():
        raise ValueError("Fuel source sheet disagrees with reporting month")
    if data[["geography_name_ru", "product_name_ru", "quality_flag"]].isna().any().any():
        raise ValueError("Missing fuel source labels or quality flags")
    if sources is not None:
        price_sources = [s for s in sources if s.get("role") == "monthly_average_consumer_price_levels"]
        hashes = {s["filename"]: s["sha256"] for s in price_sources}
        if len(hashes) != len(price_sources) or not data.source_file.map(hashes).eq(data.source_sha256).all():
            raise ValueError("Fuel source hashes disagree with source manifest")
    raw = data.get("source_price_rub_per_litre", data.price_rub_per_litre).copy()
    numeric = pd.to_numeric(raw, errors="coerce")
    is_boolean = raw.map(lambda value: isinstance(value, (bool, np.bool_)))
    valid = numeric.notna() & np.isfinite(numeric) & numeric.gt(0) & ~is_boolean
    if strict_core and (~valid & data.fuel_grade.isin(CORE_GRADES)).any():
        raise ValueError("Missing or invalid core fuel prices")
    data["source_price_rub_per_litre"] = raw
    data["price_rub_per_litre"] = numeric.where(valid, np.nan).astype(float)
    data["availability"] = np.select(
        [valid, raw.isna()], ["reported", "missing_source_value"], default="invalid_source_value")
    data["period_end"] = data.period + pd.offsets.MonthEnd(0)
    data["units"] = "RUB/litre"
    return data.sort_values(GRAIN).reset_index(drop=True)


def load_fuel_candidate(
    root: Path, *, eligible_region_ids: Collection[str] | None = None,
    concordance_path: Path | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Load the supplied audited candidate without changing the live release.

    Private candidates retain their original extraction checks. Public snapshots
    also require their recorded dataset hash; they never fall back to private
    audit files when the publication envelope is invalid.
    """
    root = Path(root)
    try:
        public_manifest = root / "manifest.json"
        publication = {}
        dataset_hash = sha256_file(root / DATASET)
        if public_manifest.exists():
            publication = json.loads(public_manifest.read_text())
            if (publication.get("schema_version") != 1
                    or publication.get("distribution_mode") != "cloud_processed_fuel_v1"
                    or publication.get("status") != "candidate_not_promoted"
                    or publication.get("raw_files_included") is not False
                    or publication.get("dataset") != DATASET
                    or publication.get("dataset_sha256") != dataset_hash
                    or publication.get("measurement_basis") != MEASURE):
                raise ValueError("Public fuel snapshot integrity or envelope mismatch")
            summary, sources = publication["audit_summary"], publication["sources"]
        else:
            summary = json.loads((root / "summary.json").read_text())
            sources = json.loads((root / "source_manifest.json").read_text())
        if (summary.get("status") != "candidate_not_promoted"
                or summary.get("independent_raw_cell_checks_failed") != 0
                or summary.get("independent_raw_cell_checks", 0) <= 0):
            raise ValueError("Fuel candidate has not passed its extraction checks")
        if eligible_region_ids is None:
            crosswalk_path = concordance_path or Path(__file__).resolve().parents[2] / CONCORDANCE
            crosswalk = pd.read_csv(crosswalk_path)
            eligible = crosswalk.loc[crosswalk.eligible_for_candidate_profile.eq(True)]
            if len(eligible) != 78 or eligible.eligible_for_national_aggregation.eq(True).any():
                raise ValueError("Unexpected fuel concordance eligibility")
            eligible_region_ids = eligible.region_id.to_list()
        ids = list(eligible_region_ids)
        data = validate_fuel_prices(pd.read_parquet(root / DATASET),
                                   eligible_region_ids=ids, sources=sources, strict_core=True)
        months = pd.date_range(summary["period_start"], summary["period_end"], freq="MS")
        if (len(months) != summary["months"] or set(data.period) != set(months)
                or len(ids) != summary["existing_dashboard_eligible_regions"]
                or len(data) != summary["eligible_all_fuel_observations_expected"]):
            raise ValueError("Fuel candidate coverage disagrees with audit summary")
        core = data.loc[data.fuel_grade.isin(CORE_GRADES)]
        expected = pd.MultiIndex.from_product([ids, months, CORE_GRADES], names=GRAIN)
        if (len(core) != summary["eligible_core_fuel_observations_expected"]
                or not pd.MultiIndex.from_frame(core[GRAIN]).sort_values().equals(expected.sort_values())
                or summary["eligible_core_fuel_observations_missing"] != 0
                or summary["eligible_core_fuel_observations_nonpositive"] != 0):
            raise ValueError("Fuel candidate core calendar coverage is incomplete")
        manifest = dict(summary, sources=sources, eligible_region_ids=ids,
                        dataset_sha256=dataset_hash, dataset=DATASET,
                        measurement_basis=MEASURE, units="RUB/litre",
                        observation_timing="end_of_reporting_month",
                        publication_date=None, retrieval_date=None,
                        distribution_mode=publication.get("distribution_mode", "private_candidate"),
                        integrity_scope=("checksummed_public_snapshot; source_manifest_and_audited_dimensions"
                                         if publication else
                                         "source_manifest_and_audited_dimensions; computed_dataset_hash"))
        return data, manifest
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Incomplete fuel candidate audit metadata") from exc


def fuel_history(
    frame: pd.DataFrame, region_id: str, grades: Collection[str] | None = None,
) -> pd.DataFrame:
    """Compute changes only against exact same-region, same-grade calendar keys.

    Both comparison source cells and expected/actual comparison dates remain
    available for inspection/export. A gap or unavailable prior value is not
    replaced by the previous surviving record.
    """
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = validate_fuel_prices(frame)
    selected_grades = list(DEFAULT_GRADES if grades is None else grades)
    if any(grade not in PRODUCT_GRADES.values() for grade in selected_grades):
        raise ValueError("Unknown fuel grade")
    data = data.loc[data.region_id.eq(region_id) & data.fuel_grade.isin(selected_grades)].copy()
    if data.empty:
        return data
    for months, prefix, change in ((1, "previous_month", "mom"), (12, "previous_year", "yoy")):
        prior_cols = GRAIN + ["period_end", "price_rub_per_litre", "availability"] + LINEAGE
        prior = data[prior_cols].rename(columns={c: f"{prefix}_{c}" for c in prior_cols
                                               if c not in ("region_id", "fuel_grade")})
        prior["period"] = prior[f"{prefix}_period"] + pd.DateOffset(months=months)
        data = data.merge(prior, on=GRAIN, how="left", validate="one_to_one")
        data[f"expected_{prefix}_period"] = data.period - pd.DateOffset(months=months)
        previous_price = data[f"{prefix}_price_rub_per_litre"]
        matched = data.price_rub_per_litre.notna() & previous_price.notna()
        data[f"{change}_change_pct"] = (
            100 * (data.price_rub_per_litre - previous_price) / previous_price).where(matched)
        data[f"{change}_status"] = np.select(
            [data.price_rub_per_litre.isna(), data[f"{prefix}_period"].isna(), previous_price.isna()],
            ["current_price_unavailable", "prior_period_unavailable", "prior_price_unavailable"], default="matched")
    magnitude = data.mom_change_pct.abs()
    # Avoid floating-point noise turning a mathematically exact 10% into >10%.
    data["mom_review_flag"] = magnitude.gt(10) & ~np.isclose(magnitude, 10, rtol=0, atol=1e-10)
    data["review_status"] = np.select(
        [data.mom_review_flag, data.mom_change_pct.isna()],
        ["review_large_monthly_change", "not_assessable_missing_comparison"],
        default="no_large_monthly_change_flag")
    return data.sort_values(["period", "fuel_grade"]).reset_index(drop=True)


def latest_fuel_prices(
    frame: pd.DataFrame, region_id: str, *, period=None,
    grades: Collection[str] = CORE_GRADES,
) -> pd.DataFrame:
    """One reporting-month view, never an older-price fallback per grade.

    An absent grade receives an explicitly unavailable display row with no
    source lineage, not a claimed source observation. This is only a view;
    it is never added to the candidate dataset or historical export.
    """
    history = fuel_history(frame, region_id, grades=tuple(PRODUCT_GRADES.values()))
    if history.empty:
        return history
    wanted = list(grades)
    if len(wanted) != len(set(wanted)) or any(g not in PRODUCT_GRADES.values() for g in wanted):
        raise ValueError("Invalid requested fuel grades")
    reporting_month = history.period.max() if period is None else pd.Timestamp(period)
    if (reporting_month.tzinfo is not None or not reporting_month.is_month_start
            or reporting_month != reporting_month.normalize()):
        raise ValueError("Latest fuel view needs a reporting-month-start key")
    current = history.loc[history.period.eq(reporting_month)].set_index("fuel_grade")
    result = current.reindex(wanted)
    present = result.index.isin(current.index)
    result["observation_present"] = present
    result["region_id"] = region_id
    result["period"] = reporting_month
    result["period_end"] = reporting_month + pd.offsets.MonthEnd(0)
    result["units"] = "RUB/litre"
    result.loc[~present, "availability"] = "no_observation_for_reporting_month"
    result.loc[~present, "mom_status"] = "current_price_unavailable"
    result.loc[~present, "yoy_status"] = "current_price_unavailable"
    result["mom_review_flag"] = result.mom_review_flag.eq(True)
    return result.reset_index()


def fuel_export(
    frame: pd.DataFrame, region_id: str, grades: Collection[str] | None = None,
) -> pd.DataFrame:
    """Export the same scoped history, prices, flags and comparison lineage."""
    return fuel_history(frame, region_id, grades=grades)
