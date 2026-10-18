import importlib.util
from pathlib import Path
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("concordance", ROOT / "scripts/build_regional_concordance.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_normalization_preserves_scope():
    assert module.normalize("г.Москва") == module.normalize("г. Москва")
    assert module.normalize("Тюменская область") != module.normalize("Тюменская область без авт. округов")
    assert module.normalize("Республика Марий-Эл") == module.normalize("Республика Марий Эл")


def test_collision_fails_closed():
    with pytest.raises(ValueError, match="Ambiguous"):
        module.unique_lookup([("г.Москва", 1), ("г. Москва", 2)])


def test_pit_growth_suppressed_when_mining_disappears():
    from macro_rus.dashboard import pit_summary
    frame = pd.DataFrame([
        {"period": pd.Timestamp("2025-03-31"), "okved_section": "B", "pit_flow_rub": 50},
        {"period": pd.Timestamp("2025-03-31"), "okved_section": "C", "pit_flow_rub": 100},
        {"period": pd.Timestamp("2026-03-31"), "okved_section": "C", "pit_flow_rub": 120},
    ])
    summary = pit_summary(frame)
    assert summary["yoy_pct"] is None
    assert summary["coverage_changed"]
    assert summary["missing_sections"] == ["B"]
    assert summary["value"] == 120


@pytest.mark.private_data("outputs/regional_concordance_2026-09-29_v2/fiscal_preview")
def test_expanded_preview_region_and_values(monkeypatch):
    from streamlit.testing.v1 import AppTest
    from macro_rus.fiscal_view import load_candidate, regional_snapshot
    path = ROOT / "outputs/regional_concordance_2026-09-29_v2/fiscal_preview"
    data, _ = load_candidate(path)
    monkeypatch.setenv("MACRO_RUS_FISCAL_CANDIDATE_DIR", str(path))
    app = AppTest.from_file(str(ROOT / "app.py")).run()
    app.sidebar.selectbox[0].set_value("FNS-16").run()
    assert not app.exception
    assert app.radio[0].options == ["Spending & financing (preview)"]
    selected = regional_snapshot(data, "FNS-16", "2026-06-30")
    revenue = selected.loc[selected.metric_code.eq("revenue_total"), "actual_ytd_rub"].item()
    assert app.metric[0].value == f"{revenue/1e9:,.2f} bn RUB"
    assert len(data.region_id.unique()) == 78
    assert "FNS-72" not in set(data.region_id)
    assert "FNS-91" not in set(data.region_id)
