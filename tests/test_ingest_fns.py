from pathlib import Path

import pandas as pd

from macro_rus.ingest_fns import ingest_fns_result


def test_fns_uses_top_level_sections_and_converts_thousand_rub(tmp_path: Path):
    schema = pd.DataFrame(
        [
            ("GA", "Code", "Код субъекта", "text"),
            ("GB", "Name", "Наименование субъекта", "text"),
            ("G1", "A total PIT", "А 01-03 Сельское хозяйство – всего налог на доходы физических лиц всего", "integer"),
            ("G2", "A child PIT", "А 01 Растениеводство налог на доходы физических лиц всего", "integer"),
            ("G3", "C total PIT", "С 10-33 Обрабатывающие производства – всего налог на доходы физических лиц всего", "integer"),
            ("G4", "Unmapped PIT", "Суммы налогов, не распределенные по кодам ОКВЭД налог на доходы физических лиц всего", "integer"),
        ],
        columns=["field name", "english description", "russian description", "format"],
    )
    schema.to_csv(tmp_path / "structure-20260401.csv", index=False)
    pd.DataFrame(
        [{"GA": "40", "GB": "Калужская область", "G1": 10, "G2": 7, "G3": 20, "G4": 1}]
    ).to_csv(tmp_path / "data-20260401-structure-20260401.csv", index=False)

    result = ingest_fns_result(tmp_path)
    pit = result.pit_receipts
    assert set(pit["okved_section"]) == {"A", "C", "UNMAPPED"}
    assert pit["pit_ytd_rub"].sum() == 31_000
    assert pit["pit_flow_rub"].sum() == 31_000  # Q1 YTD equals Q1 flow.


def test_fns_skips_release_without_exact_schema(tmp_path: Path):
    (tmp_path / "data-20250401-structure-20250401.csv").write_text("GA,GB,G1\n40,Калужская область,1\n")
    result = ingest_fns_result(tmp_path)
    assert result.pit_receipts.empty
    assert "missing structure-20250401.csv" in result.skipped_files[0]


def test_fns_accepts_explicit_mapping_and_unpadded_source_code(tmp_path):
    test_fns_uses_top_level_sections_and_converts_thousand_rub(tmp_path)
    path = tmp_path / "data-20260401-structure-20260401.csv"
    data = pd.read_csv(path)
    data["GA"] = 1
    data["GB"] = "Source-specific label"
    data.to_csv(path, index=False)
    result = ingest_fns_result(tmp_path, region_records=[{
        "region_id": "FNS-01", "fns_code": "01", "region_name_ru": "Canonical label", "region_name_en": "",
    }])
    assert set(result.pit_receipts.region_id) == {"FNS-01"}
    assert result.pit_receipts.pit_ytd_rub.sum() == 31000


def test_fns_ignores_browser_copy_filenames_as_non_releases(tmp_path: Path):
    (tmp_path / "data-20250401-structure-20250401 copy.csv").write_text(
        "GA,GB,G1\n40,Калужская область,1\n"
    )
    result = ingest_fns_result(tmp_path)
    assert result.pit_receipts.empty
    assert result.skipped_files == ()


def test_fns_prefers_canonical_filename_over_numbered_duplicate(tmp_path: Path):
    schema = pd.DataFrame(
        [
            ("GA", "Code", "Код субъекта", "text"),
            ("GB", "Name", "Наименование субъекта", "text"),
            (
                "G1",
                "C PIT",
                "С 10-33 Обрабатывающие производства – всего налог на доходы физических лиц всего",
                "integer",
            ),
        ],
        columns=["field name", "english description", "russian description", "format"],
    )
    schema.to_csv(tmp_path / "structure-20250401.csv", index=False)
    data = pd.DataFrame([{"GA": "40", "GB": "Калужская область", "G1": 10}])
    canonical = tmp_path / "data-20250401-structure-20250401.csv"
    duplicate = tmp_path / "data-20250401-structure-20250401 (1).csv"
    data.to_csv(canonical, index=False)
    duplicate.write_bytes(canonical.read_bytes())

    result = ingest_fns_result(tmp_path)

    assert result.source_metadata.loc[0, "local_path"].endswith(canonical.name)
    assert any(
        duplicate.name in message and "duplicate content" in message
        for message in result.skipped_files
    )
