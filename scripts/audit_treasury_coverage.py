"""Audit every source catalogue entry without promoting it or inventing region codes.

Source territory labels are not a geographical endorsement or a harmonized
region dimension. No national totals are calculated from these entries.
"""
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile
import hashlib
import json
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from macro_rus.ingest_treasury import (
    _find_html_member, _parse_catalogue, _cutoff_from_path,
    _sha256_file, _decode_zip_name, TreasuryIngestionError, SOURCE_URL,
)
from macro_rus.treasury_fiscal import parse_fiscal


def territory_key(name):
    """Stable source-local key, explicitly not an official region identifier."""
    return "treasury-label-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def run(output):
    output = Path(output)
    if output.exists():
        raise FileExistsError("Use a fresh output directory to preserve prior audits")
    coverage, records, checks, sources = [], [], [], []
    paths = sorted([*ROOT.glob("KBSRF*.zip"), *ROOT.glob("КБСРФ*.zip")])
    if not paths:
        raise ValueError("No Treasury archives found")
    for path in paths:
        sha = _sha256_file(path)
        cutoff = _cutoff_from_path(path)
        with ZipFile(path) as outer:
            bad = outer.testzip()
            payload = outer.read(_find_html_member(outer))
        with ZipFile(BytesIO(payload)) as archive:
            bad_html = archive.testzip()
            if bad_html:
                raise ValueError(f"Corrupt selected HTML archive: {path.name}: {bad_html}")
            catalogue = _parse_catalogue(archive.read("TerrList_428m.html"))
            sources.append(dict(source_file=path.name, sha256=sha, catalogue_entries=len(catalogue),
                                ancillary_crc_failure=_decode_zip_name(bad) if bad else None))
            for name, member in catalogue.items():
                common = dict(territory_key=territory_key(name), source_territory_name_ru=name,
                              period=(cutoff - pd.Timedelta(days=1)).date().isoformat(),
                              reporting_cutoff=cutoff.date().isoformat(), source_file=path.name,
                              source_sha256=sha, source_member=member, source_url=SOURCE_URL,
                              units="RUB", frequency="YTD", budget_level="consolidated_regional_budget")
                try:
                    rows, events = parse_fiscal(archive.read(member))
                    failures = sum(e["blocking"] and not e["passed"] for e in events)
                    warnings = sum(not e["blocking"] and not e["passed"] for e in events)
                    records.extend({**common, **r} for r in rows)
                    checks.extend({**common, **e} for e in events)
                    coverage.append({**common, "status": "blocked" if failures else "accounting_passed",
                                     "observations": len(rows), "blocking_failures": failures,
                                     "plan_warnings": warnings, "error": ""})
                except (TreasuryIngestionError, KeyError, UnicodeError) as exc:
                    coverage.append({**common, "status": "parse_failed", "observations": 0,
                                     "blocking_failures": 1, "plan_warnings": 0, "error": str(exc)})
        print(f"Audited {path.name}: {len(catalogue)} entries", flush=True)
    coverage = pd.DataFrame(coverage)
    observations = pd.DataFrame(records)
    if coverage.duplicated(["territory_key", "period"]).any():
        raise ValueError("Multiple vintages at same territory-period require explicit selection")
    if observations.duplicated(["territory_key", "period", "kind", "metric_code"]).any():
        raise ValueError("Duplicate observation grain")
    output.mkdir(parents=True)
    coverage.to_csv(output / "coverage.csv", index=False)
    observations.to_csv(output / "staging_observations.csv", index=False)
    pd.DataFrame(checks).to_csv(output / "checks.csv", index=False)
    matrix = coverage.pivot(index="source_territory_name_ru", columns="period", values="status").fillna("absent")
    matrix.to_csv(output / "coverage_matrix.csv")
    summary = dict(status="audit_only_not_promoted", archives=len(paths),
                   source_territories=len(matrix), territory_periods=len(coverage),
                   observations=len(observations), status_counts=coverage.status.value_counts().to_dict(),
                   plan_warning_count=int(coverage.plan_warnings.sum()),
                   absent_territory_periods=int(matrix.eq("absent").sum().sum()), sources=sources,
                   limitations=["Source-local keys; official cross-source region concordance pending",
                                "No national sum: territory scope and nested-region overlaps require review",
                                "Accounting checks do not establish economic comparability",
                                "Snapshot plans are not original budgets; unresolved plan gaps retained",
                                "Blocked observations retained for audit, never promoted"])
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in summary.items() if k != "sources"}, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else ROOT / "outputs/treasury_coverage_2026-09-29")
