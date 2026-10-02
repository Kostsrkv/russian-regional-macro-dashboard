"""Public snapshot parity, privacy, integrity and loader contracts."""
from pathlib import Path
import gzip
import hashlib
import importlib.util
import io
import json
import re

import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "data/dashboard/releases/2026-10-02-cloud-v1"
SPEC = importlib.util.spec_from_file_location("cloud_release_builder", ROOT / "scripts/build_cloud_release.py")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)
PRIVATE_AVAILABLE = all((ROOT / value / "manifest.json").is_file()
                        for value in BUILDER.INPUT_ROOTS.values())


def test_public_release_processed_data_coverage_and_review_status():
    manifest = BUILDER.validate_cloud_release(RELEASE)
    public = BUILDER._load_envelopes(RELEASE)
    for name in public:
        if "region_id" in public[name]:
            assert public[name].region_id.nunique() == 78
            assert set(public[name].region_id) == set(manifest["tables"][name]["region_ids"])
    assert manifest["review_status"] == "candidate_not_promoted"
    assert public["pit_receipts"].quality_status.eq("warning").all()
    assert public["budget_execution"].quality_status.eq("provisional").all()
    assert public["industrial_production_cumulative"].quality_status.eq("candidate").all()
    assert public["industrial_production_annual"].index_value.isna().sum() == 11
    assert public["industrial_production_cumulative"].index_value.isna().sum() == 144
    assert public["pit_receipts"].cpi_index.isna().all()
    assert {name: len(frame) for name, frame in public.items()} == {
        "pit_receipts": 4758, "industrial_production": 54066, "macro_sources": 5,
        "budget_execution": 3744, "revenue_sources": 8, "revenue_lineage": 3744,
        "fiscal_observations": 14664, "industrial_production_annual": 4290,
        "industrial_production_cumulative": 54210}


@pytest.mark.skipif(not PRIVATE_AVAILABLE, reason="Private source candidate outputs are intentionally not published")
def test_private_candidate_exact_parity_before_publication():
    original, public = BUILDER._load_inputs(ROOT), BUILDER._load_envelopes(RELEASE)
    for name in original:
        assert_frame_equal(BUILDER._sanitize_frame(original[name]), public[name], check_exact=True)
        assert public[name].isna().sum().to_dict() == original[name].isna().sum().to_dict()
        if "region_id" in public[name]:
            assert set(public[name].region_id) == set(original[name].region_id)
    manifest = BUILDER.validate_cloud_release(RELEASE)
    for record in manifest["payloads"].values():
        assert BUILDER.sha256_file(ROOT / record["input_file"]) == record["input_sha256"]
    candidate = json.loads((RELEASE / "cumulative/manifest.json").read_text())
    original_manifest = json.loads((ROOT / BUILDER.INPUT_ROOTS["cumulative"] / "manifest.json").read_text())
    assert candidate["private_audit_artifact_sha256"] == original_manifest["artifact_sha256"]
    assert candidate["sources"] == original_manifest["sources"]


def test_public_release_allowlist_checksums_upstream_lineage_and_compact_size():
    manifest = BUILDER.validate_cloud_release(RELEASE)
    assert manifest["raw_files_included"] is False
    assert "manifest.json" not in manifest["payloads"]
    assert sum(item["bytes"] for item in manifest["payloads"].values()) < 2_000_000
    expected = {f"{kind}/{file}" for kind, files in BUILDER._files(True).items()
                for file in (*files, "manifest.json")}
    assert set(manifest["payloads"]) == expected
    for relative, record in manifest["payloads"].items():
        assert BUILDER.sha256_file(RELEASE / relative) == record["sha256"]
        assert re.fullmatch(r"[a-f0-9]{64}", record["input_sha256"])
        assert record["bytes"] == (RELEASE / relative).stat().st_size
    candidate = json.loads((RELEASE / "cumulative/manifest.json").read_text())
    assert candidate["distribution_mode"] == "cloud_compact"
    assert set(candidate["artifact_sha256"]) == set(BUILDER.COMPACT_AUDITS)
    assert set(candidate["private_audit_artifact_sha256"]) == set(BUILDER.AUDIT_FILES)
    assert all(re.fullmatch(r"[a-f0-9]{64}", value)
               for value in candidate["private_audit_artifact_sha256"].values())
    assert candidate["status"] == "candidate_not_promoted"
    assert not (RELEASE / "cumulative/vintage_observations.csv").exists()
    assert not (RELEASE / "cumulative/overlap_audit.csv").exists()
    assert not (RELEASE / "cumulative/industrial_production_cumulative.csv").exists()


def test_public_metadata_never_contains_machine_paths_raw_files_or_credentials():
    paths = list(RELEASE.rglob("*"))
    assert not any(path.suffix in {".xlsx", ".xls", ".zip", ".ipynb", ".html", ".pdf"}
                   for path in paths)
    assert not any(path.is_symlink() for path in paths)
    for path in paths:
        if not path.is_file():
            continue
        text = BUILDER._payload_text(path)
        assert not BUILDER.MACHINE_PATH.search(text), path
        assert not BUILDER.SECRET.search(text), path
        assert not re.search(r"\b(?:localhost|127\.0\.0\.1):\d+", text), path
    for kind in BUILDER.FILES:
        candidate = json.loads((RELEASE / kind / "manifest.json").read_text())
        assert candidate["status"] == "candidate_not_promoted"
        assert not any(text in candidate.get("limitations", []) for text in [
            "No deployment or promotion", "Candidate is staged locally for review; no promotion or deployment."])


def test_existing_release_is_immutable():
    with pytest.raises(FileExistsError, match="immutable"):
        BUILDER.build_cloud_release(ROOT, RELEASE)


def test_machine_path_normalization_and_fail_closed_secret_metadata():
    assert BUILDER._sanitize_string("/Users/example/private/input.csv", "local_path") == "input.csv"
    assert BUILDER._sanitize_string(r"C:\private\input.xlsx", "source_file") == "input.xlsx"
    assert BUILDER._sanitize_string("inputs/raw/data.csv", "local_path") == "data.csv"
    assert BUILDER._sanitize_string("https://rosstat.gov.ru/enterprise_industrial", "source_url") == "https://rosstat.gov.ru/enterprise_industrial"
    with pytest.raises(ValueError, match="Embedded private"):
        BUILDER._sanitize_string("Read my /Users/example/private.csv instead", "notes")
    with pytest.raises(ValueError, match="credential"):
        BUILDER._sanitize_string("ghp_" + "x" * 36, "notes")
    with pytest.raises(ValueError, match="credential"):
        BUILDER._sanitize_string("https://example.org/file?access_token=private", "source_url")


def test_missing_payload_and_unrecognized_envelope_rejected(tmp_path):
    manifest = json.loads((RELEASE / "manifest.json").read_text())
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Unexpected or missing"):
        BUILDER.validate_cloud_release(tmp_path)
    manifest["review_status"] = "verified"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Unrecognized"):
        BUILDER.validate_cloud_release(tmp_path)


def test_public_outer_manifest_detects_tampering_without_loader_guessing(tmp_path):
    import shutil
    shutil.copytree(RELEASE, tmp_path / "release")
    payload = tmp_path / "release" / "fiscal/fiscal_observations.csv.gz"
    with payload.open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError, match="integrity mismatch"):
        BUILDER.validate_cloud_release(tmp_path / "release")


def test_gzip_csvs_are_lossless_deterministic_and_loader_selected():
    manifest = BUILDER.validate_cloud_release(RELEASE)
    assert manifest["csv_distribution"] == "deterministic_gzip_v1"
    for kind, original_filename in BUILDER.GZIP_INPUTS.items():
        relative = f"{kind}/{original_filename}.gz"
        path = RELEASE / relative
        payload = path.read_bytes()
        assert payload[:3] == b"\x1f\x8b\x08"
        assert payload[3] == 0  # No filename header or other optional metadata.
        assert payload[4:8] == bytes(4)  # mtime=0.
        decompressed = gzip.decompress(payload)
        assert hashlib.sha256(decompressed).hexdigest() == manifest["payloads"][relative]["input_sha256"]
        regenerated = io.BytesIO()
        with gzip.GzipFile(fileobj=regenerated, mode="wb", filename="", mtime=0,
                           compresslevel=9) as compressed:
            compressed.write(decompressed)
        assert regenerated.getvalue() == payload
        candidate = json.loads((RELEASE / kind / "manifest.json").read_text())
        field = "observations_file" if kind == "fiscal" else "lineage_file"
        assert candidate[field] == original_filename + ".gz"
        if kind == "fiscal":
            assert candidate["observations_sha256"] == BUILDER.sha256_file(path)
        else:
            assert candidate["derived_hashes"][original_filename + ".gz"] == BUILDER.sha256_file(path)


def test_unknown_csv_distribution_is_not_silently_guessed(tmp_path):
    manifest = json.loads((RELEASE / "manifest.json").read_text())
    manifest["csv_distribution"] = "unsupported_archive_format"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Unknown public CSV distribution"):
        BUILDER.validate_cloud_release(tmp_path)
