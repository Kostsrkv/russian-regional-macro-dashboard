"""Prepare and seal a compact offline handoff from previously checked inputs.

No downloads, raw-file redistribution, promotion or dashboard edits. Existing
payloads are copied byte-for-byte; new CSV metadata is authored by the companion
Artifact Tool builder. Analytical tables remain at their native grains; the
optional production-history extension never substitutes one index basis for another.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date
import hashlib
import json
import re
import shutil
import stat
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd

DATASETS = {
    "pit_receipts": ("macro/pit_receipts.parquet", ["region_id", "period", "okved_section"], "pit_ytd_rub", "FNS industry-associated PIT", "Reported YTD RUB; period flows only where source endpoints support them"),
    "budget_execution": ("revenue/budget_execution.parquet", ["region_id", "period", "budget_level", "revenue_code"], "actual_ytd_rub", "Treasury revenue codes", "Nominal YTD RUB; do not sum nested revenue codes"),
    "fiscal_observations": ("fiscal/fiscal_observations.csv.gz", ["region_id", "period", "budget_level", "kind", "metric_code"], "actual_ytd_rub", "Treasury spending, balance and financing", "Nominal YTD RUB; consolidated regional + municipal, excluding health-insurance funds"),
    "industrial_production_cumulative": ("cumulative/industrial_production_cumulative.parquet", ["region_id", "period", "okved_section"], "index_value", "Rosstat January-to-month production", "Same window previous year = 100; not a standalone monthly or quarter index"),
    "fuel_prices": ("fuel/fuel_prices_eligible_regions_candidate.parquet", ["region_id", "period", "fuel_grade"], "price_rub_per_litre", "Rosstat regional retail fuel prices", "Spatial-average end-of-month RUB/litre; month-start storage keys"),
    "industrial_production_monthly": ("macro/industrial_production.parquet", ["region_id", "period", "okved_section"], "index_value", "Rosstat standalone-month production", "Same month previous year = 100; missing source cells may be absent rows"),
    "industrial_production_annual": ("annual/industrial_production_annual.parquet", ["region_id", "period", "okved_section"], "index_value", "Rosstat full-year production", "Whole year previous year = 100; not an average of monthly indices"),
}
CORE_DATASETS = tuple(name for name in DATASETS if name not in
                      {"industrial_production_monthly", "industrial_production_annual"})
PRODUCTION_BASES = {
    "industrial_production_cumulative": ("YTD", "same_period_previous_year_pct"),
    "industrial_production_monthly": ("monthly", "same_month_previous_year_pct"),
    "industrial_production_annual": ("annual", "same_year_previous_year_pct"),
}

FIELD_INFO = {
    "region_id": "Stable project join key. RU-KLU/RU-SVE are pilot keys; FNS-XX keys are not ISO codes. Use region_registry.csv.",
    "region_name_ru": "Original Russian regional name retained by the dataset.",
    "region_name_en": "Available English regional label; blank when no reviewed translation exists.",
    "period": "Analytical period key. Fuel uses month starts; fiscal/PIT/production use reporting window ends. Not a publication date.",
    "reporting_cutoff": "Source as-of cutoff. A first-of-month cutoff usually refers to the preceding completed period.",
    "frequency": "Source/canonical frequency label. Revenue can say quarterly while its amounts are cumulative YTD.",
    "okved_raw": "Original retained economic-activity code. Do not interpret the code as the location of physical output.",
    "okved_section": "Harmonized section. B-E are industrial scopes; TOTAL is headline industry. T-U is combined; UNMAPPED is retained separately.",
    "industry_name_en": "English industry label linked to the retained activity classification.",
    "industry_name_ru": "Original Russian industry label.",
    "pit_ytd_rub": "FNS reported cumulative PIT associated with a region/activity, converted from thousand RUB to RUB. Not Treasury retained PIT.",
    "pit_flow_rub": "Period receipt flow where exact endpoints support derivation. FY 2025 standalone-quarter flows are unavailable without preceding cutoffs.",
    "cpi_index": "Reserved regional CPI field. Currently missing; it must not produce real-growth estimates.",
    "source_id": "Join key to the corresponding source catalogue; not a global business/entity identifier.",
    "source_vintage": "Selected source version identifier/date. A generated version ID or observation cutoff is not necessarily a publication date.",
    "revision_status": "Upstream selection/revision label preserved without promoting it to verified status.",
    "revision_state": "Production source revision/selection state.",
    "quality_status": "Original research-review label; the package remains candidate_not_promoted.",
    "is_official": "Source designation, not independent verification of extraction or official reporting.",
    "budget_level": "Consolidated regional plus municipal budget, excluding territorial health-insurance funds.",
    "revenue_code": "Treasury code. TOTAL_REVENUE, 100 and 200 overlap with their children. Never sum parent and child codes.",
    "revenue_name_en": "English revenue-code label.",
    "revenue_name_ru": "Original Russian revenue-code label.",
    "actual_ytd_rub": "Signed reported actual cumulative RUB. Kind/code define whether it is a total, function or financing amount.",
    "approved_plan_rub": "Legacy field holding the source plan at the reporting cutoff. Original enacted-budget status/seasonal path are not established.",
    "revised_plan_rub": "Separate revised-plan field. Currently missing; do not infer amendments from it.",
    "plan_components_reconciled": "Recorded plan component reconciliation flag. It does not establish an original versus revised budget path.",
    "territory_key": "Original Treasury-name-based mapping key; the registry governs eligibility.",
    "kind": "Distinguishes summary totals, top-level expenditure functions and financing components. Different kinds are not additive.",
    "metric_code": "Fiscal observation code. Top-level spending codes 0100-1400; summary and financing codes remain separate.",
    "name_ru": "Original Russian fiscal row label.",
    "source_line_code": "Report's original row/line code.",
    "source_row_index": "Extractor row locator in the source report. Retain alongside the member/cell evidence.",
    "plan_column_index": "Extractor locator for the source plan column.",
    "actual_column_index": "Extractor locator for the source actual column.",
    "plan_at_cutoff_rub": "Reported annual plan at the source cutoff, not a seasonally phased target or independently verified original plan.",
    "units": "Recorded source/analytical unit; amounts in fiscal/revenue tables are RUB.",
    "source_file": "Original raw source basename; the raw file is not distributed in this package.",
    "source_sha256": "SHA-256 of the original source file, not of this processed table.",
    "source_member": "HTML member within the Treasury archive.",
    "source_url": "Recorded original-source URL. An empty URL must not be replaced by a guessed download link.",
    "window_start": "Beginning of the cumulative output window, always 1 January of the period year.",
    "window_months": "Number of January-to-month observations included in the published cumulative window.",
    "index_value": "Published production index; matching previous-year month, cumulative window or whole year = 100. Read index_measure.",
    "index_measure": "Recorded comparison basis. Same-month, January-to-month and whole-year indices are separate measures.",
    "index_base": "Source weighting/base-year caveat; not the comparison denominator of 100.",
    "growth_yoy_pct": "Published production index minus 100; the recorded comparison basis determines the matching prior-year window.",
    "source_sheet": "Original workbook sheet locator.",
    "source_cell": "Original Excel A1 cell locator.",
    "source_region_name": "Russian geography label observed in the original production workbook.",
    "region_match_method": "Recorded name/alias mapping method, not a confidence score.",
    "scope_qualifier": "Source geography/perimeter qualifier retained for comparability.",
    "raw_value": "Original cell value or missing marker before numeric interpretation.",
    "raw_year_header": "Original source year header, retaining footnotes.",
    "raw_window_header": "Original January-to-month header, retaining footnotes.",
    "availability": "Whether the source value is reported or explicitly unavailable; absence is not zero.",
    "missing_source_value": "Recorded source missing-value flag.",
    "year_header_has_footnote": "Whether the original year header carries a footnote.",
    "window_header_has_footnote": "Whether the original window header carries a footnote.",
    "source_geography_code": "Original regional OKATO-like source identifier. Import as text to preserve leading zeros; use the registry.",
    "geography_name_ru": "Original fuel-source geography label.",
    "geography_scope": "Must equal region; city and national aggregate prices are not included.",
    "eligible_for_existing_dashboard": "Source-concordance eligibility flag; not permission for national aggregation.",
    "product_code": "Original Rosstat fuel product identifier; import as text.",
    "fuel_grade": "Separate fuel grade. All petrol is an aggregate and must not be added to individual grades.",
    "product_name_ru": "Original Russian fuel product label.",
    "price_rub_per_litre": "Positive spatial-average consumer price at end of reporting month, in RUB/litre. Not CPI or a fiscal-period average.",
    "measurement_basis": "average_consumer_price_at_end_of_month; no daily, wholesale or period-average substitution.",
    "quality_flag": "Preserved price extraction/review flag. Large positive source values are not silently removed.",
}

UNITS = {
    "pit_ytd_rub": "RUB", "pit_flow_rub": "RUB", "actual_ytd_rub": "RUB",
    "approved_plan_rub": "RUB", "revised_plan_rub": "RUB", "plan_at_cutoff_rub": "RUB",
    "price_rub_per_litre": "RUB/litre", "growth_yoy_pct": "%",
    "index_value": "index; same prior-year window = 100", "cpi_index": "unavailable CPI basis",
    "window_months": "months",
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def hydrated(path: Path) -> bool:
    """Do not trigger a blocking read of macOS iCloud-offloaded files."""
    return path.is_file() and not path.is_symlink() and not (path.stat().st_flags & getattr(stat, "SF_DATALESS", 0x40000000))


def read_table(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, dtype={"metric_code": str, "revenue_code": str}, keep_default_na=False)


def validate_table(name: str, frame: pd.DataFrame, eligible: set[str]) -> None:
    _, grain, value, _, _ = DATASETS[name]
    if not set(grain + [value]).issubset(frame):
        raise ValueError(f"Missing observation contract: {name}")
    if frame[grain].isna().any().any() or frame.duplicated(grain).any() or set(frame.region_id) != eligible:
        raise ValueError(f"Invalid selected-vintage grain/region coverage: {name}")
    numbers = pd.to_numeric(frame[value], errors="coerce")
    invalid = frame[value].notna() & frame[value].ne("") & numbers.isna()
    boolean = frame[value].map(lambda v: isinstance(v, (bool, np.bool_)))
    if np.isinf(numbers.dropna()).any() or invalid.any() or boolean.any():
        raise ValueError(f"Invalid/non-numeric source value: {name}")
    dates = pd.to_datetime(frame.period, errors="raise")
    if dates.isna().any():
        raise ValueError(f"Missing period: {name}")
    if name in ("fiscal_observations", "budget_execution") and not frame.budget_level.eq("consolidated_regional_budget").all():
        raise ValueError("Budget perimeter mismatch")
    if name == "fiscal_observations" and (not frame.units.eq("RUB").all() or not frame.frequency.eq("YTD").all()):
        raise ValueError("Fiscal unit or YTD basis mismatch")
    if name in PRODUCTION_BASES:
        frequency, index_measure = PRODUCTION_BASES[name]
        if not frame.frequency.eq(frequency).all() or not frame.index_measure.eq(index_measure).all():
            raise ValueError("Production basis mismatch")
        if not dates.dt.is_month_end.all() or not set(frame.okved_section).issubset({"TOTAL", "B", "C", "D", "E"}):
            raise ValueError("Production endpoint or industrial scope mismatch")
        if name == "industrial_production_annual" and not dates.dt.month.eq(12).all():
            raise ValueError("Annual production must represent a completed calendar year")
        if numbers.dropna().lt(0).any():
            raise ValueError("Negative production index")
        if "growth_yoy_pct" in frame and not np.allclose(frame.growth_yoy_pct, frame.index_value - 100, equal_nan=True):
            raise ValueError("Production growth disagrees with published index")
    if name == "fuel_prices":
        if not dates.dt.is_month_start.all() or not frame.geography_scope.eq("region").all():
            raise ValueError("Fuel geography or endpoint key mismatch")
        if not frame.measurement_basis.eq("average_consumer_price_at_end_of_month").all():
            raise ValueError("Fuel measurement basis mismatch")
        core = frame.fuel_grade.isin(["AI-92", "AI-95", "Diesel"])
        if numbers[core].isna().any() or numbers[core].le(0).any():
            raise ValueError("Core fuel price unavailable or nonpositive")


def coverage_rows(name: str, frame: pd.DataFrame) -> list[dict]:
    _, _, measure, _, basis = DATASETS[name]
    work = frame.copy()
    work["period"] = pd.to_datetime(work.period)
    work["_value"] = pd.to_numeric(work[measure], errors="coerce")
    result = []
    for region_id, group in work.groupby("region_id", sort=True):
        latest = group.loc[group.period.eq(group.period.max())]
        gap = ""
        if name == "pit_receipts" and "B" not in set(latest.okved_section):
            gap = "Mining B absent in latest source classification; aggregate PIT growth is not comparable."
        if name == "fuel_prices":
            gap = "Optional grades may be missing; keep grades separate and do not sum All petrol with individual grades."
        expected_rows = None
        missing_calendar_rows = None
        if name in PRODUCTION_BASES:
            # Explicitly count holes without fabricating zero-valued source rows.
            calendar = pd.period_range(work.period.min(), work.period.max(),
                                       freq="Y" if name == "industrial_production_annual" else "M")
            expected_rows = len(calendar) * 5  # Published headline and disjoint B–E scopes.
            missing_calendar_rows = expected_rows - len(group)
            if missing_calendar_rows < 0:
                raise ValueError("Production exceeds its stated calendar/scope grain")
            if name == "industrial_production_monthly":
                gap = "Missing source cells can be absent rows; calendar holes are unavailable, not zero. Weight-base changes remain recorded."
        result.append(dict(dataset=name, region_id=region_id, first_period=group.period.min().strftime("%Y-%m-%d"),
                           latest_period=group.period.max().strftime("%Y-%m-%d"), periods_present=group.period.nunique(),
                           rows=len(group), reported_primary_values=int(group._value.notna().sum()),
                           missing_primary_values=int(group._value.isna().sum()), latest_rows=len(latest),
                           latest_reported_values=int(latest._value.notna().sum()),
                           missing_period_flows=int(group.pit_flow_rub.isna().sum()) if name == "pit_receipts" else None,
                           expected_calendar_rows=expected_rows, missing_calendar_rows=missing_calendar_rows,
                           amount_or_index_basis=basis, known_limit=gap))
    return result


def dictionary_rows(name: str, frame: pd.DataFrame) -> list[dict]:
    _, grain, _, _, _ = DATASETS[name]
    return [dict(dataset=name, field=field, stored_type=str(frame[field].dtype),
                 units=UNITS.get(field, ""), observation_key=field in grain,
                 missing_rows=int((frame[field].isna() | frame[field].eq("")).sum()),
                 definition=FIELD_INFO[field]) for field in frame.columns]


def prepare(project: Path, output: Path, metadata_json: Path, *,
            include_production_history: bool = False, prepared_on: str | None = None) -> dict:
    prepared_on = date.fromisoformat(prepared_on or date.today().isoformat()).isoformat()
    selected = tuple(DATASETS) if include_production_history else CORE_DATASETS
    guides = {"START_HERE.md", "JOIN_GUIDE.md", "MEASURES.md"}
    if output.exists() and any(p.name not in guides or not p.is_file() for p in output.iterdir()):
        raise ValueError("Refusing to overwrite an existing handoff directory")
    notes_root = project / "outputs/regional_notes_2026-10-07"
    inputs = notes_root / "inputs"
    source_manifest = inputs / "manifest.json"
    manifest = json.loads(source_manifest.read_text())
    note_summary = json.loads((notes_root / "summary.json").read_text())
    pointer = json.loads((project / "data/dashboard/current.json").read_text())
    if (digest(source_manifest) != note_summary["source_manifest_sha256"]
            or pointer["manifest_sha256"] != digest(source_manifest)
            or manifest["review_status"] != "candidate_not_promoted" or manifest["raw_files_included"] is not False):
        raise ValueError("Notes and selected dashboard source release disagree")
    for name, record in note_summary["outputs"].items():
        if not hydrated(notes_root / name) or digest(notes_root / name) != record["sha256"]:
            raise ValueError(f"Previously checked note artifact changed/unavailable: {name}")
    registry_path = project / "outputs/regional_concordance_2026-09-29_v2/region_concordance.csv"
    registry = pd.read_csv(registry_path, dtype=str, keep_default_na=False)
    included = registry.loc[registry.eligible_for_candidate_profile.eq("True")].copy()
    eligible = set(included.region_id)
    if len(included) != 78 or len(eligible) != 78 or not included.eligible_for_national_aggregation.eq("False").all():
        raise ValueError("Unexpected territory eligibility")
    release = project / "data/dashboard" / pointer["release_path"]
    required = {DATASETS[name][0] for name in selected} | {
        "revenue/lineage.csv.gz", "revenue/sources.csv", "macro/sources.csv",
        "fiscal/manifest.json", "revenue/manifest.json", "fuel/manifest.json"}
    payload_paths, unavailable_audits = {}, []
    for name in note_summary["source_payloads"]:
        path = inputs / name
        if not hydrated(path) and hydrated(release / name):
            path = release / name
        if not hydrated(path) and name not in required:
            unavailable_audits.append(name)
            continue
        payload_paths[name] = path
    extras = ("macro/industrial_production.parquet", "annual/industrial_production_annual.parquet", "annual/manifest.json") if include_production_history else ()
    for name in ("macro/pit_receipts.parquet", "macro/sources.csv", *extras):
        payload_paths[name] = release / name
    for name, path in payload_paths.items():
        record = manifest["payloads"][name]
        if not hydrated(path) or digest(path) != record["sha256"] or path.stat().st_size != record["bytes"]:
            raise ValueError(f"Selected source payload changed/unavailable: {name}")
    tables = {name: read_table(payload_paths[DATASETS[name][0]]) for name in selected}
    for name, frame in tables.items():
        validate_table(name, frame, eligible)
    # Fiscal/revenue totals must reconcile, and lineage joins must remain 1:1.
    budget = tables["budget_execution"]
    lineage = pd.read_csv(payload_paths["revenue/lineage.csv.gz"], dtype={"revenue_code": str}, keep_default_na=False)
    keys = ["region_id", "period", "source_id", "source_vintage", "revenue_code"]
    left = budget.copy()
    left["period"] = pd.to_datetime(left.period).dt.strftime("%Y-%m-%d")
    joined = left.merge(lineage[keys + ["actual_ytd_rub"]], on=keys, validate="one_to_one", suffixes=("", "_lineage"))
    if len(joined) != len(budget) or not np.allclose(joined.actual_ytd_rub, joined.actual_ytd_rub_lineage, atol=.02, rtol=0):
        raise ValueError("Revenue lineage mismatch")
    fiscal = tables["fiscal_observations"]
    ftotal = fiscal.loc[fiscal.kind.eq("summary") & fiscal.metric_code.eq("revenue_total")].copy()
    ftotal["period"] = pd.to_datetime(ftotal.period).dt.strftime("%Y-%m-%d")
    ftotal["actual_ytd_rub"] = pd.to_numeric(ftotal.actual_ytd_rub, errors="raise")
    total = left.loc[left.revenue_code.eq("TOTAL_REVENUE")].merge(
        ftotal[["region_id", "period", "actual_ytd_rub"]], on=["region_id", "period"], validate="one_to_one", suffixes=("", "_fiscal"))
    if len(total) != 624 or not np.allclose(total.actual_ytd_rub, total.actual_ytd_rub_fiscal, atol=.02, rtol=0):
        raise ValueError("Fiscal/revenue total reconciliation failed")
    sources = pd.read_csv(payload_paths["macro/sources.csv"], keep_default_na=False)
    if not set(tables["pit_receipts"].source_id).issubset(set(sources.source_id)):
        raise ValueError("PIT has orphan source IDs")
    if include_production_history and not set(tables["industrial_production_monthly"].source_id).issubset(set(sources.source_id)):
        raise ValueError("Monthly production has orphan source IDs")
    # Native processed datasets and extraction evidence remain byte-identical.
    for name, path in payload_paths.items():
        destination = output / "data" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    provenance = output / "provenance"
    provenance.mkdir()
    shutil.copyfile(source_manifest, provenance / "source_release_manifest.json")
    shutil.copyfile(registry_path, provenance / "territory_concordance.csv")
    shutil.copyfile(notes_root / "coverage.csv", provenance / "note_window_coverage.csv")
    notes = json.loads((notes_root / "regional_notes.json").read_text())
    pilots = [n for n in notes if n["region_id"] in {"RU-KLU", "RU-SVE"}
              and n["period"] == note_summary["latest_period"] and n["amount_basis"] == "YTD"]
    if len(pilots) != 2:
        raise ValueError("Missing predeclared pilot notes")
    notes_dir = output / "regional_notes"
    notes_dir.mkdir()
    for note in pilots:
        name = f"{note['region_id']}_{note['period']}_H1.md"
        text = (notes_root / "notes" / name).read_text()
        if any(f"- {paragraph}" not in text for paragraph in note["paragraphs"]):
            raise ValueError("Pilot Markdown narrative disagrees with the checked note")
        shutil.copyfile(notes_root / "notes" / name, notes_dir / name)
    (notes_dir / "pilot_claims_and_sources.json").write_text(json.dumps(pilots, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    prior_qa = json.loads((notes_root / "validation.json").read_text())
    (provenance / "prior_note_checks.json").write_text(json.dumps({key: prior_qa[key] for key in
        ("validated_on", "source_review_status", "regression_tests", "final_fiscal_and_note_tests",
         "independent_source_claim_checks", "independent_production_and_fuel_checks", "unavailable_claims_preserved")}, indent=2) + "\n")
    catalog, coverage, fields = [], [], []
    for name in selected:
        path, grain, measure, label, basis = DATASETS[name]
        data = tables[name]
        dates = pd.to_datetime(data.period)
        catalog.append(dict(dataset=name, label=label, included=True, file="data/" + path, rows=len(data), regions=len(eligible),
                            first_period=dates.min().strftime("%Y-%m-%d"), latest_period=dates.max().strftime("%Y-%m-%d"),
                            grain=" + ".join(grain), basis=basis, availability="payload_hash_verified",
                            source_review_status="candidate_not_promoted"))
        coverage.extend(coverage_rows(name, data))
        fields.extend(dictionary_rows(name, data))
    for name, path in (("industrial_production_monthly", "macro/industrial_production.parquet"),
                       ("industrial_production_annual", "annual/industrial_production_annual.parquet")):
        if name in selected:
            continue
        catalog.append(dict(dataset=name, label="Existing dashboard production dataset", included=False, file="",
                            rows=None, regions=None, first_period="", latest_period="", grain="", basis="",
                            availability=("existing_payload_offloaded_not_packaged" if not hydrated(release / path)
                                          else "existing_payload_not_selected_for_compact_scope"),
                            source_review_status="candidate_not_promoted"))
    fuel_codes = tables["fuel_prices"][["region_id", "source_geography_code"]].drop_duplicates()
    if fuel_codes.region_id.duplicated().any():
        raise ValueError("Ambiguous fuel territory mapping")
    names = included.set_index("region_id")
    region_records = [dict(region_id=row.region_id, region_name_ru=names.loc[row.region_id, "treasury_name_ru"],
                           fns_code=names.loc[row.region_id, "fns_code"].zfill(2),
                           fuel_source_geography_code=str(row.source_geography_code),
                           eligibility_status=names.loc[row.region_id, "status"], national_aggregation_allowed=False)
                      for row in fuel_codes.sort_values("region_id").itertuples(index=False)]
    gaps = [
        ("regional_CPI", "not_in_release", "Real tax/wage growth and total inflation", "Rosstat", "Do not use fuel-price changes as CPI."),
        ("regional_industry_wages", "not_in_release", "Nominal and real wage change by industry", "Rosstat/reviewed research replication files", "PIT receipts are not average wages."),
        ("population_and_employment", "not_in_release", "Per-capita measures and labour-market checks", "Rosstat/reviewed research replication files", "No invented denominators."),
        ("original_and_amended_budget_paths", "not_in_release", "Plan-based shortfall and amendment analysis", "Regional finance ministries/Budget Portal", "Legacy plan fields are source plans at cutoff, not verified original plans."),
        ("regional_debt_stock", "not_in_release", "Debt level and composition", "Ministry of Finance/regional finance ministries", "Financing flows do not measure debt stocks."),
        ("FNS_history_and_mining_perimeter", "incomplete_source_pairs_and_schema_break", "Comparable quarterly industry PIT history", "FNS Form 1-NOM", "Mining B disappears in latest classification; FY quarter flow is unavailable."),
        ("military_contract_or_recruitment_spending", "not_identified_in_release", "Separately documented payments/contracts", "Relevant official documents/procurement sources", "National-defence function 0200 is not an equivalent measure."),
        ("product_CPI_and_industry_producer_prices", "not_in_release", "Consumer-item and producer-price changes", "Rosstat", "Consumer fuel prices are not industry producer-price inflation."),
    ]
    records = {
        "dataset_catalog": catalog, "coverage_register": coverage, "region_registry": region_records,
        "data_dictionary": fields, "missing_data_register": [dict(dataset=g[0], availability=g[1], analysis_enabled=g[2],
                                                                   source_family=g[3], limitation=g[4]) for g in gaps],
    }
    metadata_json.parent.mkdir(parents=True, exist_ok=True)
    metadata_json.write_text(json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    summary = dict(package_id=output.name, prepared_on=prepared_on, source_release=manifest["release_id"],
                   source_manifest_sha256=digest(source_manifest), source_review_status="candidate_not_promoted",
                   source_payloads_verified=len(payload_paths), eligible_regions=len(eligible), datasets_included=len(tables),
                   rows={name: len(data) for name, data in tables.items()}, fiscal_revenue_total_checks=len(total),
                   revenue_lineage_checks=len(joined), source_concordance_sha256=digest(registry_path),
                   source_notes_sha256=note_summary["outputs"]["regional_notes.json"]["sha256"],
                   unsupported_files=[r["dataset"] for r in catalog if not r["included"]],
                   optional_audits_not_included=unavailable_audits,
                   raw_files_included=False, external_distribution_authorized=False, metadata_tables=list(records))
    (provenance / "handoff_checks.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def seal(output: Path, metadata_json: Path, archive: Path) -> dict:
    if archive.exists():
        raise ValueError("Refusing to overwrite an existing handoff archive")
    metadata = json.loads(metadata_json.read_text())
    for name, expected in metadata.items():
        path = output / "metadata" / f"{name}.csv"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            actual = list(csv.DictReader(stream))
        if len(actual) != len(expected):
            raise ValueError(f"Metadata CSV row loss: {name}")
        for a, e in zip(actual, expected):
            for key, value in e.items():
                target = "" if value is None else "true" if value is True else "false" if value is False else str(value)
                if a[key] != target:
                    raise ValueError(f"Metadata CSV value/type loss: {name}.{key}")
    required = ["START_HERE.md", "JOIN_GUIDE.md", "MEASURES.md", "verify_package.py"]
    if any(not (output / name).is_file() for name in required):
        raise ValueError("Missing handoff guide or verification entrypoint")
    files = {}
    private = re.compile(r"/Users/|/private/tmp/|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|Bearer\s+[A-Za-z0-9._-]{20,}")
    for path in sorted(output.rglob("*")):
        if path.is_symlink():
            raise ValueError("Package must not contain symlinks")
        if not path.is_file():
            continue
        relative = path.relative_to(output).as_posix()
        if relative == "manifest.json":
            continue
        if path.suffix in {".md", ".json", ".csv"} and private.search(path.read_text(encoding="utf-8-sig")):
            raise ValueError(f"Private path or secret pattern in package: {relative}")
        if path.suffix == ".parquet" or path.name.endswith(".csv.gz"):
            frame = read_table(path)
            for field in frame.select_dtypes(include=["object", "string"]):
                if frame[field].fillna("").astype(str).str.contains(private).any():
                    raise ValueError(f"Private field in processed payload: {relative}.{field}")
        files[relative] = dict(sha256=digest(path), bytes=path.stat().st_size)
    checks = json.loads((output / "provenance/handoff_checks.json").read_text())
    prepared = date.fromisoformat(checks.get("prepared_on", date.today().isoformat()))
    manifest = dict(schema_version=1, package_id=output.name, prepared_on=prepared.isoformat(),
                    source_release=checks["source_release"], status="internal_review_draft_not_sent",
                    raw_files_included=False, official_data_independently_verified=False,
                    redistribution_rights="not_assumed_confirm_before_external_distribution", files=files)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo(f"{output.name}/{path.relative_to(output).as_posix()}", (prepared.year, prepared.month, prepared.day, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                zipped.writestr(info, path.read_bytes())
    with zipfile.ZipFile(archive) as zipped:
        if zipped.testzip() is not None or len(zipped.namelist()) != len(files) + 1:
            raise ValueError("Archive CRC or file coverage failed")
        for relative, record in files.items():
            if hashlib.sha256(zipped.read(f"{output.name}/{relative}")).hexdigest() != record["sha256"]:
                raise ValueError("Archived payload changed")
    return dict(archive_bytes=archive.stat().st_size, archive_sha256=digest(archive), files=len(files) + 1,
                metadata_rows={name: len(rows) for name, rows in metadata.items()}, **checks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["prepare", "seal"])
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata-json", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--integrity-json", type=Path, help="New external archive-integrity sidecar (seal stage only)")
    parser.add_argument("--prepared-on", help="Package preparation date (YYYY-MM-DD), not source-data freshness")
    parser.add_argument("--include-production-history", action="store_true", help="Require and include existing monthly and annual production payloads")
    args = parser.parse_args()
    if args.integrity_json and (args.stage != "seal" or args.integrity_json.exists()):
        parser.error("Integrity sidecar requires seal stage and a new file path")
    result = prepare(args.project.resolve(), args.output.resolve(), args.metadata_json.resolve(),
                     include_production_history=args.include_production_history, prepared_on=args.prepared_on) if args.stage == "prepare" else seal(
        args.output.resolve(), args.metadata_json.resolve(), args.archive.resolve())
    if args.integrity_json:
        args.integrity_json.parent.mkdir(parents=True, exist_ok=True)
        args.integrity_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
