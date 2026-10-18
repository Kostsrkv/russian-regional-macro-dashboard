"""Source-backed candidate concordance; no fuzzy joins, promotion or map totals."""
from pathlib import Path
import json
import re
import sys
import unicodedata

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from macro_rus.ingest_fns import _delimiter
from macro_rus.ingest_rosstat import SHEETS
from macro_rus.provenance import sha256_file
from macro_rus.regions import REGION_RECORDS


def normalize(name):
    """Normalize typography only; never remove territorial scope qualifiers."""
    text = unicodedata.normalize("NFKC", str(name)).casefold().replace("ё", "е")
    return re.sub(r"[\s.\-–—]+", "", text)


# Explicit aliases observed in the selected workbooks; retain originals in output.
ROSSTAT_ALIASES = {
    "Ненецкий автономный округ": "в том числе Ненецкий авт.округ",
    "Ханты-Мансийский автономный округ - Югра": "в том числе: Ханты-Мансийский авт. округ - Югра",
    "Ямало-Ненецкий автономный округ": "Ямало-Ненецкий авт. округ",
    "Кемеровская область - Кузбасс": "Кемеровская область",
    "Еврейская автономная область": "Еврейская авт.область",
    "Чукотский автономный округ": "Чукотский авт.округ",
}
SCOPE_HOLD = {"Архангельская область", "Ненецкий автономный округ", "Тюменская область",
              "Ханты-Мансийский автономный округ - Югра", "Ямало-Ненецкий автономный округ"}
SEPARATE_GEOGRAPHY = {"Республика Крым", "г. Севастополь", "Донецкая Народная Республика",
                      "Луганская Народная Республика", "Запорожская область", "Херсонская область",
                      'Федеральная территория "Сириус"', "город Байконур"}


def unique_lookup(pairs):
    result = {}
    for label, payload in pairs:
        key = normalize(label)
        if key in result:
            raise ValueError(f"Ambiguous normalized region name: {label}")
        result[key] = payload
    return result


def run(output):
    output = Path(output)
    if output.exists():
        raise FileExistsError("Preserve previous candidate; choose a fresh folder")
    treasury = ROOT / "outputs/treasury_coverage_2026-09-29"
    fns_path = ROOT / "data-20260401-structure-20260401.csv"
    schema_path = ROOT / "structure-20260401.csv"
    rosstat_path = ROOT / "ind_sub_2023_07-2026.xlsx"
    schema = pd.read_csv(schema_path, dtype=str).set_index("field name")
    if not all("субъекта Российской Федерации" in schema.loc[c, "russian description"] for c in ("GA", "GB")):
        raise ValueError("FNS identifier field semantics changed")
    fns = pd.read_csv(fns_path, sep=_delimiter(fns_path), usecols=["GA", "GB"], dtype=str)
    if fns[["GA", "GB"]].isna().any().any() or fns.GA.duplicated().any():
        raise ValueError("Invalid FNS regional identifiers")
    fns_lookup = unique_lookup((r.GB, {"fns_code": r.GA.zfill(2), "fns_name_ru": r.GB.strip(), "fns_csv_row": i + 2})
                               for i, r in enumerate(fns.itertuples(index=False)))
    workbook = pd.ExcelFile(rosstat_path)
    rosstat = {}
    for sheet in SHEETS:
        frame = pd.read_excel(workbook, sheet_name=sheet, header=None)
        pairs = [(str(row[0]), {"name": str(row[0]).strip(), "excel_row": index + 1,
                               "numeric_cells": int(pd.to_numeric(row.iloc[1:], errors="coerce").notna().sum())})
                 for index, row in frame.iloc[5:].iterrows() if pd.notna(row[0])]
        rosstat[sheet] = unique_lookup(pairs)
    review = pd.read_csv(treasury / "region_crosswalk_review.csv", dtype=str)
    pilots = {r["region_name_ru"]: r for r in REGION_RECORDS}
    rows, evidence = [], []
    for source in review.to_dict("records"):
        name = source["source_territory_name_ru"]
        tax = fns_lookup.get(normalize(name), {})
        target = ROSSTAT_ALIASES.get(name, name)
        matches = [rosstat[sheet].get(normalize(target)) for sheet in SHEETS]
        production_match = all(match is not None for match in matches)
        production_values = all(match is not None and match["numeric_cells"] > 0 for match in matches)
        status = ("separate_geography_review" if name in SEPARATE_GEOGRAPHY else
                  "overlap_scope_review" if name in SCOPE_HOLD else
                  "unmatched_source" if not tax or not production_match else "matched_candidate")
        row = {"territory_key": source["territory_key"], "treasury_name_ru": name,
               **tax, "rosstat_name_ru": matches[0]["name"] if matches[0] else "",
               "rosstat_match_method": "explicit_alias" if name in ROSSTAT_ALIASES else "typography_normalization",
               "matched_all_five_production_sheets": production_match,
               "numeric_values_in_all_five_production_sheets": production_values,
               "status": status, "region_id": pilots[name]["region_id"] if name in pilots else
               ("FNS-" + tax["fns_code"] if tax else ""),
               "eligible_for_candidate_profile": status == "matched_candidate",
               "eligible_for_national_aggregation": False}
        rows.append(row)
        for sheet, match in zip(SHEETS, matches):
            if match:
                evidence.append({"territory_key": source["territory_key"], "source_file": rosstat_path.name,
                                 "sheet": sheet, "industrial_section": SHEETS[sheet][0], **match})
    crosswalk = pd.DataFrame(rows)
    mapped = crosswalk[crosswalk.fns_code.notna()]
    if mapped.fns_code.duplicated().any() or mapped.region_id.duplicated().any():
        raise ValueError("Crosswalk is not one-to-one")
    actuals = pd.read_csv(treasury / "staging_observations.csv", dtype={"metric_code": str})
    selected = crosswalk[crosswalk.eligible_for_candidate_profile][["territory_key", "region_id"]]
    candidate = actuals.merge(selected, on="territory_key", how="inner", validate="many_to_one")
    candidate = candidate.rename(columns={"source_territory_name_ru": "region_name_ru"})
    if candidate.duplicated(["region_id", "period", "kind", "metric_code"]).any():
        raise ValueError("Expanded fiscal grain is not unique")
    output.mkdir(parents=True)
    crosswalk.to_csv(output / "region_concordance.csv", index=False)
    pd.DataFrame(evidence).to_csv(output / "rosstat_row_evidence.csv", index=False)
    candidate.to_csv(output / "fiscal_candidate.csv", index=False)
    preview = output / "fiscal_preview"
    preview.mkdir()
    candidate.to_csv(preview / "fiscal_observations.csv", index=False)
    audit_manifest = json.loads((treasury / "summary.json").read_text())
    checks = pd.read_csv(treasury / "checks.csv")
    selected_checks = checks.merge(selected, on="territory_key", validate="many_to_one")
    preview_manifest = dict(status="candidate_not_promoted", rows=len(candidate),
                            region_periods=len(candidate[["region_id", "period"]].drop_duplicates()),
                            blocking_failures=int((selected_checks.blocking & ~selected_checks.passed).sum()),
                            plan_warnings=int((~selected_checks.blocking & ~selected_checks.passed).sum()),
                            sources=[{**s, "selected_html_crc_passed": True} for s in audit_manifest["sources"]])
    (preview / "manifest.json").write_text(json.dumps(preview_manifest, ensure_ascii=False, indent=2))
    source_paths = [fns_path, schema_path, rosstat_path, treasury / "staging_observations.csv", treasury / "region_crosswalk_review.csv"]
    manifest = dict(status="candidate_not_promoted", status_counts=crosswalk.status.value_counts().to_dict(),
                    fns_matched=int(crosswalk.fns_code.notna().sum()),
                    rosstat_matched=int(crosswalk.matched_all_five_production_sheets.sum()),
                    profile_regions=len(selected), fiscal_rows=len(candidate),
                    fiscal_region_periods=len(candidate[["region_id", "period"]].drop_duplicates()),
                    sources=[{"file": str(p.relative_to(ROOT)), "sha256": sha256_file(p)} for p in source_paths],
                    limitations=["Latest-release name concordance only, not validation of all FNS history",
                                 "FNS-nn IDs are project IDs based on source codes, not ISO codes",
                                 "Rosstat row matches establish identity, not full period/measure comparability",
                                 "Five nested-region entries and eight separate-geography entries held back",
                                 "No national aggregation, deployment, or promotion"])
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in manifest.items() if k != "sources"}, ensure_ascii=False, indent=2))
    print(crosswalk.loc[crosswalk.status.ne("matched_candidate"), ["treasury_name_ru", "status"]].to_string(index=False))
    return manifest


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else ROOT / "outputs/regional_concordance_2026-09-29")
