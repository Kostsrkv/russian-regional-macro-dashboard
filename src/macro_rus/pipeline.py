"""Build and promote one immutable, source-backed dashboard vintage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from .contracts import QUALITY_EVENT_COLUMNS, TABLE_CONTRACTS, TABLE_FILES
from .ingest_fns import ingest_fns_result
from .ingest_rosstat import ingest_rosstat_result
from .ingest_treasury import ingest_treasury_archives
from .regions import regions_frame
from .validation import ValidationResult, validate_tables


@dataclass(frozen=True)
class BuildResult:
    output_dir: Path
    manifest: dict[str, object]
    validation: ValidationResult


def _coverage_event(table: str, message: str, severity: str = "medium") -> dict[str, object]:
    return {
        "check_id": "source_coverage",
        "table_name": table,
        "region_id": None,
        "period": None,
        "severity": severity,
        "status": "failed",
        "message": message,
        "observed_value": None,
        "expected_value": None,
        "source_vintage": None,
    }


def _reconciliation_events(pit: pd.DataFrame, budget: pd.DataFrame) -> pd.DataFrame:
    """Compare FNS total PIT with Treasury retained PIT without conflating scope."""

    rows: list[dict[str, object]] = []
    if pit.empty or budget.empty:
        return pd.DataFrame(columns=QUALITY_EVENT_COLUMNS)
    treasury = budget.loc[
        budget["revenue_code"].astype(str).eq("10102000010000110")
    ].copy()
    fns_total = (
        pit.groupby(["region_id", "period"], as_index=False)["pit_ytd_rub"]
        .sum(min_count=1)
        .rename(columns={"pit_ytd_rub": "fns_pit_ytd_rub"})
    )
    joined = fns_total.merge(
        treasury[["region_id", "period", "actual_ytd_rub", "source_vintage"]],
        on=["region_id", "period"],
        how="inner",
    )
    for row in joined.itertuples(index=False):
        gap = float(row.fns_pit_ytd_rub - row.actual_ytd_rub)
        relative = gap / float(row.actual_ytd_rub) if row.actual_ytd_rub else None
        rows.append(
            {
                "check_id": "fns_treasury_pit_scope_reconciliation",
                "table_name": "pit_receipts",
                "region_id": row.region_id,
                "period": row.period,
                "severity": "medium",
                "status": "warning" if relative is None or abs(relative) > 0.05 else "passed",
                "message": (
                    "FNS collected PIT and Treasury retained consolidated-budget PIT differ in scope; "
                    "the gap is displayed for review and is not treated as a data error."
                ),
                "observed_value": relative,
                "expected_value": "scope-aligned comparison not yet available",
                "source_vintage": row.source_vintage,
            }
        )
    return pd.DataFrame(rows, columns=QUALITY_EVENT_COLUMNS)


def build_tables(
    raw_dir: str | Path,
    *,
    regions: Iterable[str] | None = None,
) -> tuple[dict[str, pd.DataFrame], ValidationResult, dict[str, object]]:
    """Ingest real local files and run promotion-blocking validation gates."""

    raw_root = Path(raw_dir)
    fns = ingest_fns_result(raw_root, regions)
    rosstat = ingest_rosstat_result(raw_root, regions)
    # Treasury has published the same consolidated-regional-budget package
    # under both Cyrillic and transliterated Latin filenames across vintages.
    # Discover both without depending on the analyst renaming immutable raws.
    treasury_paths = sorted(
        {
            *raw_root.glob("КБСРФ*.zip"),
            *raw_root.glob("KBSRF*.zip"),
        },
        key=lambda path: path.name,
    )
    treasury = ingest_treasury_archives(
        treasury_paths,
        regions=regions,
        continue_on_error=True,
    )

    sources = pd.concat(
        [fns.source_metadata, rosstat.source_metadata, treasury.source_metadata],
        ignore_index=True,
    ).drop_duplicates("source_id", keep="last")
    tables: dict[str, pd.DataFrame] = {
        "pit_receipts": fns.pit_receipts,
        "industrial_production": rosstat.industrial_production,
        "budget_execution": treasury.budget_execution,
        "regions": regions_frame(),
        "sources": sources.loc[:, list(TABLE_CONTRACTS["sources"])],
    }

    validation = validate_tables(tables)
    extra_events = [treasury.quality_events, _reconciliation_events(fns.pit_receipts, treasury.budget_execution)]
    skipped_events: list[dict[str, object]] = []
    if fns.skipped_files:
        missing_schema_count = sum("missing structure-" in item for item in fns.skipped_files)
        skipped_events.append(
            _coverage_event(
                "pit_receipts",
                f"{len(fns.skipped_files)} FNS downloads were not ingested, including "
                f"{missing_schema_count} without an exact matching schema. Details are in the manifest.",
            )
        )
    if rosstat.skipped_files:
        skipped_events.append(
            _coverage_event(
                "industrial_production",
                f"{len(rosstat.skipped_files)} non-monthly or duplicate Rosstat workbooks were excluded. "
                "Details are in the manifest.",
                severity="low",
            )
        )
    for name in ("pit_receipts", "industrial_production", "budget_execution"):
        if tables[name].empty:
            skipped_events.append(_coverage_event(name, "Core analytical table is empty.", severity="critical"))
    event_frames = [
        validation.events,
        *extra_events,
        pd.DataFrame(skipped_events, columns=QUALITY_EVENT_COLUMNS),
    ]
    event_records = [
        record
        for frame in event_frames
        if not frame.empty
        for record in frame.to_dict("records")
    ]
    combined_events = pd.DataFrame(event_records, columns=QUALITY_EVENT_COLUMNS)
    tables["quality_events"] = combined_events.loc[:, list(QUALITY_EVENT_COLUMNS)]
    final_validation = ValidationResult(combined_events)
    diagnostics = {
        "fns_skipped": list(fns.skipped_files),
        "rosstat_skipped": list(rosstat.skipped_files),
        "treasury_archives": [path.name for path in treasury_paths],
    }
    return tables, final_validation, diagnostics


def promote(
    tables: dict[str, pd.DataFrame],
    output_dir: str | Path,
    validation: ValidationResult,
    *,
    diagnostics: dict[str, object] | None = None,
) -> BuildResult:
    """Write canonical Parquet tables and a manifest after blocking checks pass."""

    if not validation.is_valid:
        failed = validation.blocking[["check_id", "table_name", "message"]].to_dict("records")
        raise ValueError(f"Promotion blocked by validation: {failed}")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    table_summary: dict[str, object] = {}
    for name, filename in TABLE_FILES.items():
        frame = tables.get(name, pd.DataFrame(columns=TABLE_CONTRACTS[name]))
        frame = frame.reindex(columns=TABLE_CONTRACTS[name])
        if name == "sources":
            # Keep immutable raw filename and hash, but never expose the
            # analyst's absolute local filesystem path in a public snapshot.
            frame["local_path"] = frame["local_path"].map(
                lambda value: Path(str(value)).name if pd.notna(value) else None
            )
        if name == "quality_events":
            for column in ("observed_value", "expected_value"):
                frame[column] = frame[column].map(
                    lambda value: None if pd.isna(value) else str(value)
                )
        frame.to_parquet(output / filename, index=False)
        periods = []
        for column in ("period", "reporting_cutoff"):
            if column in frame and frame[column].notna().any():
                values = pd.to_datetime(frame[column], errors="coerce").dropna()
                if not values.empty:
                    periods.extend([values.min(), values.max()])
        table_summary[name] = {
            "rows": int(len(frame)),
            "coverage_start": min(periods).isoformat() if periods else None,
            "coverage_end": max(periods).isoformat() if periods else None,
        }
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "promoted_with_documented_gaps",
        "tables": table_summary,
        "validation": {
            "passed": int(validation.events["status"].eq("passed").sum()),
            "warnings": int(validation.events["status"].isin(["warning", "failed"]).sum()),
            "blocking": int(len(validation.blocking)),
        },
        "diagnostics": diagnostics or {},
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return BuildResult(output, manifest, validation)


def build_and_promote(
    raw_dir: str | Path,
    output_dir: str | Path,
    *,
    regions: Iterable[str] | None = None,
) -> BuildResult:
    tables, validation, diagnostics = build_tables(raw_dir, regions=regions)
    return promote(tables, output_dir, validation, diagnostics=diagnostics)
