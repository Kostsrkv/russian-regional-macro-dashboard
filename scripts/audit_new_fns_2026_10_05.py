"""Read-only source audit of the 5 October FNS downloads.

Raw files and established pipeline outputs are never changed. Generated reports
remain private candidate evidence, not promoted data or verified PIT receipts.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
import re
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from macro_rus.ingest_fns import _pit_fields

OUT = ROOT / "outputs/new_fns_audit_2026-10-05"
SCHEMAS = ["structure-20250101.csv", "structure-20250701.csv", "structure-20251001.csv"]
DATA = [
    "data-20250101-structure-20250101 copy 3.csv",
    "data-20250101-structure-20250101.csv",
    "data-20250101-structure-20250101 (1).csv",
    "data-20250701-structure-20250701.csv",
    "data-20250701-structure-20250701 (1).csv",
    "data-20251001-structure-20251001.csv",
    "data-20251001-structure-20251001 (1).csv",
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def profile(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        first = stream.readline()
        stream.seek(0)
        sep = ";" if first.count(";") > first.count(",") else ","
        reader = csv.reader(stream, delimiter=sep)
        header = next(reader)
        rows = list(reader)
    widths = Counter(len(row) for row in rows)
    aligned = [row for row in rows if len(row) == len(header)]
    codes = [row[0].strip().lstrip("0") or "0" for row in rows if len(row) >= 2]
    names = [row[1].strip() for row in rows if len(row) >= 2]
    keys = Counter(zip(codes, names))
    out = {
        "file": path.name, "sha256": digest(path), "file_bytes": path.stat().st_size,
        "encoding": "utf-8-sig (strict decode)", "delimiter": sep,
        "columns": len(header), "header_first": header[:4], "header_last": header[-4:],
        "rows": len(rows), "row_width_counts": dict(sorted(widths.items())),
        "malformed_width_rows": len(rows) - len(aligned),
        "unique_region_codes": len(set(codes)), "unique_region_names": len(set(names)),
        "duplicate_region_keys": sum(n - 1 for n in keys.values() if n > 1),
        "exact_duplicate_rows": sum(n - 1 for n in Counter(map(tuple, rows)).values() if n > 1),
    }
    if header[:2] == ["GA", "GB"]:
        out["duplicate_region_keys_detail"] = [{"code": code, "name": name, "extra_rows": n - 1} for (code, name), n in keys.items() if n > 1]
        by_key = {}
        for row in aligned:
            by_key.setdefault((row[0].strip(), row[1].strip()), []).append(tuple(row[2:]))
        out["conflicting_aligned_region_keys"] = sum(len(set(records)) > 1 for records in by_key.values())
        with path.open(encoding="utf-8-sig", newline="") as stream:
            physical_lines = stream.readlines()
        comma_fragments = [next(csv.reader([line])) for line in physical_lines[1:] if sep == ";" and ',' in line and ';' not in line]
        out["alternate_comma_records"] = len(comma_fragments)
        out["alternate_comma_width_counts"] = dict(Counter(map(len, comma_fragments)))
        out["aligned_distinct_region_codes"] = len({row[0].strip().lstrip("0") or "0" for row in aligned})
    if aligned and header[:2] == ["GA", "GB"]:
        raw = pd.DataFrame(aligned, columns=header)
        values = raw.iloc[:, 2:]
        normalised = values.apply(lambda col: col.astype(str).str.replace(r"[\s\u00a0\u202f]", "", regex=True))
        numeric = normalised.apply(pd.to_numeric, errors="coerce")
        out.update({
            "aligned_numeric_cells": int(numeric.size), "blank_numeric_cells": int(values.eq("").sum().sum()),
            "numeric_parse_failures": int((numeric.isna() & values.ne("")).sum().sum()),
            "space_grouped_numeric_cells": int(values.apply(lambda col: col.str.contains(r"\d\s\d", regex=True)).sum().sum()),
            "negative_numeric_cells": int(numeric.lt(0).sum().sum()),
            "zero_numeric_cells": int(numeric.eq(0).sum().sum()),
        })
    return out, header, rows


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    profiles = []
    schema_maps = {}
    for name in SCHEMAS:
        path = ROOT / name
        item, header, rows = profile(path)
        frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
        fields, unmapped = _pit_fields(frame)
        descriptions = frame["russian description"].fillna("")
        item.update({
            "role": "schema", "described_fields": len(frame),
            "duplicate_field_names": int(frame["field name"].duplicated().sum()),
            "pit_tax_measure_descriptions": int(descriptions.str.contains("налог на доходы физических лиц", case=False).sum()),
            "pit_section_fields": fields, "pit_unmapped_fields": unmapped,
            "mining_name_fields": frame.loc[descriptions.str.contains("Добыча полезных ископаемых", case=False), "field name"].tolist(),
            "measure_identity": "Not established: schemas describe industries, not a PIT/tax-type measure.",
            "pit_ingestion_status": "blocked_no_pit_fields",
        })
        schema_maps[name] = set(frame["field name"])
        profiles.append(item)
    data_records = {}
    for name in DATA:
        path = ROOT / name
        if not path.exists():
            continue
        item, header, rows = profile(path)
        schema_date = re.search(r"structure-(\d{8})", name).group(1)
        schema_name = f"structure-{schema_date}.csv"
        declared = schema_maps[schema_name]
        item.update({
            "role": "data", "declared_schema": schema_name,
            "data_header_fields_not_in_schema": len(set(header) - declared),
            "schema_fields_absent_from_data_header": len(declared - set(header)),
            "header_schema_field_set_equal": set(header) == declared,
            "safe_for_pit": False,
        })
        data_records[name] = (header, rows)
        profiles.append(item)
    # No unknown measure is promoted. Region matching is a grain/coverage check only.
    concordance = pd.read_csv(ROOT / "outputs/regional_concordance_2026-09-29_v2/region_concordance.csv", dtype={"fns_code": str})
    eligible = concordance.loc[concordance.eligible_for_candidate_profile.eq(True)]
    required = {str(value).strip().lstrip("0") or "0" for value in eligible.fns_code}
    small = DATA[0]
    header, rows = data_records[small]
    found = {row[0].strip().lstrip("0") or "0" for row in rows if len(row) == len(header)}
    small_item = next(item for item in profiles if item["file"] == small)
    small_item.update({
        "eligible_regions_expected": len(required), "eligible_regions_present": len(required & found),
        "eligible_region_codes_missing": sorted(required - found),
        "additional_region_codes_not_eligible": sorted(found - required),
    })
    # Fingerprint numeric columns only across unambiguous unique region codes.
    # Matching values are evidence of a projection, not a tax-type definition.
    wide_name = "data-20250101-structure-20250101.csv"
    wide_header, wide_rows = data_records[wide_name]
    def by_code(source_header, source_rows):
        grouped = {}
        for row in source_rows:
            if len(row) == len(source_header):
                code = row[0].strip().lstrip("0") or "0"
                grouped.setdefault(code, []).append(row)
        return {code: records[0] for code, records in grouped.items() if len(records) == 1}
    narrow_codes = by_code(header, rows)
    wide_codes = by_code(wide_header, wide_rows)
    small_item["eligible_regions_with_unambiguous_single_aligned_row"] = len(required & set(narrow_codes))
    small_item["eligible_region_codes_with_conflicting_rows"] = sorted((required & found) - set(narrow_codes))
    common = sorted(set(narrow_codes) & set(wide_codes))
    def canonical(value):
        return re.sub(r"[\s\u00a0\u202f]", "", value)
    def vectors(source_header, source_rows):
        return {field: tuple(canonical(source_rows[code][i]) for code in common) for i, field in enumerate(source_header[2:], start=2)}
    narrow_vectors = vectors(header, narrow_codes)
    wide_vectors = vectors(wide_header, wide_codes)
    reverse = {}
    for field, vector in wide_vectors.items():
        reverse.setdefault(vector, []).append(field)
    matches = [{"narrow_field": field, "matching_wide_fields": reverse.get(vector, []), "matched_region_count": len(common), "degenerate_all_zero_vector": all(value == "0" for value in vector)} for field, vector in narrow_vectors.items()]
    fingerprints = {
        "unique_region_codes_compared": common, "regions_compared": len(common),
        "narrow_columns_with_exact_vector_matches": sum(bool(row["matching_wide_fields"]) for row in matches),
        "narrow_columns_without_exact_vector_matches": sum(not row["matching_wide_fields"] for row in matches),
        "nonzero_narrow_columns_with_exact_vector_matches": sum(bool(row["matching_wide_fields"]) and not row["degenerate_all_zero_vector"] for row in matches),
        "note": "Numeric equality does not establish tax measure, units or the applicable full schema. No positional tax reconstruction is permitted.",
        "matches": matches,
    }
    (OUT / "numeric_fingerprints.json").write_text(json.dumps(fingerprints, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(matches).to_csv(OUT / "numeric_fingerprints.csv", index=False)
    old_pit = pd.read_parquet(ROOT / "data/dashboard/releases/2026-10-02-cloud-v1/macro/pit_receipts.parquet")
    summary = {
        "assessment_date": "2026-10-05", "status": "audit_only_not_promoted",
        "findings": [
            "Three new schemas are syntactically valid CSV field dictionaries but contain no PIT tax-measure descriptions.",
            "H1 and nine-month 2025 wide data cannot be joined to the new small schemas by filename alone.",
            "FY2024 copy 3 has a matching narrow header but mixes malformed records, duplicate region keys and delimiters; its measure and units remain unverified.",
            "Space-grouped numeric cells require explicit parsing, not the existing pd.to_numeric call.",
            "No historical PIT quarter flows can safely be restored from this batch.",
            "No 20260701 data/schema pair was found; processing proceeds without that unavailable release.",
        ],
        "existing_registered_pit": {
            "rows": len(old_pit), "regions": int(old_pit.region_id.nunique()),
            "cutoffs": sorted(old_pit.reporting_cutoff.astype(str).unique().tolist()),
            "missing_period_flows": int(old_pit.pit_flow_rub.isna().sum()),
        },
        "new_20260701_files_found": [path.name for path in ROOT.glob("*20260701*")],
        "numeric_fingerprint_summary": {key: value for key, value in fingerprints.items() if key != "matches"},
        "profiles": profiles,
        "limits": [
            "No numerical reconciliation to published source totals or independent original report was performed.",
            "Region/industry codes alone do not establish PIT, units, tax scope or revision vintage.",
            "No raw inputs, registered releases, application code or Git state was modified.",
        ],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    flat = []
    for item in profiles:
        flat.append({key: value if not isinstance(value, (list, dict)) else json.dumps(value, ensure_ascii=False) for key, value in item.items()})
    pd.DataFrame(flat).to_csv(OUT / "file_profiles.csv", index=False)
    print(json.dumps({"status": summary["status"], "findings": summary["findings"], "existing_registered_pit": summary["existing_registered_pit"], "narrow_data": small_item, "profile_files": len(profiles), "nonzero_fingerprint_matches": fingerprints["nonzero_narrow_columns_with_exact_vector_matches"], "output": str(OUT)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
