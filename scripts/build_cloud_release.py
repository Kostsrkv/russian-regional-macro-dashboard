"""Package reviewed aggregate data for the existing Streamlit application.

This is an explicit publication snapshot, not a promotion to verified status.
Only the allowlisted processed files are copied. The private source files and
research exchange package are never opened or copied by this builder.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys

import pandas as pd
from pandas.testing import assert_frame_equal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from macro_rus.annual_production import load_annual_candidate
from macro_rus.cumulative_production import AUDIT_FILES, DATASET, load_cumulative_candidate
from macro_rus.fiscal_view import load_candidate as load_fiscal_candidate
from macro_rus.macro_preview import load_macro_candidate
from macro_rus.provenance import sha256_file
from macro_rus.revenue_view import load_revenue_candidate

RELEASE_ID = "2026-10-02"
RELEASE_DIRECTORY = RELEASE_ID + "-cloud-v1"
INPUT_ROOTS = {
    "macro": "outputs/expanded_macro_candidate_2026-09-29_v2",
    "revenue": "outputs/regional_revenue_candidate_2026-09-30",
    "fiscal": "outputs/regional_concordance_2026-09-29_v2/fiscal_preview",
    "annual": "outputs/annual_social_candidate_2026-10-02",
    "cumulative": "outputs/cumulative_production_candidate_2026-10-02",
}
FILES = {
    "macro": ("pit_receipts.parquet", "industrial_production.parquet", "sources.csv"),
    "revenue": ("budget_execution.parquet", "sources.csv", "lineage.csv"),
    "fiscal": ("fiscal_observations.csv",),
    "annual": ("industrial_production_annual.parquet",),
    "cumulative": (DATASET, *AUDIT_FILES),
}
COMPACT_AUDITS = ("source_basis_evidence.csv", "coverage.csv", "missing_observations.csv",
                  "pilot_q1_handchecks.csv")
GZIP_INPUTS = {"fiscal": "fiscal_observations.csv", "revenue": "lineage.csv"}
PATH_FIELD = re.compile(r"(?:^|_)(?:local_path|path|file|filename|candidate)$")
MACHINE_PATH = re.compile(r"(?:/(?:Users|home|private|tmp|var/folders)/|(?<![A-Za-z0-9])[A-Za-z]:[\\/])")
SECRET = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"https?://[^\s/]+:[^\s/@]+@|"
    r"[?&](?:access_token|token|api_key|signature|X-Amz-Signature)=[^\s&]+)", re.I)


def _sanitize_string(value: str, field: str = "") -> str:
    """Strip machine locations but retain public filenames, URLs and hashes."""
    if SECRET.search(value):
        raise ValueError("A recognizable credential or signed URL is present in publication metadata")
    if value.startswith(("http://", "https://")):
        return value
    if MACHINE_PATH.search(value):
        # A field containing a filesystem location is portable as a basename.
        # Embedded prose with a machine path cannot be safely interpreted.
        if PATH_FIELD.search(field) or value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
            return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        raise ValueError("Embedded private filesystem path in publication metadata")
    if field == "local_path":
        return value.replace("\\", "/").rsplit("/", 1)[-1]
    return value


def _sanitize_json(value, field=""):
    if isinstance(value, dict):
        return {key: _sanitize_json(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_json(item, field) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value, field)
    return value


def _sanitize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    for column in data:
        if data[column].dtype == object or isinstance(data[column].dtype, pd.StringDtype):
            data[column] = data[column].map(
                lambda value: _sanitize_string(value, column) if isinstance(value, str) else value)
    return data


def _copy_processed(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError("Required processed input is missing or a symlink")
    if source.suffix == ".parquet":
        original = pd.read_parquet(source)
        cleaned = _sanitize_frame(original)
        if original.equals(cleaned):
            shutil.copyfile(source, destination)
        else:
            cleaned.to_parquet(destination, index=False)
    elif source.suffix == ".csv":
        original = pd.read_csv(source, dtype=str, keep_default_na=False)
        cleaned = _sanitize_frame(original)
        if destination.name.endswith(".csv.gz"):
            # No source filename or wall-clock timestamp leaks into the header.
            with destination.open("wb") as raw, gzip.GzipFile(
                    fileobj=raw, mode="wb", filename="", mtime=0, compresslevel=9) as compressed:
                if original.equals(cleaned):
                    with source.open("rb") as incoming:
                        shutil.copyfileobj(incoming, compressed)
                else:
                    compressed.write(cleaned.to_csv(index=False).encode("utf-8"))
        elif original.equals(cleaned):
            shutil.copyfile(source, destination)
        else:
            cleaned.to_csv(destination, index=False)
    else:
        raise ValueError("Only allowlisted processed CSV and Parquet payloads can be published")


def _public_limitations(limits: list[str], compact: bool) -> list[str]:
    result = []
    for text in limits:
        if text in {"No deployment or promotion",
                    "No data promotion, download, deployment or external exchange performed.",
                    "Candidate is staged locally for review; no promotion or deployment."}:
            text = "Published descriptive review candidate; publication does not promote or verify observations."
        if compact and text.startswith("All original source vintages and missing/suppressed cells remain"):
            text = ("Selected-source missing/suppressed cells and source-cell lineage are retained. "
                    "Full vintage histories remain in the private research audit; their original hashes "
                    "are registered here without publishing those files.")
        result.append(text)
    return result


def _files(compact: bool, compressed_csv: bool = True) -> dict[str, tuple[str, ...]]:
    selected = dict(FILES)
    if compact:
        selected["cumulative"] = (DATASET, *COMPACT_AUDITS)
    if compressed_csv:
        for kind, source_csv in GZIP_INPUTS.items():
            selected[kind] = tuple(file + ".gz" if file == source_csv else file
                                   for file in selected[kind])
    return selected


def _input_filename(filename: str) -> str:
    return filename[:-3] if filename.endswith(".csv.gz") else filename


def _payload_text(path: Path) -> str:
    if path.name.endswith(".csv.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as compressed:
            return compressed.read()
    if path.suffix in {".json", ".csv"}:
        return path.read_text()
    if path.suffix == ".parquet":
        frame = pd.read_parquet(path)
        return "\n".join(str(value) for column in frame.select_dtypes(include=["object", "string"])
                         for value in frame[column].dropna().unique())
    raise ValueError("Non-processed public payload")


def _load_envelopes(root: Path) -> dict[str, pd.DataFrame]:
    macro, _ = load_macro_candidate(root / "macro")
    revenue, _ = load_revenue_candidate(root / "revenue")
    fiscal, _ = load_fiscal_candidate(root / "fiscal")
    annual, _ = load_annual_candidate(root / "annual")
    cumulative, _ = load_cumulative_candidate(root / "cumulative")
    return {"pit_receipts": macro["pit_receipts"],
            "industrial_production": macro["industrial_production"],
            "macro_sources": macro["sources"],
            "budget_execution": revenue["budget_execution"],
            "revenue_sources": revenue["sources"], "revenue_lineage": revenue["revenue_lineage"],
            "fiscal_observations": fiscal, "industrial_production_annual": annual,
            "industrial_production_cumulative": cumulative}


def _load_inputs(workspace: Path) -> dict[str, pd.DataFrame]:
    roots = {key: workspace / value for key, value in INPUT_ROOTS.items()}
    macro, _ = load_macro_candidate(roots["macro"])
    revenue, _ = load_revenue_candidate(roots["revenue"])
    fiscal, _ = load_fiscal_candidate(roots["fiscal"])
    annual, _ = load_annual_candidate(roots["annual"])
    cumulative, _ = load_cumulative_candidate(roots["cumulative"])
    return {"pit_receipts": macro["pit_receipts"], "industrial_production": macro["industrial_production"],
            "macro_sources": macro["sources"], "budget_execution": revenue["budget_execution"],
            "revenue_sources": revenue["sources"], "revenue_lineage": revenue["revenue_lineage"],
            "fiscal_observations": fiscal, "industrial_production_annual": annual,
            "industrial_production_cumulative": cumulative}


def _table_summary(frame: pd.DataFrame) -> dict:
    result = {"rows": len(frame), "missing_by_column": {c: int(frame[c].isna().sum()) for c in frame}}
    if "region_id" in frame:
        result["region_ids"] = sorted(frame.region_id.unique())
        result["regions"] = frame.region_id.nunique()
    return result


def validate_cloud_release(output: Path) -> dict:
    """Verify the bounded payload census, every file hash and all runtime loaders."""
    output = Path(output)
    manifest = json.loads((output / "manifest.json").read_text())
    if (manifest.get("schema_version") != 1 or manifest.get("review_status") != "candidate_not_promoted"
            or manifest.get("release_id") != RELEASE_ID or manifest.get("raw_files_included") is not False):
        raise ValueError("Unrecognized public release envelope")
    compact = manifest.get("cumulative_runtime_profile") == "cloud_compact_processed_v1"
    distribution = manifest.get("csv_distribution", "plain_csv")
    if distribution not in {"plain_csv", "deterministic_gzip_v1"}:
        raise ValueError("Unknown public CSV distribution")
    expected = {f"{kind}/{file}" for kind, files in _files(
                    compact, distribution == "deterministic_gzip_v1").items()
                for file in (*files, "manifest.json")}
    payloads = manifest.get("payloads")
    if not isinstance(payloads, dict) or set(payloads) != expected:
        raise ValueError("Public release payload allowlist mismatch")
    actual = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
    if actual != expected | {"manifest.json"}:
        raise ValueError("Unexpected or missing public release payload")
    for relative, record in payloads.items():
        relative_path = PurePosixPath(relative)
        path = output / relative
        if (relative_path.is_absolute() or ".." in relative_path.parts or path.is_symlink()
                or not isinstance(record, dict) or sha256_file(path) != record.get("sha256")
                or path.stat().st_size != record.get("bytes")):
            raise ValueError(f"Public payload integrity mismatch: {relative}")
        text = _payload_text(path)
        if MACHINE_PATH.search(text) or SECRET.search(text):
            raise ValueError("Private filesystem path or recognizable credential in public payload")
    public = _load_envelopes(output)
    expected_summaries = manifest.get("tables")
    if set(expected_summaries or {}) != set(public):
        raise ValueError("Public release table census mismatch")
    for name, frame in public.items():
        if _table_summary(frame) != expected_summaries[name]:
            raise ValueError("Public release missingness or coverage differs from declared snapshot")
    region_sets = [set(frame.region_id) for frame in public.values() if "region_id" in frame]
    if not region_sets or any(regions != region_sets[0] or len(regions) != 78 for regions in region_sets):
        raise ValueError("Public release regional population differs across datasets")
    return manifest


def build_cloud_release(workspace: Path = ROOT, output: Path | None = None, *,
                        compact_cumulative: bool = True) -> dict:
    workspace = Path(workspace)
    output = Path(output) if output else workspace / "data/dashboard/releases" / RELEASE_DIRECTORY
    if output.exists():
        raise FileExistsError("Release paths are immutable: choose a fresh versioned destination")
    original = _load_inputs(workspace)
    selected = _files(compact_cumulative)
    input_hashes = {}
    # Validate the complete allowlist before producing any output.
    for kind, files in selected.items():
        root = workspace / INPUT_ROOTS[kind]
        for file in (*files, "manifest.json"):
            path = root / _input_filename(file)
            if path.is_symlink() or not path.is_file():
                raise ValueError("Missing processed publication input")
            input_hashes[f"{kind}/{file}"] = sha256_file(path)
    output.mkdir(parents=True, exist_ok=False)
    payloads = {}
    for kind, files in selected.items():
        source_root, target = workspace / INPUT_ROOTS[kind], output / kind
        target.mkdir()
        for file in files:
            _copy_processed(source_root / _input_filename(file), target / file)
        candidate = _sanitize_json(json.loads((source_root / "manifest.json").read_text()))
        if "limitations" in candidate:
            candidate["limitations"] = _public_limitations(candidate["limitations"], kind == "cumulative" and compact_cumulative)
        candidate["publication_context"] = "Processed aggregate snapshot for Streamlit; observations retain their original review status."
        if kind == "revenue":
            candidate["upstream_derived_hashes"] = candidate["derived_hashes"]
            candidate["derived_hashes"] = {file: sha256_file(target / file) for file in files}
            candidate["lineage_file"] = "lineage.csv.gz"
        if kind == "fiscal":
            candidate["observations_file"] = "fiscal_observations.csv.gz"
            candidate["observations_sha256"] = sha256_file(target / candidate["observations_file"])
        if kind in {"annual", "cumulative"}:
            dataset = "industrial_production_annual.parquet" if kind == "annual" else DATASET
            candidate["dataset_sha256"] = sha256_file(target / dataset)
        if kind == "cumulative":
            candidate["private_audit_artifact_sha256"] = candidate["artifact_sha256"]
            candidate["artifact_sha256"] = {file: sha256_file(target / file) for file in files if file != DATASET}
            if compact_cumulative:
                candidate["distribution_mode"] = "cloud_compact"
        (target / "manifest.json").write_text(json.dumps(candidate, ensure_ascii=False, indent=2))
        for file in (*files, "manifest.json"):
            path, relative = target / file, f"{kind}/{file}"
            payloads[relative] = {"sha256": sha256_file(path), "bytes": path.stat().st_size,
                                  "role": "candidate_manifest" if file == "manifest.json" else "processed_aggregate_or_provenance",
                                  "input_file": f"{INPUT_ROOTS[kind]}/{_input_filename(file)}",
                                  "input_sha256": input_hashes[relative]}
    public = _load_envelopes(output)
    for name in original:
        assert_frame_equal(_sanitize_frame(original[name]), public[name], check_dtype=True, check_exact=True)
    manifest = {
        "schema_version": 1, "release_id": RELEASE_ID,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "publication_status": "prepared_for_requested_streamlit_publication",
        "review_status": "candidate_not_promoted", "raw_files_included": False,
        "contains": "Processed regional aggregates and bounded provenance; no raw files, private research documents or credentials.",
        "cumulative_runtime_profile": "cloud_compact_processed_v1" if compact_cumulative else "full_candidate",
        "csv_distribution": "deterministic_gzip_v1",
        "compression": "Lossless gzip of fiscal observations and revenue lineage; empty filename header, mtime=0, compression level 9.",
        "runtime_directories": {kind: kind for kind in selected},
        "payloads": payloads, "tables": {name: _table_summary(frame) for name, frame in public.items()},
        "parity": "Exact loaded values, rows, region population, review flags and missingness match the reviewed local candidates; only machine locations and publication-context wording may change.",
        "limitations": ["Publication does not promote the candidate to verified status.",
                        "Only 78 eligible regional profiles; no national aggregation or boundary map.",
                        "Regional inflation, wages, fuel prices, debt stocks and original/amended budgets are not included.",
                        "Source files are identified by names and SHA-256 hashes but remain private."],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return validate_cloud_release(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact-cumulative", action="store_true", default=True,
                        help="Publish only bounded cumulative proofs (the default)")
    parser.add_argument("--validate", type=Path)
    args = parser.parse_args()
    result = (validate_cloud_release(args.validate) if args.validate else
              build_cloud_release(ROOT, args.output, compact_cumulative=args.compact_cumulative))
    print(json.dumps({"release_id": result["release_id"], "review_status": result["review_status"],
                      "files": len(result["payloads"]),
                      "payload_bytes": sum(record["bytes"] for record in result["payloads"].values())}, indent=2))


if __name__ == "__main__":
    main()
