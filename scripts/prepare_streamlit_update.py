"""Prepare a byte-pinned, private-input-free local deployment checkout.

No network, Git writes, source downloads, promotion, commits or publication.
The selected data pointer and source release remain unchanged. This directory
is for testing the next code update, not an authorization to publish it.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from macro_rus.cloud_release import resolve_dashboard_config

TOP_LEVEL = ("app.py", "requirements.txt", "pyproject.toml", "README.md", "DEPLOYMENT.md",
             ".gitignore", ".streamlit/config.toml", ".github/workflows/ci.yml")
ROLLBACK_RELEASES = ("2026-10-02-cloud-v1",)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(project: Path, output: Path, report: Path, *, prepared_on: str | None = None) -> dict:
    if output.exists() or report.exists():
        raise ValueError("Refusing to overwrite an existing readiness checkout/report")
    config = resolve_dashboard_config(project, {})
    if not config.release_id or not config.fuel_root:
        raise ValueError("A checksummed public release with fuel data is required")
    pointer = json.loads((project / "data/dashboard/current.json").read_text())
    release = project / "data/dashboard" / pointer["release_path"]
    selected = [project / name for name in TOP_LEVEL]
    selected.extend(sorted((project / "src/macro_rus").glob("*.py")))
    selected.extend(sorted((project / "tests").glob("*.py")))
    # Existing boundary tests import these builders. They do not acquire data.
    selected.extend(sorted((project / "scripts").glob("*.py")))
    selected.append(project / "data/dashboard/current.json")
    selected.extend(sorted(path for path in release.rglob("*") if path.is_file()))
    rollbacks = []
    for name in ROLLBACK_RELEASES:
        previous = project / "data/dashboard/releases" / name
        if previous == release:
            continue
        if not previous.is_dir() or previous.is_symlink():
            raise ValueError(f"Required rollback release is unavailable: {name}")
        previous_manifest = json.loads((previous / "manifest.json").read_text())
        for relative, record in previous_manifest["payloads"].items():
            source = previous / relative
            if Path(relative).is_absolute() or ".." in Path(relative).parts or source.is_symlink() or digest(source) != record["sha256"]:
                raise ValueError("Rollback release path/hash mismatch")
        selected.extend(sorted(path for path in previous.rglob("*") if path.is_file()))
        rollbacks.append({"release_path": "releases/" + name, "manifest_sha256": digest(previous / "manifest.json")})
    records = {}
    for source in selected:
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Required source file is missing or a symlink: {source.relative_to(project)}")
        relative = source.relative_to(project).as_posix()
        if relative in records:
            raise ValueError("Duplicate readiness payload")
        expected = digest(source)
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if digest(destination) != expected or digest(source) != expected:
            raise ValueError(f"Source changed while preparing checkout: {relative}")
        records[relative] = {"sha256": expected, "bytes": source.stat().st_size}
    copied = resolve_dashboard_config(output, {})
    if copied.release_id != config.release_id or (output / "outputs").exists():
        raise ValueError("Copied checkout release/private-input isolation failed")
    result = {
        "prepared_on": date.fromisoformat(prepared_on or date.today().isoformat()).isoformat(), "status": "prepared_not_published",
        "source_release": config.release_id, "source_manifest_sha256": pointer["manifest_sha256"],
        "entrypoint": "app.py", "python_target": "3.12", "files": records,
        "raw_files_included": False, "private_outputs_included": False,
        "data_promoted": False, "network_requests": 0, "git_writes": 0,
        "publication_verification": "not_performed",
        "scope": "Current runtime source and selected immutable dataset; no external sharing",
        "rollback_releases": rollbacks,
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2) + "\n")
    return {key: value for key, value in result.items() if key != "files"} | {"file_count": len(records)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--prepared-on", help="Preparation date (YYYY-MM-DD); source-data freshness is unchanged")
    args = parser.parse_args()
    print(json.dumps(prepare(args.project.resolve(), args.output.resolve(), args.report.resolve(), prepared_on=args.prepared_on), indent=2))
