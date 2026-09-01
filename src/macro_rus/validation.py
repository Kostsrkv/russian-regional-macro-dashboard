"""Validation gates for canonical dashboard tables."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pandas as pd

from .contracts import (
    ALLOWED_FREQUENCIES,
    ALLOWED_QUALITY_STATUSES,
    INDUSTRIAL_OKVED_SECTIONS,
    QUALITY_EVENT_COLUMNS,
    TABLE_CONTRACTS,
)


GRAINS: dict[str, tuple[str, ...]] = {
    "pit_receipts": (
        "region_id",
        "reporting_cutoff",
        "okved_raw",
        "source_vintage",
    ),
    "industrial_production": (
        "region_id",
        "period",
        "okved_section",
        "index_measure",
        "source_vintage",
    ),
    "budget_execution": (
        "region_id",
        "period",
        "budget_level",
        "revenue_code",
        "source_vintage",
    ),
    "regions": ("region_id",),
    "sources": ("source_id",),
}

REQUIRED_NOT_NULL: dict[str, tuple[str, ...]] = {
    "pit_receipts": (
        "region_id",
        "reporting_cutoff",
        "period",
        "okved_raw",
        "pit_ytd_rub",
        "source_id",
        "source_vintage",
    ),
    "industrial_production": (
        "region_id",
        "period",
        "index_measure",
        "index_value",
        "source_id",
        "source_vintage",
    ),
    "budget_execution": (
        "region_id",
        "period",
        "budget_level",
        "revenue_code",
        "actual_ytd_rub",
        "source_id",
        "source_vintage",
    ),
    "regions": ("region_id", "region_name_en", "region_name_ru"),
    "sources": ("source_id", "publisher", "dataset_name", "local_path", "sha256"),
}


@dataclass(frozen=True)
class ValidationResult:
    events: pd.DataFrame

    @property
    def blocking(self) -> pd.DataFrame:
        if self.events.empty:
            return self.events
        return self.events[
            self.events["severity"].isin(["critical", "high"])
            & self.events["status"].eq("failed")
        ]

    @property
    def is_valid(self) -> bool:
        return self.blocking.empty


def _event(
    check_id: str,
    table_name: str,
    *,
    severity: str,
    status: str,
    message: str,
    observed_value: object = None,
    expected_value: object = None,
    region_id: object = None,
    period: object = None,
    source_vintage: object = None,
) -> dict[str, object]:
    return {
        "check_id": check_id,
        "table_name": table_name,
        "region_id": region_id,
        "period": period,
        "severity": severity,
        "status": status,
        "message": message,
        "observed_value": observed_value,
        "expected_value": expected_value,
        "source_vintage": source_vintage,
    }


def validate_table(name: str, frame: pd.DataFrame) -> list[dict[str, object]]:
    """Run stable contract, grain, completeness and domain checks."""

    events: list[dict[str, object]] = []
    expected = TABLE_CONTRACTS[name]
    missing = [column for column in expected if column not in frame.columns]
    events.append(
        _event(
            "contract_columns",
            name,
            severity="critical",
            status="failed" if missing else "passed",
            message=(
                f"Missing canonical columns: {', '.join(missing)}"
                if missing
                else "All canonical columns are present."
            ),
            observed_value=len(frame.columns),
            expected_value=len(expected),
        )
    )
    if missing:
        return events

    grain = GRAINS.get(name)
    if grain:
        duplicate_count = int(frame.duplicated(list(grain), keep=False).sum())
        events.append(
            _event(
                "unique_grain",
                name,
                severity="critical",
                status="failed" if duplicate_count else "passed",
                message=(
                    f"{duplicate_count} rows participate in duplicate canonical keys."
                    if duplicate_count
                    else "Canonical grain is unique."
                ),
                observed_value=duplicate_count,
                expected_value=0,
            )
        )

    for column in REQUIRED_NOT_NULL.get(name, ()):
        missing_count = int(frame[column].isna().sum())
        events.append(
            _event(
                f"not_null_{column}",
                name,
                severity="high",
                status="failed" if missing_count else "passed",
                message=(
                    f"{missing_count} rows have a missing {column}."
                    if missing_count
                    else f"{column} is complete."
                ),
                observed_value=missing_count,
                expected_value=0,
            )
        )

    if "frequency" in frame:
        invalid = sorted(
            set(frame["frequency"].dropna().astype(str)) - ALLOWED_FREQUENCIES
        )
        events.append(
            _event(
                "frequency_domain",
                name,
                severity="high",
                status="failed" if invalid else "passed",
                message=f"Invalid frequencies: {invalid}" if invalid else "Frequencies are valid.",
                observed_value=", ".join(invalid),
                expected_value=", ".join(sorted(ALLOWED_FREQUENCIES)),
            )
        )

    if "quality_status" in frame:
        invalid = sorted(
            set(frame["quality_status"].dropna().astype(str))
            - ALLOWED_QUALITY_STATUSES
        )
        events.append(
            _event(
                "quality_status_domain",
                name,
                severity="medium",
                status="failed" if invalid else "passed",
                message=(
                    f"Invalid quality statuses: {invalid}"
                    if invalid
                    else "Quality statuses are valid."
                ),
                observed_value=", ".join(invalid),
                expected_value=", ".join(sorted(ALLOWED_QUALITY_STATUSES)),
            )
        )

    if name == "pit_receipts":
        invalid_ytd = int((pd.to_numeric(frame["pit_ytd_rub"], errors="coerce") < 0).sum())
        events.append(
            _event(
                "nonnegative_reported_ytd",
                name,
                severity="medium",
                status="warning" if invalid_ytd else "passed",
                message=(
                    f"{invalid_ytd} negative reported YTD values retained for review."
                    if invalid_ytd
                    else "Reported YTD PIT is non-negative."
                ),
                observed_value=invalid_ytd,
                expected_value=0,
            )
        )

    if name == "industrial_production":
        sections = set(frame["okved_section"].dropna().astype(str))
        unsupported = sorted(sections - INDUSTRIAL_OKVED_SECTIONS - {"TOTAL"})
        events.append(
            _event(
                "industrial_scope_domain",
                name,
                severity="medium",
                status="warning" if unsupported else "passed",
                message=(
                    f"Industrial rows outside matching B-E/TOTAL perimeter: {unsupported}."
                    if unsupported
                    else "Industrial scope is compatible with B-E cross-checks."
                ),
                observed_value=", ".join(unsupported),
                expected_value="B,C,D,E,TOTAL",
            )
        )

    if name == "budget_execution":
        actual = pd.to_numeric(frame["actual_ytd_rub"], errors="coerce")
        plan = pd.to_numeric(frame["approved_plan_rub"], errors="coerce")
        negative_count = int(((actual < 0) | (plan < 0)).sum())
        events.append(
            _event(
                "budget_amount_domain",
                name,
                severity="medium",
                status="warning" if negative_count else "passed",
                message=(
                    f"{negative_count} negative plan or actual values retained for review."
                    if negative_count
                    else "Budget plan and actual values are non-negative."
                ),
                observed_value=negative_count,
                expected_value=0,
            )
        )

    return events


def validate_tables(tables: Mapping[str, pd.DataFrame]) -> ValidationResult:
    """Validate all supplied canonical tables and return dashboard-ready events."""

    rows: list[dict[str, object]] = []
    for name, frame in tables.items():
        if name == "quality_events":
            continue
        if name not in TABLE_CONTRACTS:
            rows.append(
                _event(
                    "known_table",
                    name,
                    severity="critical",
                    status="failed",
                    message="Table is not declared in the canonical contracts.",
                )
            )
            continue
        rows.extend(validate_table(name, frame))
    return ValidationResult(pd.DataFrame(rows, columns=QUALITY_EVENT_COLUMNS))

