from pathlib import Path

import pytest

from macro_rus.macro_preview import load_macro_candidate

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "outputs/expanded_macro_candidate_2026-09-29_v2"
pytestmark = pytest.mark.private_data("outputs/expanded_macro_candidate_2026-09-29_v2",
                                    "outputs/regional_concordance_2026-09-29_v2/fiscal_preview")


def test_candidate_load_and_mining_guard():
    from macro_rus.dashboard import pit_summary
    tables, manifest = load_macro_candidate(CANDIDATE)
    assert manifest["eligible_regions"] == 78
    frame = tables["pit_receipts"]
    assert pit_summary(frame.loc[frame.region_id.eq("FNS-16")])["yoy_pct"] is None


def test_candidate_rejects_orphan_sources(monkeypatch):
    import pandas as pd
    original = pd.read_parquet

    def altered(path):
        frame = original(path)
        frame.loc[0, "source_id"] = "missing_source"
        return frame

    monkeypatch.setattr(pd, "read_parquet", altered)
    with pytest.raises(ValueError, match="orphan"):
        load_macro_candidate(CANDIDATE)


def test_macro_preview_pages_and_region_switch(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("MACRO_RUS_MACRO_CANDIDATE_DIR", str(CANDIDATE))
    monkeypatch.setenv("MACRO_RUS_FISCAL_CANDIDATE_DIR", str(ROOT / "outputs/regional_concordance_2026-09-29_v2/fiscal_preview"))
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    assert not app.exception
    app.sidebar.selectbox[0].set_value("FNS-16").run()
    assert not app.exception
    assert "Industry PIT" in app.radio[0].options
    assert len(app.sidebar.selectbox[0].options) == 78
    first = app.metric[0].value
    for page in app.radio[0].options:
        app.radio[0].set_value(page).run()
        assert not app.exception, page
    app.radio[0].set_value("Macro overview").run()
    app.sidebar.selectbox[0].set_value("FNS-77").run()
    assert not app.exception
    assert app.metric[0].value != first
    assert any("Mining PIT" in item.value for item in app.warning)
