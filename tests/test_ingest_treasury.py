from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd
import pytest

from macro_rus.contracts import BUDGET_COLUMNS, QUALITY_EVENT_COLUMNS, SOURCE_COLUMNS
from macro_rus.ingest_treasury import (
    TreasuryIngestionError,
    ingest_treasury_archive,
    ingest_treasury_archives,
)


REGIONS = {
    "Калужская область": "37",
    "Свердловская область": "62",
}

VALUES = {
    "37": {
        "TOTAL_REVENUE": ("144359542097.87", "30297494075.96"),
        "10000000000000000": ("129214085103.79", "27423271799.37"),
        "10101000000000110": ("28854687934.09", "6798269079.43"),
        "10102000010000110": ("54402653525.00", "11713402015.01"),
        "20000000000000000": ("15145456994.08", "2874222276.59"),
    },
    "62": {
        "TOTAL_REVENUE": ("646095974880.01", "130844350842.80"),
        "10000000000000000": ("607877940452.50", "121203774561.35"),
        "10101000000000110": ("142170836000.00", "35023294249.05"),
        "10102000010000110": ("272476727617.87", "49354426907.83"),
        "20000000000000000": ("38218034427.51", "9640576281.45"),
    },
}


METRIC_ROWS = {
    "TOTAL_REVENUE": ("Доходы бюджета - всего", ["***", "850", "00000", "00", "0000", "000"]),
    "10000000000000000": (
        "НАЛОГОВЫЕ И НЕНАЛОГОВЫЕ ДОХОДЫ",
        ["000", "100", "00000", "00", "0000", "000"],
    ),
    "10101000000000110": (
        "Налог на прибыль организаций",
        ["000", "101", "01000", "00", "0000", "110"],
    ),
    "10102000010000110": (
        "Налог на доходы физических лиц",
        ["000", "101", "02000", "01", "0000", "110"],
    ),
    "20000000000000000": (
        "БЕЗВОЗМЕЗДНЫЕ ПОСТУПЛЕНИЯ",
        ["000", "200", "00000", "00", "0000", "000"],
    ),
}


def _report_html(values: dict[str, tuple[str, str]]) -> bytes:
    rows = []
    for code, (plan, actual) in values.items():
        name, kbk = METRIC_ROWS[code]
        cells = [name, "010", *kbk]
        cells.extend(["0"] * (10 - len(cells)))
        cells.append(plan)  # zero-based consolidated-budget plan cell 10
        cells.extend(["0"] * (24 - len(cells)))
        cells.append(actual)  # zero-based consolidated-budget actual cell 24
        rows.append("<tr>" + "".join(f"<td>{value}</td>" for value in cells) + "</tr>")
    return ("<html><table>" + "".join(rows) + "</table></html>").encode("cp1251")


def _archive(tmp_path: Path, *, cutoff: str = "01.04.2026", missing: str | None = None) -> Path:
    catalog_entries = []
    nested_buffer = BytesIO()
    with ZipFile(nested_buffer, "w", ZIP_DEFLATED) as nested:
        for name, number in REGIONS.items():
            if name == missing:
                continue
            filename = f"Mes_428m_{number}.html"
            catalog_entries.append(f'<li><a href=".\\{filename}">{name}</a>')
            nested.writestr(filename, _report_html(VALUES[number]))
        catalogue = "<html>" + "".join(catalog_entries) + "</html>"
        nested.writestr("TerrList_428m.html", catalogue.encode("cp1251"))

    path = tmp_path / f"КБСРФ на {cutoff}.zip"
    with ZipFile(path, "w", ZIP_DEFLATED) as outer:
        outer.writestr("unrelated.zip", b"not used")
        outer.writestr(f"f. 0503317_{cutoff}_html.zip", nested_buffer.getvalue())
    return path


def test_ingests_canonical_budget_contract_and_catalogue(tmp_path: Path) -> None:
    result = ingest_treasury_archive(
        _archive(tmp_path), retrieved_at_utc="2026-05-25T09:00:00Z"
    )

    assert tuple(result.budget_execution.columns) == BUDGET_COLUMNS
    assert tuple(result.source_metadata.columns) == SOURCE_COLUMNS
    assert tuple(result.quality_events.columns) == QUALITY_EVENT_COLUMNS
    assert len(result.budget_execution) == 10
    assert set(result.region_catalog) == set(REGIONS)
    assert result.region_catalog["Калужская область"] == "Mes_428m_37.html"
    assert not result.budget_execution.duplicated(
        ["region_id", "period", "budget_level", "revenue_code", "source_vintage"]
    ).any()
    assert result.budget_execution["revised_plan_rub"].isna().all()
    assert set(result.budget_execution["quality_status"]) == {"verified"}
    assert set(result.budget_execution["period"]) == {pd.Timestamp("2026-03-31")}

    kaluga_pit = result.budget_execution.query(
        "region_id == 'RU-KLU' and revenue_code == '10102000010000110'"
    ).iloc[0]
    assert kaluga_pit["approved_plan_rub"] == pytest.approx(54_402_653_525.00)
    assert kaluga_pit["actual_ytd_rub"] == pytest.approx(11_713_402_015.01)
    assert result.source_metadata.iloc[0]["retrieved_at_utc"] == pd.Timestamp(
        "2026-05-25T09:00:00Z"
    )


def test_reconciles_top_level_revenue_components(tmp_path: Path) -> None:
    result = ingest_treasury_archive(_archive(tmp_path))
    checks = result.quality_events.query("check_id == 'revenue_components_reconcile'")
    assert len(checks) == 2
    assert set(checks["status"]) == {"passed"}


def test_catalogue_mapping_not_filename_region_code(tmp_path: Path) -> None:
    result = ingest_treasury_archive(_archive(tmp_path), regions=["Калужская область"])
    assert set(result.budget_execution["region_id"]) == {"RU-KLU"}
    # Kaluga is report sequence 37 here, not its tax/vehicle code 40.
    assert result.region_catalog == {"Калужская область": "Mes_428m_37.html"}


def test_missing_requested_region_is_a_hard_failure(tmp_path: Path) -> None:
    path = _archive(tmp_path, missing="Свердловская область")
    with pytest.raises(TreasuryIngestionError, match="missing from territory catalogue"):
        ingest_treasury_archive(path)


def test_combines_quarterly_vintages(tmp_path: Path) -> None:
    q1 = _archive(tmp_path, cutoff="01.04.2026")
    q2 = _archive(tmp_path, cutoff="01.07.2026")
    result = ingest_treasury_archives([q1, q2])
    assert len(result.budget_execution) == 20
    assert set(result.budget_execution["period"]) == {
        pd.Timestamp("2026-03-31"),
        pd.Timestamp("2026-06-30"),
    }
    assert len(result.source_metadata) == 2
    assert result.source_metadata["source_id"].is_unique
    assert set(result.budget_execution["source_id"]) == set(
        result.source_metadata["source_id"]
    )


def test_quarantines_corrupt_archive_when_refresh_continues(tmp_path: Path) -> None:
    valid = _archive(tmp_path, cutoff="01.04.2026")
    corrupt = tmp_path / "KBSRF-na-01.07.2025.zip"
    corrupt.write_bytes(b"truncated")

    result = ingest_treasury_archives([valid, corrupt], continue_on_error=True)

    assert len(result.budget_execution) == 10
    quarantined = result.quality_events.query(
        "check_id == 'treasury_archive_quarantined'"
    )
    assert len(quarantined) == 1
    assert quarantined.iloc[0]["severity"] == "high"
    assert "KBSRF-na-01.07.2025.zip" in quarantined.iloc[0]["message"]
