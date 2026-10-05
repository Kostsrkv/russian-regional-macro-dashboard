"""Public fuel snapshot parity, rollback and fail-closed integrity checks."""
from pathlib import Path
import importlib.util
import json
import shutil

from pandas.testing import assert_frame_equal
import pytest

from macro_rus.cloud_release import resolve_dashboard_config
from macro_rus.fuel_prices import DATASET, load_fuel_candidate
from macro_rus.provenance import sha256_file

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data/dashboard/releases/2026-10-02-cloud-v1"
RELEASE = ROOT / "data/dashboard/releases/2026-10-05-cloud-v1"
SPEC = importlib.util.spec_from_file_location("fuel_publication_validator", ROOT / "scripts/build_cloud_release.py")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


def test_public_fuel_snapshot_and_existing_data_exact_parity():
    manifest = BUILDER.validate_cloud_release(RELEASE, release_id="2026-10-05")
    assert manifest["review_status"] == "candidate_not_promoted"
    assert manifest["raw_files_included"] is False
    assert manifest["previous_manifest_sha256"] == sha256_file(BASE / "manifest.json")
    before = BUILDER.validate_cloud_release(BASE)
    for path, record in before["payloads"].items():
        assert (RELEASE / path).read_bytes() == (BASE / path).read_bytes()
        assert manifest["payloads"][path] == record
    public = BUILDER._load_envelopes(RELEASE)
    fuel, envelope = load_fuel_candidate(RELEASE / "fuel", eligible_region_ids=public["pit_receipts"].region_id.unique())
    assert len(fuel) == 21840 and fuel.region_id.nunique() == 78
    assert fuel.price_rub_per_litre.isna().sum() == 112
    assert envelope["distribution_mode"] == "cloud_processed_fuel_v1"
    assert len(envelope["sources"]) == 5
    assert envelope["publication_date"] is None and envelope["retrieval_date"] is None
    private = ROOT / "outputs/new_prices_audit_2026-10-05"
    if private.is_dir():
        original, _ = load_fuel_candidate(private, eligible_region_ids=fuel.region_id.unique())
        assert_frame_equal(original, fuel, check_exact=True)


def test_public_payload_is_small_raw_free_and_has_no_machine_locations():
    manifest = BUILDER.validate_cloud_release(RELEASE, release_id="2026-10-05")
    assert sum(item["bytes"] for item in manifest["payloads"].values()) < 3_000_000
    for path in RELEASE.rglob("*"):
        if path.is_file():
            assert path.suffix not in {".xlsx", ".xls", ".zip", ".pdf", ".ipynb", ".html"}
            text = BUILDER._payload_text(path)
            assert not BUILDER.MACHINE_PATH.search(text)
            assert not BUILDER.SECRET.search(text)


def test_old_release_can_be_selected_without_fuel(tmp_path):
    public = tmp_path / "data/dashboard"
    (public / "releases").mkdir(parents=True)
    shutil.copytree(BASE, public / "releases" / BASE.name)
    pointer = dict(schema_version=1, release_id="2026-10-02",
                   release_path=f"releases/{BASE.name}",
                   manifest_sha256=sha256_file(BASE / "manifest.json"))
    (public / "current.json").write_text(json.dumps(pointer))
    config = resolve_dashboard_config(tmp_path, {})
    assert config.release_id == "2026-10-02" and config.fuel_root is None
    assert len(config.render_arguments()) == 6


def test_corrupt_fuel_payload_rejected_before_app_load(tmp_path):
    public = tmp_path / "data/dashboard"
    (public / "releases").mkdir(parents=True)
    target = public / "releases" / RELEASE.name
    shutil.copytree(RELEASE, target)
    (public / "current.json").write_bytes((ROOT / "data/dashboard/current.json").read_bytes())
    with (target / "fuel" / DATASET).open("ab") as stream:
        stream.write(b"corruption")
    with pytest.raises(ValueError, match="checksum or path mismatch"):
        resolve_dashboard_config(tmp_path, {})
    with pytest.raises(ValueError, match="integrity or envelope mismatch"):
        load_fuel_candidate(target / "fuel", eligible_region_ids=["RU-KLU"])


def test_public_envelope_cannot_downgrade_to_private_checks(tmp_path):
    shutil.copytree(RELEASE / "fuel", tmp_path / "fuel")
    target = tmp_path / "fuel"
    envelope = json.loads((target / "manifest.json").read_text())
    envelope["status"] = "verified"
    (target / "manifest.json").write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="integrity or envelope mismatch"):
        load_fuel_candidate(target, eligible_region_ids=["RU-KLU"])
