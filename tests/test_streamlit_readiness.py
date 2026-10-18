"""Readiness staging is non-destructive and cannot widen data access."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("readiness_builder", ROOT / "scripts/prepare_streamlit_update.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def test_existing_readiness_target_is_not_overwritten(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("Keep existing data")
    with pytest.raises(ValueError, match="overwrite"):
        build.prepare(ROOT, output, tmp_path / "report.json")
    assert sentinel.read_text() == "Keep existing data"


def test_existing_readiness_report_is_not_overwritten(tmp_path):
    report = tmp_path / "report.json"
    report.write_text("Existing report")
    with pytest.raises(ValueError, match="overwrite"):
        build.prepare(ROOT, tmp_path / "new", report)
    assert report.read_text() == "Existing report"


def test_readiness_payloads_pin_the_public_release_without_private_inputs(tmp_path):
    output = tmp_path / "prepared"
    result = build.prepare(ROOT, output, tmp_path / "report.json")
    assert result["status"] == "prepared_not_published"
    assert result["source_release"] == "2026-10-05"
    assert result["git_writes"] == result["network_requests"] == 0
    assert (output / "src/macro_rus/regional_notes.py").is_file()
    assert (output / "src/macro_rus/fiscal_diagnostics.py").is_file()
    assert not (output / "outputs").exists()
    assert not (output / ".git").exists()
    assert not (output / ".venv").exists()
    assert not list(output.rglob("*.xlsx"))
    assert build.digest(output / "data/dashboard/current.json") == build.digest(ROOT / "data/dashboard/current.json")
