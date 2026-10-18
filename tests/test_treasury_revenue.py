from html import escape
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from macro_rus.treasury_revenue import parse_revenue, REVENUE_METRICS
from macro_rus.treasury_fiscal import PLAN, ACTUAL
from macro_rus.ingest_treasury import TreasuryIngestionError
from macro_rus.revenue_view import revenue_indicators, load_revenue_candidate, PIT, OWN, TRANSFERS

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "outputs/regional_revenue_candidate_2026-09-30"


def source_report(plan_total="100", wrong_scope=False, duplicate=False):
    # Moving both amount columns verifies selection by header, not fixed offsets.
    header = [""] * 30
    header[0] = "Наименование показателя"
    header[9] = PLAN
    header[21] = ACTUAL if not wrong_scope else "Исполнено: бюджет субъекта РФ"
    rows = [header]
    for code, amount in zip(REVENUE_METRICS, (100, 80, 20, 30, 20, 15)):
        row = ["0"] * 30
        row[:3] = ["Доходы бюджета - всего" if code == "TOTAL_REVENUE" else REVENUE_METRICS[code], "010", "***" if code == "TOTAL_REVENUE" else "000"]
        if code != "TOTAL_REVENUE":
            row[3:8] = [code[:3], code[3:8], code[8:10], code[10:14], code[14:]]
        row[9] = plan_total if code == "TOTAL_REVENUE" else str(amount)
        row[21] = str(amount)
        rows.append(row)
        child = row.copy()
        child[2] = "182"  # Administrator detail must not duplicate parent.
        rows.append(child)
        if duplicate and code == PIT:
            rows.append(row)
    return ("<table>" + "".join("<tr>" + "".join("<td>" + escape(c) + "</td>" for c in r) + "</tr>" for r in rows) + "</table>").encode("cp1251")


def test_parent_scope_columns_and_real_transfer_category():
    records, checks = parse_revenue(source_report())
    assert len(records) == 6
    assert all(r["actual_column_index"] == 21 for r in records)
    assert next(r for r in records if r["revenue_code"] == TRANSFERS)["actual_ytd_rub"] == Decimal(15)
    assert all(c["passed"] for c in checks)


def test_source_plan_discrepancy_retains_actuals():
    _, checks = parse_revenue(source_report(plan_total="120"))
    assert all(c["passed"] for c in checks if c["blocking"])
    assert any(not c["passed"] and not c["blocking"] for c in checks)


def test_wrong_scope_and_duplicate_fail():
    with pytest.raises(TreasuryIngestionError, match="perimeter"):
        parse_revenue(source_report(wrong_scope=True))
    with pytest.raises(TreasuryIngestionError, match="Duplicate"):
        parse_revenue(source_report(duplicate=True))


def test_ytd_differences_preserve_negative_adjustments_and_missing_quarters():
    frame = pd.DataFrame([dict(region_id="R", period=pd.Timestamp(p), revenue_code=PIT,
                               budget_level="consolidated_regional_budget", actual_ytd_rub=v,
                               approved_plan_rub=0, plan_components_reconciled=False)
                          for p, v in [("2025-03-31", 100), ("2025-06-30", 90), ("2025-12-31", 400),
                                       ("2026-03-31", 120), ("2026-06-30", 180)]])
    data = revenue_indicators(frame).set_index("period")
    assert data.loc["2025-06-30", "quarter_flow_rub"] == -10
    assert pd.isna(data.loc["2025-12-31", "quarter_flow_rub"])
    assert data.loc["2026-03-31", "quarter_flow_rub"] == 120
    assert data.loc["2026-06-30", "growth_yoy_pct"] == pytest.approx(100)
    assert data.snapshot_execution_pct.isna().all()


@pytest.mark.private_data("outputs/regional_revenue_candidate_2026-09-30",
                          "outputs/regional_concordance_2026-09-29_v2/fiscal_preview")
def test_existing_archive_candidate_matches_fiscal_totals():
    tables, manifest = load_revenue_candidate(CANDIDATE)
    budget = tables["budget_execution"]
    assert manifest["region_periods"] == 624
    assert budget.region_id.nunique() == 78
    fiscal = pd.read_csv(ROOT / "outputs/regional_concordance_2026-09-29_v2/fiscal_preview/fiscal_observations.csv")
    actuals = fiscal.loc[fiscal.metric_code.eq("revenue_total")]
    merged = actuals.merge(budget.loc[budget.revenue_code.eq("TOTAL_REVENUE")].assign(period=lambda d: d.period.dt.strftime("%Y-%m-%d")),
                           on=["region_id", "period"], suffixes=("_fiscal", "_revenue"), validate="one_to_one")
    assert len(merged) == 624
    assert (merged.actual_ytd_rub_fiscal - merged.actual_ytd_rub_revenue).abs().le(.02).all()
    assert not set(budget.region_id) & {"FNS-72", "FNS-91"}


@pytest.mark.private_data("outputs/regional_revenue_candidate_2026-09-30",
                          "outputs/expanded_macro_candidate_2026-09-29_v2")
def test_revenue_ui_period_region_and_plan_warning(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("MACRO_RUS_REVENUE_CANDIDATE_DIR", str(CANDIDATE))
    monkeypatch.setenv("MACRO_RUS_MACRO_CANDIDATE_DIR", str(ROOT / "outputs/expanded_macro_candidate_2026-09-29_v2"))
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=25).run()
    app.sidebar.selectbox[0].set_value("FNS-16").run()
    assert not app.exception
    assert "Retained PIT" in app.metric[2].label
    app.radio[0].set_value("Fiscal execution").run()
    assert not app.exception
    value = app.metric[0].value
    next(w for w in app.selectbox if w.label == "Revenue reporting period").set_value(pd.Timestamp("2023-03-31")).run()
    assert not app.exception
    assert app.metric[0].value != value
    app.sidebar.selectbox[0].set_value("FNS-63").run()
    assert not app.exception
    next(w for w in app.selectbox if w.label == "Revenue reporting period").set_value(pd.Timestamp("2023-03-31")).run()
    assert any("do not reconcile" in w.value for w in app.warning)
