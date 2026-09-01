"""Helpers for immutable source provenance."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return a stable SHA-256 digest without loading a large file into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_id(path: Path) -> str:
    """Create a compact, content-addressed identifier for a raw source."""

    return f"{path.stem[:48]}-{sha256_file(path)[:12]}"

