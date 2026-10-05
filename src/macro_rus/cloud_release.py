"""Resolve a checked-in, immutable public research snapshot without a VPN."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Mapping

OVERRIDES = {
    "fiscal": "MACRO_RUS_FISCAL_CANDIDATE_DIR",
    "macro": "MACRO_RUS_MACRO_CANDIDATE_DIR",
    "revenue": "MACRO_RUS_REVENUE_CANDIDATE_DIR",
    "annual": "MACRO_RUS_ANNUAL_CANDIDATE_DIR",
    "cumulative": "MACRO_RUS_CUMULATIVE_CANDIDATE_DIR",
}
PUBLIC_FILES = {
    "macro": {"manifest.json", "pit_receipts.parquet", "industrial_production.parquet", "sources.csv"},
    "revenue": {"manifest.json", "budget_execution.parquet", "sources.csv", "lineage.csv.gz"},
    "fiscal": {"manifest.json", "fiscal_observations.csv.gz"},
    "annual": {"manifest.json", "industrial_production_annual.parquet"},
    "cumulative": {"manifest.json", "industrial_production_cumulative.parquet",
                   "source_basis_evidence.csv", "coverage.csv", "missing_observations.csv",
                   "pilot_q1_handchecks.csv"},
}
FUEL_PUBLIC_FILES = {"manifest.json", "fuel_prices_eligible_regions_candidate.parquet"}


@dataclass(frozen=True)
class DashboardConfig:
    data_root: Path
    candidates: Mapping[str, Path | None]
    release_id: str | None = None
    fuel_root: Path | None = None

    def render_arguments(self) -> tuple:
        return (self.data_root, *(self.candidates[name] for name in OVERRIDES))


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def resolve_dashboard_config(project_root: Path, environ: Mapping[str, str]) -> DashboardConfig:
    """Explicit local overrides win; invalid public releases fail closed."""
    base = Path(environ.get("MACRO_RUS_DATA_DIR", project_root / "data/promoted/current"))
    candidates = {name: Path(environ[key]) if environ.get(key) else None
                  for name, key in OVERRIDES.items()}
    if "MACRO_RUS_DATA_DIR" in environ or any(key in environ for key in OVERRIDES.values()):
        return DashboardConfig(base, candidates)
    public_root = project_root / "data/dashboard"
    pointer_file = public_root / "current.json"
    if not pointer_file.exists():
        return DashboardConfig(base, candidates)
    pointer = json.loads(pointer_file.read_text())
    if pointer.get("schema_version") != 1:
        raise ValueError("Unsupported public release pointer")
    relative = Path(pointer["release_path"])
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 2 or relative.parts[0] != "releases":
        raise ValueError("Public release path must be a repository-relative release directory")
    release = public_root / relative
    if release.is_symlink() or release.resolve().parent != (public_root / "releases").resolve():
        raise ValueError("Public release path escapes the release directory")
    manifest_file = release / "manifest.json"
    if _sha256(manifest_file) != pointer["manifest_sha256"]:
        raise ValueError("Public release manifest checksum mismatch")
    manifest = json.loads(manifest_file.read_text())
    if (manifest.get("schema_version") != 1 or manifest.get("release_id") != pointer["release_id"]
            or manifest.get("review_status") != "candidate_not_promoted"
            or manifest.get("raw_files_included") is not False):
        raise ValueError("Unrecognized public research release")
    directories = dict(PUBLIC_FILES)
    if manifest.get("runtime_directories", {}).get("fuel") == "fuel":
        directories["fuel"] = FUEL_PUBLIC_FILES
    expected = {f"{kind}/{filename}" for kind, files in directories.items() for filename in files}
    payloads = manifest["payloads"]
    actual = {path.relative_to(release).as_posix() for path in release.rglob("*")
              if path.is_file() and path != manifest_file}
    if set(payloads) != expected or actual != expected:
        raise ValueError("Unexpected or missing public release files")
    if manifest.get("runtime_directories") != {name: name for name in directories}:
        raise ValueError("Unexpected public runtime directories")
    for relative_name, record in payloads.items():
        path = release / relative_name
        if (path.is_symlink() or path.resolve().parent != (release / relative_name.split("/")[0]).resolve()
                or path.stat().st_size != record["bytes"] or _sha256(path) != record["sha256"]):
            raise ValueError(f"Public release checksum or path mismatch: {relative_name}")
    return DashboardConfig(base, {name: release / name for name in OVERRIDES}, pointer["release_id"],
                           release / "fuel" if "fuel" in directories else None)
