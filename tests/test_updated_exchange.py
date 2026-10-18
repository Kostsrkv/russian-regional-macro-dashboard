"""Portable review package content, lineage, execution and archive contracts."""
from pathlib import Path
import hashlib
import importlib.util
import json
import re
from zipfile import ZipFile

import nbformat
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "outputs/research_exchange_2026-10-02"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(name):
    return pd.read_csv(PACKAGE / (name + ".csv"), dtype={"region_id":str,
        "fns_code":str, "metric_code":str, "revenue_code":str}, low_memory=False)


@pytest.mark.private_data("outputs/research_exchange_2026-10-02")
def test_exchange_file_and_archive_integrity_without_self_reference():
    manifest = json.loads((PACKAGE / "manifest.json").read_text())
    sidecar = json.loads(PACKAGE.with_suffix(".integrity.json").read_text())
    assert manifest["status"] == sidecar["status"] == "candidate_not_promoted"
    assert not manifest["raw_files_included"] and not manifest["shared_externally"]
    assert "manifest.json" not in manifest["files"]
    actual = {p.name:digest(p) for p in PACKAGE.iterdir() if p.is_file()}
    assert sidecar["files"] == actual
    assert manifest["files"] == {name:value for name,value in actual.items() if name != "manifest.json"}
    assert sidecar["archive_sha256"] == digest(PACKAGE.with_suffix(".zip"))
    assert sidecar["manifest_sha256"] == actual["manifest.json"]
    with ZipFile(PACKAGE.with_suffix(".zip")) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {PACKAGE.name + "/" + name for name in actual}
        assert all(hashlib.sha256(archive.read(PACKAGE.name + "/" + name)).hexdigest() == value for name,value in actual.items())
        assert not any(name.endswith(".integrity.json") for name in archive.namelist())


@pytest.mark.private_data("outputs/research_exchange_2026-10-02", "outputs/research_exchange_2026-09-30")
def test_every_baseline_csv_preserved_and_all_fields_documented():
    for path in (ROOT / "outputs/research_exchange_2026-09-30").glob("*.csv"):
        assert digest(path) == digest(PACKAGE / path.name), path.name
    dictionary = load("field_dictionary")
    expected = {(path.stem, field) for path in PACKAGE.glob("*.csv")
                for field in pd.read_csv(path, nrows=0).columns}
    assert set(zip(dictionary.table, dictionary.field)) == expected
    assert not dictionary.duplicated(["table","field"]).any()
    assert dictionary.definition.notna().all() and dictionary.definition.str.len().ge(12).all()
    for table in ["industrial_production_annual","industrial_production_cumulative","annual_vintage_comparison"]:
        definitions = dictionary.loc[dictionary.table.eq(table)].set_index("field").definition
        assert "minus 100" in definitions.loc["growth_yoy_pct" if "growth_yoy_pct" in definitions.index else "growth_yoy_pct_later"]
        assert "workbook" in definitions.loc["source_sha256" if "source_sha256" in definitions.index else "source_sha256_later"]
    assert "period_flow" in dictionary.loc[dictionary.field.eq("amount_basis"),"definition"].iloc[0]


@pytest.mark.private_data("outputs/research_exchange_2026-10-02")
def test_canonical_keys_scope_missingness_and_cell_lineage():
    regions = load("regions")
    canonical = set(regions.region_id)
    assert len(canonical) == 78 and {"RU-KLU","RU-SVE","FNS-77"}.issubset(canonical)
    assert not regions.eligible_for_national_aggregation.any()
    assert regions.fns_code.str.len().eq(2).all()
    manifest = json.loads((PACKAGE / "manifest.json").read_text())
    for table, metadata in manifest["tables"].items():
        frame = load(table)
        assert len(frame) == metadata["rows"]
        if metadata["grain"]:
            assert not frame.duplicated(metadata["grain"]).any()
            assert set(frame.region_id) == canonical
    annual, cumulative = load("industrial_production_annual"), load("industrial_production_cumulative")
    assert len(annual) == 4290 and annual.index_value.isna().sum() == 11
    assert len(cumulative) == 54210 and cumulative.index_value.isna().sum() == 144
    for frame in (annual,cumulative):
        assert frame[["source_file","source_sha256","source_sheet","source_cell"]].notna().all().all()
        assert frame.source_sha256.str.fullmatch(r"[0-9a-f]{64}").all()
        assert np.allclose(frame.growth_yoy_pct, frame.index_value-100, equal_nan=True)
    matched = load("matched_pit_production")
    assert len(matched) == 936
    assert matched.loc[matched.period.eq("2026-03-31") & matched.okved_section.eq("B"),"pit_current_rub"].isna().all()
    assert matched.loc[matched.period.eq("2026-03-31"),"pit_current_source_sha256"].notna().all()
    cells = load("source_cell_spotchecks")
    assert cells.passed.all() and cells.hash_verified.all()
    assert set(cells.region_id) == {"RU-KLU","RU-SVE"}
    assert np.allclose(cells.raw_numeric_value*cells.scale_to_output, cells.output_value, atol=.02, rtol=0)
    schema_cells = cells.loc[cells.table.eq("pit_receipts")]
    assert schema_cells.source_schema_sha256.str.fullmatch(r"[0-9a-f]{64}").all()
    assert all(digest(ROOT / row.source_schema_file) == row.source_schema_sha256 for row in schema_cells.itertuples())


@pytest.mark.private_data("outputs/research_exchange_2026-10-02")
def test_fiscal_basis_and_same_window_reconciliation():
    flows = load("fiscal_flows")
    q2 = flows.loc[flows.window.eq("Q2")]
    assert np.allclose(q2.quarter_flow_rub, q2.actual_ytd_rub-q2.start_actual_ytd_rub, atol=.02, rtol=0, equal_nan=True)
    complete_q2 = q2.loc[q2.quarter_flow_rub.notna()]
    assert complete_q2[["start_source_sha256","end_source_sha256","start_source_member","end_source_member"]].notna().all().all()
    missing = q2.loc[q2.quarter_flow_rub.isna()]
    assert len(missing) == 7 and missing.flow_status.eq("missing endpoint").all()
    assert missing.start_actual_ytd_rub.isna().all()
    spending = load("spending_function_history")
    assert spending.groupby(["region_id","period","amount_basis"]).size().eq(14).all()
    assert spending.spending_reconciliation_gap_rub.abs().le(.02).all()
    complete = spending.loc[spending.comparison_complete]
    assert complete.change_reconciliation_gap_rub.abs().le(.02).all()
    bridges = load("budget_balance_bridges")
    assert len(bridges) == 3744
    assert np.allclose(bridges.start_balance_rub+bridges.revenue_change_rub-bridges.expenditure_change_rub,
        bridges.end_balance_rub, atol=.02, rtol=0)
    transfers = load("transfer_history")
    assert np.allclose(transfers.transfer_share_pct, transfers.transfers_rub/transfers.total_revenue_rub*100)


@pytest.mark.private_data("outputs/research_exchange_2026-10-02")
def test_saved_notebook_is_valid_executed_and_portable():
    notebook = nbformat.read(PACKAGE / "combined_source_metric_audit.ipynb", as_version=4)
    nbformat.validate(notebook)
    code_cells = [cell for cell in notebook.cells if cell.cell_type == "code"]
    assert len(code_cells) >= 10
    assert all(cell.execution_count is not None and cell.outputs for cell in code_cells)
    assert not any(out.output_type == "error" for cell in code_cells for out in cell.outputs)
    assert "PACKAGE_ROOT = discover_package()" in code_cells[0].source
    assert (PACKAGE / "combined_source_metric_audit.html").stat().st_size > 10000
    assert json.loads((PACKAGE / "manifest.json").read_text())["notebook_executed"]


@pytest.mark.private_data("outputs/research_exchange_2026-10-02")
def test_no_raw_files_credentials_or_machine_paths():
    forbidden = re.compile(r"(?:/Users/|/home/|/private/|/tmp/|[A-Za-z]:(?:\\|/)+(?:Users|Windows|Documents|Temp)\b|file://|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{8,})", re.I)
    allowed = {".csv",".json",".ipynb",".html",".md",".py"}
    for path in PACKAGE.iterdir():
        assert path.suffix in allowed, path.name
        assert not forbidden.search(path.read_text(encoding="utf-8-sig")), path.name
    assert not forbidden.search(PACKAGE.with_suffix(".integrity.json").read_text())


def test_builder_refuses_existing_output(tmp_path):
    spec = importlib.util.spec_from_file_location("updated_exchange_builder", ROOT / "scripts/build_updated_exchange.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError, match="fresh"):
        builder.run(existing)
