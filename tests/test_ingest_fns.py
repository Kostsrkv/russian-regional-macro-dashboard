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

