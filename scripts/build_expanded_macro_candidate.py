"""Stage expanded tax/production series, retaining existing pilots as controls.

This does not promote data or enable new macro pages in the running dashboard.
"""
from pathlib import Path
import sys
import json
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from macro_rus.ingest_fns import ingest_fns_result
from macro_rus.ingest_rosstat import ingest_rosstat_result
from macro_rus.validation import validate_table
from macro_rus.regions import REGION_RECORDS


def run(output):
    output = Path(output)
    if output.exists():
        raise FileExistsError("Use a fresh candidate directory")
    concordance = ROOT / "outputs/regional_concordance_2026-09-29_v2/region_concordance.csv"
    crosswalk = pd.read_csv(concordance, dtype={"fns_code": str}).fillna("")
    records = crosswalk[crosswalk.eligible_for_candidate_profile.eq(True)].to_dict("records")
    pilots = {r["region_id"]: r for r in REGION_RECORDS}
    for record in records:
        record["region_name_ru"] = record["treasury_name_ru"]
        record["region_name_en"] = pilots.get(record["region_id"], {}).get("region_name_en", "")
    fns = ingest_fns_result(ROOT, region_records=records)
    rosstat = ingest_rosstat_result(ROOT, region_records=records)
    tables = {"pit_receipts": fns.pit_receipts, "industrial_production": rosstat.industrial_production}
    checks = [e for name, frame in tables.items() for e in validate_table(name, frame)]
    failures = [e for e in checks if e["severity"] in ("critical", "high") and e["status"] == "failed"]
    if failures:
        raise ValueError(f"Candidate blocked: {failures}")
    parity = {}
    for name, frame in tables.items():
        old = pd.read_parquet(ROOT / "data/promoted/current" / f"{name}.parquet")
        new = frame[frame.region_id.isin(pilots)]
        key = ["region_id", "period", "okved_section", "source_vintage"]
        value = "pit_ytd_rub" if name == "pit_receipts" else "index_value"
        joined = old.merge(new, on=key, how="outer", suffixes=("_old", "_new"), indicator=True, validate="one_to_one")
        good = joined["_merge"].eq("both") & (joined[value + "_old"] - joined[value + "_new"]).abs().le(.02)
        parity[name] = {"compared": len(joined), "mismatches": int((~good).sum())}
        if not good.all():
            raise ValueError(f"Pilot regression: {name}: {parity[name]}")
    output.mkdir(parents=True)
    coverage = []
    for name, frame in tables.items():
        # This broader candidate has not had a full semantic/source reconciliation.
        frame = frame.copy()
        frame["quality_status"] = "warning"
        frame.to_parquet(output / f"{name}.parquet", index=False)
        for record in records:
            subset = frame[frame.region_id.eq(record["region_id"]) ]
            coverage.append(dict(table=name, region_id=record["region_id"], region_name_ru=record["region_name_ru"],
                                 observations=len(subset), periods=subset.period.nunique(),
                                 earliest=subset.period.min(), latest=subset.period.max(),
                                 missing_flow=int(subset.pit_flow_rub.isna().sum()) if name == "pit_receipts" else None))
    sources = pd.concat([fns.source_metadata, rosstat.source_metadata], ignore_index=True)
    sources["local_path"] = sources.local_path.map(lambda p: Path(p).name)
    sources.to_csv(output / "sources.csv", index=False)
    section_coverage = fns.pit_receipts.groupby(["period", "okved_section"]).region_id.nunique().unstack()
    section_coverage.to_csv(output / "pit_section_coverage.csv")
    pd.DataFrame(coverage).to_csv(output / "coverage.csv", index=False)
    pd.DataFrame(checks).to_csv(output / "validation_checks.csv", index=False)
    summary = dict(status="candidate_not_promoted", eligible_regions=len(records), pilot_parity=parity,
                   tables={name: {"rows": len(frame), "regions": frame.region_id.nunique(),
                                  "periods": frame.period.nunique(), "earliest": str(frame.period.min()),
                                  "latest": str(frame.period.max())} for name, frame in tables.items()},
                   skipped_fns=list(fns.skipped_files), skipped_rosstat=list(rosstat.skipped_files),
                   limitations=["Candidate warning status: no full regional PIT-total reconciliation yet",
                                "Mining B is absent from the 20260101 and 20260401 schemas; aggregate YoY cannot compare these with 20250401 coverage",
                                "Missing historical schemas remain excluded; no interpolated flows",
                                "CPI and per-capita denominators not integrated",
                                "Latest-name concordance may not resolve older alternative names",
                                "Industrial indices and PIT need matched periods before cross-checking",
                                "No deployment or promotion"])
    (output / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in summary.items() if not k.startswith("skipped")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else ROOT / "outputs/expanded_macro_candidate_2026-09-29")
