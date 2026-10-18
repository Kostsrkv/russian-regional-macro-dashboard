"""Verify the delivered handoff without installing analytics dependencies."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys


def verify(root: Path) -> dict:
    root = root.resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("raw_files_included") is not False:
        raise ValueError("Unsupported package manifest")
    expected = set(manifest["files"])
    # Subordinate extraction manifests are payloads too; only the root manifest
    # is excluded from its own digest inventory.
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p != root / "manifest.json"}
    if actual != expected:
        raise ValueError("Missing or unexpected package files")
    for name, record in manifest["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe package path")
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Package path escapes its root")
        with path.open("rb") as stream:
            actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != record["bytes"] or actual_hash != record["sha256"]:
            raise ValueError(f"Changed package payload: {name}")
    return {"package_id": manifest["package_id"], "payloads_verified": len(expected),
            "status": manifest["status"], "source_release": manifest["source_release"]}


if __name__ == "__main__":
    print(json.dumps(verify(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent), indent=2))
