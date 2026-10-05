"""Extend the pinned public snapshot with reviewed, raw-free fuel observations."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

import pandas as pd
from pandas.testing import assert_frame_equal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from build_cloud_release import (
    _copy_processed, _load_envelopes, _sanitize_json, _table_summary,
    validate_cloud_release,
)
from macro_rus.fuel_prices import DATASET, MEASURE, load_fuel_candidate
from macro_rus.provenance import sha256_file

RELEASE_ID = "2026-10-05"
RELEASE_DIRECTORY = RELEASE_ID + "-cloud-v1"
BASE_RELEASE = "data/dashboard/releases/2026-10-02-cloud-v1"
PRIVATE_FUEL = "outputs/new_prices_audit_2026-10-05"
SUMMARY_FIELDS = (
    "audit_executed_at_utc", "status", "period_start", "period_end", "months",
    "existing_dashboard_eligible_regions", "eligible_all_fuel_observations_expected",
    "eligible_all_fuel_observations_missing", "eligible_core_fuel_observations_expected",
    "eligible_core_fuel_observations_missing", "eligible_core_fuel_observations_nonpositive",
    "independent_raw_cell_checks", "independent_raw_cell_checks_failed",
    "matched_month_on_month_changes", "month_on_month_review_flags_abs_above10pct",
)


def build_fuel_release(workspace: Path = ROOT, output: Path | None = None) -> dict:
    workspace = Path(workspace)
    output = output or workspace / "data/dashboard/releases" / RELEASE_DIRECTORY
    if output.exists():
        raise FileExistsError("Release paths are immutable: choose a fresh destination")
    base = workspace / BASE_RELEASE
    old = validate_cloud_release(base)
    original = _load_envelopes(base)
    ids = original["pit_receipts"].region_id.unique()
    private = workspace / PRIVATE_FUEL
    fuel, audit = load_fuel_candidate(private, eligible_region_ids=ids)
    sources = [dict(filename=source["filename"], sha256=source["sha256"], role=source["role"])
               for source in audit["sources"] if source["filename"] in set(fuel.source_file)]
    if len(sources) != fuel.source_file.nunique():
        raise ValueError("Fuel publication source census mismatch")
    envelope = _sanitize_json({
        "schema_version": 1, "status": "candidate_not_promoted",
        "distribution_mode": "cloud_processed_fuel_v1", "raw_files_included": False,
        "dataset": DATASET, "measurement_basis": MEASURE, "units": "RUB/litre",
        "observation_timing": "end_of_reporting_month", "sources": sources,
        "source_publication_date": None, "source_retrieval_date": None,
        "source_authenticity": "not_independently_verified",
        "audit_summary": {field: audit[field] for field in SUMMARY_FIELDS},
        "limitations": [
            "Publication does not promote observations to independently verified data.",
            "Spatial-average end-of-month fuel prices; not official CPI or daily pump quotes.",
            "Source publication/retrieval dates and URLs were not recorded.",
            "Large monthly changes remain flagged; their economic explanation is not verified.",
            "Optional missing grades remain missing; no national average is calculated.",
        ],
    })
    # The existing public datasets are preserved byte-for-byte, not regenerated.
    shutil.copytree(base, output)
    target = output / "fuel"
    target.mkdir()
    _copy_processed(private / DATASET, target / DATASET)
    envelope["dataset_sha256"] = sha256_file(target / DATASET)
    (target / "manifest.json").write_text(json.dumps(envelope, ensure_ascii=False, indent=2))
    published, _ = load_fuel_candidate(target, eligible_region_ids=ids)
    assert_frame_equal(fuel, published, check_exact=True)
    public = _load_envelopes(output)
    for name in original:
        assert_frame_equal(original[name], public[name], check_exact=True)
    payloads = dict(old["payloads"])
    for filename in (DATASET, "manifest.json"):
        path = target / filename
        payloads[f"fuel/{filename}"] = {
            "sha256": sha256_file(path), "bytes": path.stat().st_size,
            "role": "candidate_manifest" if filename == "manifest.json" else "processed_aggregate_or_provenance",
        }
    manifest = dict(old, release_id=RELEASE_ID,
                    created_at_utc=datetime.now(timezone.utc).isoformat(),
                    previous_release_id=old["release_id"],
                    previous_manifest_sha256=sha256_file(base / "manifest.json"),
                    runtime_directories={**old["runtime_directories"], "fuel": "fuel"},
                    payloads=payloads,
                    tables={name: _table_summary(frame) for name, frame in public.items()},
                    limitations=[text.replace("wages, fuel prices, debt", "wages, debt")
                                 for text in old["limitations"]] + envelope["limitations"])
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return validate_cloud_release(output, release_id=RELEASE_ID)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--select", action="store_true", help="Select the validated new public snapshot")
    args = parser.parse_args()
    result = build_fuel_release()
    release = ROOT / "data/dashboard/releases" / RELEASE_DIRECTORY
    if args.select:
        pointer = dict(schema_version=1, release_id=RELEASE_ID,
                       release_path=f"releases/{RELEASE_DIRECTORY}",
                       manifest_sha256=sha256_file(release / "manifest.json"))
        (ROOT / "data/dashboard/current.json").write_text(json.dumps(pointer, indent=2) + "\n")
    print(json.dumps({"release_id": result["release_id"], "files": len(result["payloads"]),
                      "payload_bytes": sum(item["bytes"] for item in result["payloads"].values()),
                      "fuel_rows": result["tables"]["fuel_prices"]["rows"]}, indent=2))


if __name__ == "__main__":
    main()
