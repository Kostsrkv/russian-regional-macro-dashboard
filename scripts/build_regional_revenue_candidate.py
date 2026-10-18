"""Build regional revenue coverage from existing archives; no network reads."""
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile
from decimal import Decimal
import json
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from macro_rus.ingest_treasury import _find_html_member, _parse_catalogue, _cutoff_from_path, SOURCE_URL
from macro_rus.treasury_revenue import parse_revenue
from macro_rus.provenance import sha256_file
from macro_rus.contracts import BUDGET_COLUMNS, SOURCE_COLUMNS
from macro_rus.regions import REGION_RECORDS
from macro_rus.validation import validate_tables


def run(output):
    output = Path(output)
    if output.exists():
        raise FileExistsError("Choose a new output directory; prior candidates are immutable")
    fiscal_dir = ROOT / "outputs/regional_concordance_2026-09-29_v2/fiscal_preview"
    fiscal_manifest = json.loads((fiscal_dir / "manifest.json").read_text())
    fiscal = pd.read_csv(fiscal_dir / "fiscal_observations.csv", dtype={"metric_code": str})
    crosswalk_path = ROOT / "outputs/regional_concordance_2026-09-29_v2/region_concordance.csv"
    crosswalk = pd.read_csv(crosswalk_path, dtype={"fns_code": str})
    regions = crosswalk[crosswalk.eligible_for_candidate_profile.eq(True)]
    english = {r["region_id"]: r["region_name_en"] for r in REGION_RECORDS}
    observations, lineage, events, sources, archive_checks = [], [], [], [], []
    for source in fiscal_manifest["sources"]:
        path = ROOT / source["source_file"]
        digest = sha256_file(path)
        if digest != source["sha256"] or not source["selected_html_crc_passed"]:
            raise ValueError("Archive differs from the fiscal audit; audit it before use")
        cutoff = _cutoff_from_path(path)
        period = cutoff - pd.Timedelta(days=1)
        source_id = f"treasury_kbsrf_0503317_{cutoff:%Y%m%d}_{digest[:12]}"
        vintage = f"kbsrf-0503317-{cutoff:%Y%m%d}-{digest[:12]}"
        with ZipFile(path) as outer:
            payload = outer.read(_find_html_member(outer))
        with ZipFile(BytesIO(payload)) as reports:
            if reports.testzip():
                raise ValueError("Selected HTML archive failed CRC")
            catalogue = _parse_catalogue(reports.read("TerrList_428m.html"))
            for region in regions.to_dict("records"):
                region_id, name = region["region_id"], region["treasury_name_ru"]
                member = catalogue[name]
                rows, checks = parse_revenue(reports.read(member))
                common = dict(region_id=region_id, period=period, source_id=source_id, source_vintage=vintage)
                events.extend({**common, **e} for e in checks)
                if any(e["blocking"] and not e["passed"] for e in checks):
                    raise ValueError(f"Revenue components fail reconciliation: {name}, {period}")
                fiscal_total = fiscal.loc[fiscal.region_id.eq(region_id) & fiscal.period.eq(period.date().isoformat())
                                         & fiscal.metric_code.eq("revenue_total"), "actual_ytd_rub"]
                reported = next(r["actual_ytd_rub"] for r in rows if r["revenue_code"] == "TOTAL_REVENUE")
                if len(fiscal_total) != 1 or abs(reported - Decimal(str(fiscal_total.item()))) > Decimal("0.02"):
                    raise ValueError("Revenue differs from the fiscal profile total")
                for row in rows:
                    observations.append({**common, "region_name_en": english.get(region_id, ""),
                        "region_name_ru": name, "frequency": "quarterly",
                        "budget_level": "consolidated_regional_budget",
                        **{k: row[k] for k in ("revenue_code", "revenue_name_en", "revenue_name_ru")},
                        "actual_ytd_rub": float(row["actual_ytd_rub"]),
                        "approved_plan_rub": float(row["approved_plan_rub"]),
                        "plan_components_reconciled": all(e["passed"] for e in checks),
                        "revised_plan_rub": None, "revision_status": "latest",
                        "quality_status": "provisional", "is_official": True})
                    lineage.append({**common, **row, "source_file": path.name, "source_sha256": digest,
                                    "source_member": member, "reporting_cutoff": cutoff,
                                    "amount_basis": "YTD", "units": "RUB"})
        sources.append(dict(source_id=source_id, publisher="Federal Treasury of Russia",
            dataset_name="KBSRF Form 0503317 regional budget revenue", source_url=SOURCE_URL,
            local_path=path.name, retrieved_at_utc=None, reporting_cutoff=cutoff,
            publication_date=None, units="RUB", sha256=digest, file_size_bytes=path.stat().st_size,
            is_official=True, notes="Regional + municipal consolidated budget; excludes territorial health fund. "
            "Actual amounts are YTD. Approved plan is the assignment at cutoff; original and amended plans are not distinguished. "
            "202 group measures transfers from other budgets; 200 group includes other non-repayable receipts and repayments."))
        archive_checks.append({**source, "selected_html_crc_passed": True})
        print(f"Extracted revenue: {path.name}", flush=True)
    budget = pd.DataFrame(observations, columns=[*BUDGET_COLUMNS, "plan_components_reconciled"])
    source_frame = pd.DataFrame(sources, columns=SOURCE_COLUMNS)
    validation = validate_tables({"budget_execution": budget, "sources": source_frame})
    if not validation.is_valid or budget.duplicated(["region_id", "period", "revenue_code"]).any():
        raise ValueError("Canonical revenue validation failed")
    # Prior pilot output is an independent pipeline regression control.
    old = pd.read_parquet(ROOT / "data/promoted/current/budget_execution.parquet")
    key = ["region_id", "period", "revenue_code", "source_vintage"]
    parity = old.merge(budget, on=key, suffixes=("_old", "_new"), validate="one_to_one")
    if len(parity) != len(old) or not all((parity[c + "_old"] - parity[c + "_new"]).abs().le(.02).all()
                                         for c in ("actual_ytd_rub", "approved_plan_rub")):
        raise ValueError("Pilot revenue regression")
    output.mkdir(parents=True)
    budget.to_parquet(output / "budget_execution.parquet", index=False)
    source_frame.to_csv(output / "sources.csv", index=False)
    pd.DataFrame(lineage).to_csv(output / "lineage.csv", index=False)
    pd.DataFrame(events).to_csv(output / "accounting_checks.csv", index=False)
    validation.events.to_csv(output / "validation_checks.csv", index=False)
    coverage = budget.groupby(["region_id", "region_name_ru", "revenue_code"]).agg(
        observations=("period", "size"), earliest=("period", "min"), latest=("period", "max")).reset_index()
    coverage.to_csv(output / "coverage.csv", index=False)
    manifest = dict(status="candidate_not_promoted", rows=len(budget), eligible_regions=budget.region_id.nunique(),
        region_periods=len(budget[["region_id", "period"]].drop_duplicates()),
        periods=sorted(budget.period.dt.strftime("%Y-%m-%d").unique().tolist()),
        blocking_failures=0, accounting_checks=len(events), pilot_parity_rows=len(parity),
        plan_warning_count=sum(not e["blocking"] and not e["passed"] for e in events),
        sources=archive_checks, concordance_sha256=sha256_file(crosswalk_path),
        derived_hashes={p.name: sha256_file(p) for p in output.iterdir()},
        limitations=["Review candidate, not promoted", "Actuals are cumulative within each year",
                     "Snapshot plans do not identify original or amended budget histories",
                     "Transfers from other budgets are not necessarily exclusively federal",
                     "No national totals; overlap and separate-geography holds remain",
                     "Missing inflation, population and debt stock inputs remain unavailable"])
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in manifest.items() if k not in ("sources", "derived_hashes")}, ensure_ascii=False, indent=2))
    return manifest


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else ROOT / "outputs/regional_revenue_candidate_2026-09-30")
