"""Shared canonical table contracts.

All ingestion, analytical, and UI modules communicate through these column
sets. Raw-source-specific names must not leak beyond ingestion adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PIT_COLUMNS = (
    "region_id",
    "region_name_en",
    "region_name_ru",
    "reporting_cutoff",
    "period",
    "frequency",
    "okved_raw",
    "okved_section",
    "industry_name_en",
    "industry_name_ru",
    "pit_ytd_rub",
    "pit_flow_rub",
    "cpi_index",
    "source_id",
    "source_vintage",
    "revision_status",
    "quality_status",
    "is_official",
)

INDUSTRIAL_PRODUCTION_COLUMNS = (
    "region_id",
    "region_name_en",
    "region_name_ru",
    "period",
    "frequency",
    "okved_section",
    "industry_name_en",
    "industry_name_ru",
    "index_measure",
    "index_value",
    "index_base",
    "source_id",
    "source_vintage",
    "revision_status",
    "quality_status",
    "is_official",
)

BUDGET_COLUMNS = (
    "region_id",
    "region_name_en",
    "region_name_ru",
    "period",
    "frequency",
    "budget_level",
    "revenue_code",
    "revenue_name_en",
    "revenue_name_ru",
    "actual_ytd_rub",
    "approved_plan_rub",
    "revised_plan_rub",
    "source_id",
    "source_vintage",
    "revision_status",
    "quality_status",
    "is_official",
)

REGION_COLUMNS = (
    "region_id",
    "region_name_en",
    "region_name_ru",
    "fns_code",
    "okato_code",
    "federal_district",
    "economic_profile",
    "coverage_start",
    "coverage_end",
)

SOURCE_COLUMNS = (
    "source_id",
    "publisher",
    "dataset_name",
    "source_url",
    "local_path",
    "retrieved_at_utc",
    "reporting_cutoff",
    "publication_date",
    "units",
    "sha256",
    "file_size_bytes",
    "is_official",
    "notes",
)

QUALITY_EVENT_COLUMNS = (
    "check_id",
    "table_name",
    "region_id",
    "period",
    "severity",
    "status",
    "message",
    "observed_value",
    "expected_value",
    "source_vintage",
)

TABLE_CONTRACTS = {
    "pit_receipts": PIT_COLUMNS,
    "industrial_production": INDUSTRIAL_PRODUCTION_COLUMNS,
    "budget_execution": BUDGET_COLUMNS,
    "regions": REGION_COLUMNS,
    "sources": SOURCE_COLUMNS,
    "quality_events": QUALITY_EVENT_COLUMNS,
}

TABLE_FILES = {
    "pit_receipts": "pit_receipts.parquet",
    "industrial_production": "industrial_production.parquet",
    "budget_execution": "budget_execution.parquet",
    "regions": "regions.parquet",
    "sources": "sources.parquet",
    "quality_events": "quality_events.parquet",
}

ALLOWED_FREQUENCIES = {"monthly", "quarterly", "annual"}
ALLOWED_QUALITY_STATUSES = {
    "warning",  # Review candidates with documented comparability/coverage limits.
    "verified",
    "provisional",
    "revised",
    "scope_mismatch",
    "missing_suppressed",
    "failed",
    "demo",
}
ALLOWED_REVISION_STATUSES = {"original", "corrected", "latest", "demo"}
INDUSTRIAL_OKVED_SECTIONS = {"B", "C", "D", "E"}


@dataclass(frozen=True)
class DatasetPaths:
    """Resolved locations for one promoted dataset vintage."""

    root: Path

    def table(self, name: str) -> Path:
        if name not in TABLE_FILES:
            raise KeyError(f"Unknown canonical table: {name}")
        return self.root / TABLE_FILES[name]

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"
