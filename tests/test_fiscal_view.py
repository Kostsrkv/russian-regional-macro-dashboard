from pathlib import Path
import sys
import shutil

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from macro_rus.fiscal_view import load_candidate, regional_snapshot, period_label

CANDIDATE = ROOT / "outputs/treasury_fiscal_candidate"
pytestmark = pytest.mark.private_data("outputs/treasury_fiscal_candidate")


def test_candidate_and_filters():
    data, manifest = load_candidate(CANDIDATE)
    assert len(data) == 371
    selected = regional_snapshot(data, "RU-SVE", "2026-06-30")
    assert set(selected.region_id) == {"RU-SVE"}
    assert selected.period.nunique() == 1
    assert "cumulative" in period_label("2026-06-30")
    assert manifest["blocking_failures"] == 0


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "imbalance", "units"])
def test_reject_invalid_candidate(tmp_path, mutation):
    shutil.copytree(CANDIDATE, tmp_path / "candidate")
    path = tmp_path / "candidate/fiscal_observations.csv"
    data = pd.read_csv(path, dtype={"metric_code": str})
    if mutation == "duplicate":
        data.iloc[1] = data.iloc[0]
    elif mutation == "missing":
        data.loc[0, "actual_ytd_rub"] = float("nan")
    elif mutation == "imbalance":
        data.loc[0, "actual_ytd_rub"] += 100
    else:
        data.loc[0, "units"] = "thousand RUB"
    data.to_csv(path, index=False)
    with pytest.raises(ValueError):
        load_candidate(tmp_path / "candidate")


def test_streamlit_preview(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("MACRO_RUS_FISCAL_CANDIDATE_DIR", str(CANDIDATE))
    app = AppTest.from_file(str(ROOT / "app.py")).run()
    app.radio[0].set_value("Spending & financing (preview)").run()
    assert not app.exception
    assert len(app.metric) == 3
    before = app.metric[0].value
    app.sidebar.selectbox[0].set_value("RU-SVE").run()
    assert not app.exception
    assert app.metric[0].value != before
    app.selectbox[0].select_index(1).run()
    assert not app.exception


@pytest.mark.private_data("data/promoted/current")
def test_preview_opt_in_only(monkeypatch):
    from streamlit.testing.v1 import AppTest
    # The checked-in public research release now enables fiscal views by
    # default. Test the explicit legacy/local opt-in contract, not that release.
    monkeypatch.setenv("MACRO_RUS_DATA_DIR", str(ROOT / "data/promoted/current"))
    monkeypatch.delenv("MACRO_RUS_FISCAL_CANDIDATE_DIR", raising=False)
    app = AppTest.from_file(str(ROOT / "app.py")).run()
    assert "Spending & financing (preview)" not in app.radio[0].options
