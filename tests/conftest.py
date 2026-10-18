"""Skip only explicitly declared, absent private research inputs.

Public runtime checks remain active. Invalid available data must still fail.
No source datasets are acquired, substituted or copied into public CI.
"""
from pathlib import Path

import pytest


def missing_private_inputs(root: Path, paths) -> list[str]:
    missing = []
    for value in paths:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("Private test inputs must be explicit project-relative paths")
        if not (root / path).exists():
            missing.append(path.as_posix())
    return missing


def pytest_configure(config):
    config.addinivalue_line("markers", "private_data(*paths): requires private local audit artifacts; skipped only when absent")


def pytest_collection_modifyitems(config, items):
    for item in items:
        paths = [path for marker in item.iter_markers("private_data") for path in marker.args]
        if not paths:
            continue
        missing = missing_private_inputs(Path(config.rootpath), paths)
        if missing:
            item.add_marker(pytest.mark.skip(reason="Private audit inputs not distributed: " + ", ".join(missing)))
