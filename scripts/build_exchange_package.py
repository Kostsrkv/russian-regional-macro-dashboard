"""Create a local, versioned research exchange package from review candidates.

No raw files, credentials, user filesystem paths or external sharing. The
package remains a review candidate and includes the qualifications needed to
interpret its CSVs outside Streamlit.
"""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import json
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from macro_rus.macro_preview import load_macro_candidate
from macro_rus.revenue_view import load_revenue_candidate, revenue_indicators
from macro_rus.fiscal_view import load_candidate
from macro_rus.provenance import sha256_file

FIELDS = {
    "region_id": "Project region identifier; FNS-nn is based on the FNS code, not an ISO identifier.",
    "region_name_ru": "Original Russian regional name retained by the project concordance.",
    "region_name_en": "English name where supplied; empty when unavailable.",
    "period": "Observation period end. Always interpret together with frequency and amount/index basis.",
    "frequency": "Source reporting interval. Quarterly budget records contain YTD amounts, not quarter flows.",
    "reporting_cutoff": "Source reporting cutoff; for these snapshots observation period ends the previous day.",
    "pit_ytd_rub": "FNS reported cumulative industry PIT, nominal RUB.",
    "pit_flow_rub": "FNS derived period PIT, nominal RUB. Missing where adjacent snapshots are unavailable.",
    "cpi_index": "CPI adjustment input; unavailable in this release.",
    "okved_raw": "Industry classification as retained by the FNS adapter.",
    "okved_section": "OKVED2 section; TOTAL means a published industrial headline, not a sum of sector indices.",
    "index_measure": "Exact industrial index comparison basis, e.g. same month in previous year = 100.",
    "index_value": "Published industrial production index in percent of its stated comparison basis.",
    "index_base": "Production series basket/base description retained by the adapter; source-native measures need further historical review.",
    "actual_ytd_rub": "Treasury cumulative actual receipts/expenditure/financing within the calendar year, nominal RUB.",
    "approved_plan_rub": "Approved assignment in the revenue snapshot. Does not establish the original budget.",
    "revised_plan_rub": "Separately documented amended plan; unavailable in this release.",
    "plan_at_cutoff_rub": "Published fiscal snapshot assignment, nominal RUB; plan-balance discrepancies retained.",
    "plan_components_reconciled": "Whether revenue plan total equals tax/non-tax plus non-repayable receipts within 0.02 RUB.",
    "prior_year_ytd_rub": "Actual revenue for exactly the same period one year earlier, if observed.",
    "change_yoy_rub": "Actual YTD minus same-period prior-year actual, nominal RUB; missing when prior absent.",
    "growth_yoy_pct": "100*(actual/prior-year actual-1), only with a positive observed denominator.",
    "prior_quarter_ytd_rub": "Observed immediately preceding quarter-end YTD revenue used for differencing.",
    "quarter_flow_rub": "Q1 YTD or difference of adjacent same-year YTD snapshots; signed adjustments retained.",
    "flow_status": "How a quarterly revenue flow was derived or why it is missing.",
    "snapshot_execution_pct": "Actual/snapshot plan*100, withheld for nonpositive plans or unreconciled plan components.",
    "budget_level": "Consolidated regional plus municipal budgets, excluding territorial health-insurance fund.",
    "revenue_code": "Parent budget revenue classification; TOTAL_REVENUE is a project code for published total.",
    "metric_code": "Fiscal summary, expenditure function or financing component identifier; interpret with kind.",
    "kind": "Fiscal row role: summary, expenditure_function or financing_component. These cannot be summed together.",
    "units": "Measure units. Source FNS thousands were converted to RUB; Treasury figures are already RUB.",
    "amount_basis": "YTD identifies cumulative within-year amounts.",
    "source_id": "Join key to sources.csv; use source_vintage and provenance too.",
    "source_vintage": "Source release identifier with cutoff and hash-derived suffix.",
    "source_file": "Original raw source basename only; raw content is excluded.",
    "source_member": "HTML report member within the source Treasury archive.",
    "source_sha256": "SHA-256 of original raw Treasury archive.",
    "source_row_index": "Zero-based parsed HTML table row position, not an Excel row.",
    "actual_column_index": "Zero-based HTML column selected by consolidated-budget header.",
    "plan_column_index": "Zero-based HTML column selected by consolidated-budget plan header.",
    "quality_status": "Review status; warning/provisional does not mean independent economic verification.",
    "revision_status": "Adapter-selected release state; raw source vintages remain immutable.",
    "is_official": "Original publisher is an official source. Does not establish completeness or comparability.",
    "eligible_for_national_aggregation": "False throughout; geographical overlap and boundary treatment are unresolved.",
}


def run(output):
    output = Path(output)
    if output.exists() or output.with_suffix(".zip").exists():
        raise FileExistsError("Choose a fresh versioned exchange directory")
    macro, macro_manifest = load_macro_candidate(ROOT / "outputs/expanded_macro_candidate_2026-09-29_v2")
    revenue, revenue_manifest = load_revenue_candidate(ROOT / "outputs/regional_revenue_candidate_2026-09-30")
    fiscal, fiscal_manifest = load_candidate(ROOT / "outputs/regional_concordance_2026-09-29_v2/fiscal_preview")
    ids = set(macro["pit_receipts"].region_id)
    if ids != set(revenue["budget_execution"].region_id) or ids != set(fiscal.region_id):
        raise ValueError("Exchange tables have different regional coverage")
    indicators = revenue_indicators(revenue["budget_execution"])
    if not indicators.quarter_flow_rub.notna().all():
        raise ValueError("Expected adjacent Q1/H1 revenue snapshots in this release")
    sources = pd.concat([macro["sources"], revenue["sources"]], ignore_index=True).drop_duplicates("source_id", keep="last")
    sources["local_path"] = sources.local_path.map(lambda p: Path(p).name)
    crosswalk = pd.read_csv(ROOT / "outputs/regional_concordance_2026-09-29_v2/region_concordance.csv", dtype={"fns_code": str})
    tables = {"pit_receipts": macro["pit_receipts"], "industrial_production": macro["industrial_production"],
              "budget_revenue": indicators, "fiscal_observations": fiscal,
              "revenue_lineage": revenue["revenue_lineage"], "sources": sources,
              "regions": crosswalk.loc[crosswalk.eligible_for_candidate_profile.eq(True)].copy()}
    # Foreign-key coverage is verified before files are made available.
    for name in ("pit_receipts", "industrial_production", "budget_revenue"):
        if not tables[name].source_id.isin(sources.source_id).all():
            raise ValueError("Orphan exchange source")
    output.mkdir(parents=True)
    for name, frame in tables.items():
        frame.to_csv(output / f"{name}.csv", index=False, encoding="utf-8-sig")
    dictionary = []
    for table, frame in tables.items():
        for column in frame:
            description = FIELDS.get(column)
            if description is None:
                description = {"sha256": "SHA-256 of original source file.",
                    "retrieved_at_utc": "Source retrieval time if documented; missing when unknown.",
                    "publication_date": "Publication date retained by adapter; filename-derived FNS dates require review.",
                    "source_url": "Official dataset landing page.", "local_path": "Source basename only.",
                    "file_size_bytes": "Original source file size in bytes.",
                    "publisher": "Original source publisher.", "dataset_name": "Source dataset title.",
                    "notes": "Source qualifications and extraction notes.",
                    "industry_name_en": "English industry label supplied by the project.",
                    "industry_name_ru": "Russian industry label retained from source schema.",
                    "revenue_name_en": "English revenue parent label supplied by the project.",
                    "revenue_name_ru": "Original Russian revenue label.",
                    "name_ru": "Russian fiscal row label.",
                    "source_line_code": "Treasury row code; classification details are separate.",
                    "territory_key": "Source-label hash key, not an official identifier.",
                    "treasury_name_ru": "Treasury regional label used in concordance.",
                    "fns_code": "FNS region code stored as text with leading zero.",
                    "fns_name_ru": "FNS regional label.",
                    "fns_csv_row": "One-based CSV row number including header.",
                    "rosstat_name_ru": "Matched Rosstat row label.",
                    "rosstat_match_method": "Explicit source alias or typography normalization.",
                    "matched_all_five_production_sheets": "Region label matched across all five selected industrial sheets.",
                    "numeric_values_in_all_five_production_sheets": "At least one numeric observation exists on each selected sheet; not full temporal coverage.",
                    "status": "Concordance review classification.",
                    "eligible_for_candidate_profile": "Region is eligible for the local review preview."}.get(column)
            if description is None:
                raise ValueError(f"Data dictionary missing definition: {table}.{column}")
            dictionary.append(dict(table=table, field=column, dtype=str(frame[column].dtype), definition=description,
                                   missing_values=int(frame[column].isna().sum())))
    pd.DataFrame(dictionary).to_csv(output / "data_dictionary.csv", index=False, encoding="utf-8-sig")
    coverage = []
    for name in ("pit_receipts", "industrial_production", "budget_revenue", "fiscal_observations"):
        for region, group in tables[name].groupby("region_id"):
            dates = pd.to_datetime(group.period)
            coverage.append(dict(table=name, region_id=region, rows=len(group), periods=dates.nunique(),
                                 earliest=dates.min(), latest=dates.max(), status="candidate_review"))
    pd.DataFrame(coverage).to_csv(output / "coverage.csv", index=False, encoding="utf-8-sig")
    pd.read_csv(ROOT / "outputs/regional_revenue_candidate_2026-09-30/accounting_checks.csv").to_csv(
        output / "revenue_accounting_checks.csv", index=False, encoding="utf-8-sig")
    notes = """# Regional economy research exchange — review candidate

78 eligible regions. Local analytical preparation; not independently verified or promoted.
This package is prepared for review and has not been sent to any collaborator.
Raw sources are excluded; original basenames, official source pages and hashes are retained.

## Reading the files
PIT covers 31 March 2025, 31 December 2025 and 31 March 2026. Mining B is absent
from the newer FNS schemas. Industry sums are reported coverage, not reconciled
regional PIT totals. Aggregate growth across changed coverage must be withheld.
Missing annual-to-quarter flows remain missing; annual YTD is not Q4.

Production is source-native monthly indices spanning January 2015–July 2026.
The index_measure column defines the comparison. Monthly YoY percentages must
not be averaged to invent quarterly output growth. Missing sector cells are omitted.

Budget revenues and fiscal observations cover Q1 and first-half YTD 2023–2026.
Actuals are nominal RUB. quarter_flow_rub is Q1 YTD or H1 minus Q1 within the same
year. Negative adjustments remain. A financing flow is not a debt stock.
Intergovernmental transfers (202) are part of broader non-repayable receipts (200).
Taxes are part of tax/non-tax receipts (100). Never sum these overlapping rows.

Snapshot plan assignments do not identify original budgets or later amendments.
Execution percentages are withheld for unreconciled plan components. Other
fiscal plan-balance discrepancies also remain unresolved. No fiscal-shortfall
verdict is produced. CPI, per-capita figures and debt stocks are unavailable.

## Regional identifiers and evidence
FNS-nn identifiers are project keys, not ISO geography codes. Leading zeros in
fns_code must be preserved when importing CSVs. The five overlapping scopes and
eight separately treated territories are excluded. No national aggregate is authorized.
Original Russian names are retained; missing English names remain blank.
Use sources.csv, revenue_lineage.csv, data_dictionary.csv and coverage.csv together.
Unknown retrieval/publication dates remain missing or carry adapter qualifications.
Official-source status does not imply complete coverage or causal support.
Observed revenue and output changes do not automatically establish sanctions effects.

## Reproduction
From the project, run scripts/build_regional_revenue_candidate.py into a fresh
output directory and scripts/build_exchange_package.py into a fresh directory.
The latter currently selects the dated candidate folders recorded in manifest.json.
The companion revenue_audit.ipynb checks accounting, source lineage and flow derivation.
Before external sharing, agree definitions, permitted redistribution and any required scope with the recipient.
"""
    (output / "README.md").write_text(notes)
    manifest = dict(status="candidate_review_not_promoted", eligible_regions=len(ids),
        tables={name: {"rows": len(frame)} for name, frame in tables.items()},
        inputs={"macro": "expanded_macro_candidate_2026-09-29_v2", "revenue": "regional_revenue_candidate_2026-09-30",
                "fiscal": "regional_concordance_2026-09-29_v2/fiscal_preview"},
        revenue_plan_warnings=revenue_manifest["plan_warning_count"], raw_files_included=False,
        shared_externally=False, files={p.name: sha256_file(p) for p in output.iterdir()})
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    with ZipFile(output.with_suffix(".zip"), "w", ZIP_DEFLATED) as archive:
        for path in sorted(output.iterdir()):
            archive.write(path, arcname=output.name + "/" + path.name)
    print(json.dumps({"folder": str(output), "zip": str(output.with_suffix('.zip')), "regions": len(ids),
                      "tables": manifest["tables"]}, ensure_ascii=False, indent=2))
    return manifest


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else ROOT / "outputs/research_exchange_2026-09-30")
