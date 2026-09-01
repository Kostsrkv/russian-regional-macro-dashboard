from pathlib import Path

import pandas as pd

from macro_rus.contracts import TABLE_CONTRACTS, TABLE_FILES
from macro_rus.pipeline import promote
from macro_rus.validation import ValidationResult


def test_promote_writes_all_contract_tables_and_manifest(tmp_path: Path):
    tables = {
        name: pd.DataFrame(columns=columns)
        for name, columns in TABLE_CONTRACTS.items()
    }
    quality = pd.DataFrame(
        [
            {
                "check_id": "mixed_evidence",
                "table_name": "pit_receipts",
                "region_id": None,
                "period": None,
                "severity": "medium",
                "status": "warning",
                "message": "Mixed evidence remains serializable.",
                "observed_value": 1,
                "expected_value": "one",
                "source_vintage": None,
            }
        ],
        columns=TABLE_CONTRACTS["quality_events"],
    )
    tables["quality_events"] = quality
    validation = ValidationResult(quality)
    result = promote(tables, tmp_path / "vintage", validation)
    assert result.manifest["validation"]["blocking"] == 0
    for filename in TABLE_FILES.values():
        assert (tmp_path / "vintage" / filename).exists()
    assert (tmp_path / "vintage" / "manifest.json").exists()


def test_promote_strips_absolute_source_paths(tmp_path: Path):
    tables = {name: pd.DataFrame(columns=columns) for name, columns in TABLE_CONTRACTS.items()}
    source = {column: None for column in TABLE_CONTRACTS["sources"]}
    source.update(
        {
            "source_id": "source",
            "publisher": "Publisher",
            "dataset_name": "Dataset",
            "local_path": "/Users/analyst/private/raw.csv",
            "sha256": "a" * 64,
            "is_official": True,
        }
    )
    tables["sources"] = pd.DataFrame([source])
    validation = ValidationResult(pd.DataFrame(columns=TABLE_CONTRACTS["quality_events"]))
    promote(tables, tmp_path / "vintage", validation)
    promoted = pd.read_parquet(tmp_path / "vintage" / TABLE_FILES["sources"])
    assert promoted.loc[0, "local_path"] == "raw.csv"
