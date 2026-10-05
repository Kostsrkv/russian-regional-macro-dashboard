"""Clean-checkout startup and navigation against the public release only."""
import json
from pathlib import Path

import pytest

from macro_rus.cloud_release import OVERRIDES, resolve_dashboard_config

ROOT = Path(__file__).resolve().parents[1]


def test_public_default_and_explicit_local_override():
    config = resolve_dashboard_config(ROOT, {})
    assert config.release_id == "2026-10-05"
    assert len(config.render_arguments()) == 6
    assert all(path.is_dir() for path in config.candidates.values())
    assert config.fuel_root.is_dir()
    local = resolve_dashboard_config(ROOT, {"MACRO_RUS_DATA_DIR": "/tmp/example-only"})
    assert local.release_id is None and local.data_root == Path("/tmp/example-only")
    assert all(path is None for path in local.candidates.values())
    assert local.fuel_root is None


def test_pointer_tampering_and_path_escape_fail_closed(tmp_path):
    public = tmp_path / "data/dashboard"
    public.mkdir(parents=True)
    pointer = json.loads((ROOT / "data/dashboard/current.json").read_text())
    pointer["release_path"] = "../private"
    (public / "current.json").write_text(json.dumps(pointer))
    with pytest.raises(ValueError, match="repository-relative"):
        resolve_dashboard_config(tmp_path, {})
    pointer["release_path"] = str(ROOT / "data/dashboard/releases/2026-10-02-cloud-v1")
    (public / "current.json").write_text(json.dumps(pointer))
    with pytest.raises(ValueError, match="repository-relative"):
        resolve_dashboard_config(tmp_path, {})


def test_cloud_all_views_and_region_selection(monkeypatch):
    from streamlit.testing.v1 import AppTest

    for variable in ["MACRO_RUS_DATA_DIR", *OVERRIDES.values(), "MACRO_RUS_FUEL_CANDIDATE_DIR"]:
        monkeypatch.delenv(variable, raising=False)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=40).run()
    assert not app.exception and not app.error
    assert len(app.sidebar.selectbox[0].options) == 78
    assert any("Research release" in item.value for item in app.sidebar.caption)
    for view in app.sidebar.radio[0].options:
        app.sidebar.radio[0].set_value(view).run()
        assert not app.exception, view
        assert not app.error, view
    app.sidebar.radio[0].set_value("PIT–production check").run()
    table = app.dataframe[0].value
    assert table.loc[table["OKVED2 section"].eq("B"), "Nominal PIT YoY"].eq("Not available").all()
    app.sidebar.selectbox[0].set_value("RU-SVE").run()
    assert not app.exception and not app.error
    app.sidebar.selectbox[0].set_value("FNS-16").run()
    assert not app.exception and not app.error
    app.sidebar.radio[0].set_value("Eligible-region overview (optional)").run()
    assert not app.exception and len(app.dataframe[0].value) == 78
