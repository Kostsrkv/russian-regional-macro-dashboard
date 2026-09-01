import pandas as pd

from macro_rus.contracts import BUDGET_COLUMNS
from macro_rus.validation import validate_tables


def _budget_row():
    values = {column: None for column in BUDGET_COLUMNS}
    values.update(
        {
            "region_id": "RU-KLU",
            "region_name_en": "Kaluga Oblast",
            "region_name_ru": "Калужская область",
            "period": pd.Timestamp("2026-03-31"),
            "frequency": "quarterly",
            "budget_level": "consolidated_subject",
            "revenue_code": "10102000010000110",
            "revenue_name_en": "Personal income tax",
            "revenue_name_ru": "Налог на доходы физических лиц",
            "actual_ytd_rub": 10.0,
            "approved_plan_rub": 100.0,
            "source_id": "source",
            "source_vintage": "2026-04-01",
            "revision_status": "latest",
            "quality_status": "provisional",
            "is_official": True,
        }
    )
    return values


def test_validation_accepts_valid_budget_grain():
    result = validate_tables({"budget_execution": pd.DataFrame([_budget_row()])})
    assert result.is_valid


def test_validation_blocks_duplicate_budget_grain():
    row = _budget_row()
    result = validate_tables({"budget_execution": pd.DataFrame([row, row])})
    assert not result.is_valid
    assert "unique_grain" in set(result.blocking["check_id"])

