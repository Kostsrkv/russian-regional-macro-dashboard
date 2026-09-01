"""Ingest Federal Treasury KBSRF Form 0503317 archives.

The Treasury download is an outer ZIP containing several nested ZIP files.  We
use the HTML rendition of Form 0503317 because it is self-describing and can be
parsed without a legacy ``.xls`` reader.  Region numbers in report filenames
are catalogue positions, not official region codes, so every report is resolved
through ``TerrList_428m.html``.

``approved_plan_rub`` means the approved budget assignments reported at the
snapshot cutoff.  It must not be interpreted as the originally enacted budget.
The source does not distinguish an original plan from later amendments, hence
``revised_plan_rub`` remains null.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import re
from typing import Iterable, Sequence
from zipfile import BadZipFile, ZipFile, ZipInfo
from io import BytesIO

import pandas as pd

from macro_rus.contracts import (
    BUDGET_COLUMNS,
    QUALITY_EVENT_COLUMNS,
    SOURCE_COLUMNS,
)


SOURCE_ID_PREFIX = "treasury_kbsrf_0503317"
SOURCE_URL = (
    "https://roskazna.gov.ru/ispolnenie-byudzhetov/"
    "konsolidirovannye-byudzhety-subektov-rossijskoj-federacii"
)


@dataclass(frozen=True)
class _Region:
    region_id: str
    name_en: str
    name_ru: str


PILOT_REGIONS = {
    "RU-KLU": _Region("RU-KLU", "Kaluga Oblast", "Калужская область"),
    "RU-SVE": _Region("RU-SVE", "Sverdlovsk Oblast", "Свердловская область"),
}

_REGION_ALIASES = {
    region.region_id.casefold(): region.region_id
    for region in PILOT_REGIONS.values()
}
_REGION_ALIASES.update(
    {
        region.name_en.casefold(): region.region_id
        for region in PILOT_REGIONS.values()
    }
)
_REGION_ALIASES.update(
    {
        region.name_ru.casefold(): region.region_id
        for region in PILOT_REGIONS.values()
    }
)


@dataclass(frozen=True)
class _Metric:
    revenue_code: str
    name_en: str
    name_ru: str
    source_code: str | None


METRICS = (
    _Metric("TOTAL_REVENUE", "Total revenue", "Доходы бюджета - всего", None),
    _Metric(
        "10000000000000000",
        "Tax and non-tax revenue",
        "НАЛОГОВЫЕ И НЕНАЛОГОВЫЕ ДОХОДЫ",
        "10000000000000000",
    ),
    _Metric(
        "10101000000000110",
        "Corporate income tax",
        "Налог на прибыль организаций",
        "10101000000000110",
    ),
    _Metric(
        "10102000010000110",
        "Personal income tax",
        "Налог на доходы физических лиц",
        "10102000010000110",
    ),
    _Metric(
        "20000000000000000",
        "Transfers and other non-repayable receipts",
        "БЕЗВОЗМЕЗДНЫЕ ПОСТУПЛЕНИЯ",
        "20000000000000000",
    ),
)


class TreasuryIngestionError(ValueError):
    """Raised when a Treasury package cannot satisfy the canonical contract."""


@dataclass(frozen=True)
class TreasuryIngestionResult:
    """Canonical tables and inspectable metadata produced from one archive."""

    budget_execution: pd.DataFrame
    source_metadata: pd.DataFrame
    quality_events: pd.DataFrame
    region_catalog: dict[str, str]

    @property
    def sources(self) -> pd.DataFrame:
        """Compatibility alias matching the canonical table name."""

        return self.source_metadata


class _RowsParser(HTMLParser):
    """Small tolerant HTML table parser for Treasury's generated reports."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"td", "th"} and self._cell_parts is not None:
            assert self._row is not None
            self._row.append(_normalise_text("".join(self._cell_parts)))
            self._cell_parts = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._cell_parts = None


class _CatalogueParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        values = dict(attrs)
        href = values.get("href")
        if href and "Mes_428m_" in href:
            self._href = href
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            filename = re.split(r"[\\/]", self._href)[-1]
            self.entries.append((_normalise_text("".join(self._parts)), filename))
            self._href = None
            self._parts = []


def _normalise_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _decode_zip_name(name: str) -> str:
    """Repair DOS Cyrillic names that were stored without the Unicode flag."""

    try:
        candidate = name.encode("cp437").decode("cp866")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name
    # ASCII names are unchanged; Cyrillic conversion fixes the outer members.
    return candidate


def _find_html_member(archive: ZipFile) -> ZipInfo:
    matches = [
        info
        for info in archive.infolist()
        if "0503317" in _decode_zip_name(info.filename)
        and "html" in _decode_zip_name(info.filename).casefold()
    ]
    if len(matches) != 1:
        raise TreasuryIngestionError(
            f"Expected one nested Form 0503317 HTML archive, found {len(matches)}"
        )
    return matches[0]


def _parse_catalogue(raw_html: bytes) -> dict[str, str]:
    parser = _CatalogueParser()
    parser.feed(raw_html.decode("cp1251", errors="strict"))
    catalogue = {name: filename for name, filename in parser.entries}
    if not catalogue:
        raise TreasuryIngestionError("Territory catalogue contains no report links")
    return catalogue


def _parse_decimal(value: str, *, field: str) -> Decimal:
    cleaned = value.replace(" ", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise TreasuryIngestionError(f"Invalid {field} amount: {value!r}") from exc


def _row_source_code(row: Sequence[str]) -> str | None:
    if len(row) < 8:
        return None
    # The first classification component is the revenue administrator (AP).
    # The canonical 17-digit KBK used in this project joins components 4..8.
    parts = row[3:8]
    if not parts or any(not re.fullmatch(r"\d+", part) for part in parts):
        return None
    return "".join(parts)


def _extract_metrics(raw_html: bytes) -> dict[str, tuple[Decimal, Decimal]]:
    parser = _RowsParser()
    parser.feed(raw_html.decode("cp1251", errors="strict"))
    data_rows = [row for row in parser.rows if len(row) >= 25]
    extracted: dict[str, tuple[Decimal, Decimal]] = {}

    for metric in METRICS:
        candidates = []
        for row in data_rows:
            if _normalise_text(row[0]).casefold() != metric.name_ru.casefold():
                continue
            if metric.source_code is not None and _row_source_code(row) != metric.source_code:
                continue
            candidates.append(row)
        if len(candidates) != 1:
            raise TreasuryIngestionError(
                f"Expected one {metric.name_en!r} row, found {len(candidates)}"
            )
        row = candidates[0]
        # Zero-based cells 10 and 24 are respectively plan and execution for
        # the consolidated budget of the subject, excluding the territorial
        # extra-budgetary fund and avoiding the subject-only budget perimeter.
        plan = _parse_decimal(row[10], field=f"{metric.name_en} plan")
        actual = _parse_decimal(row[24], field=f"{metric.name_en} actual")
        extracted[metric.revenue_code] = (plan, actual)
    return extracted


def _resolve_regions(regions: Iterable[str] | None) -> list[_Region]:
    requested = list(regions) if regions is not None else list(PILOT_REGIONS)
    resolved = []
    for value in requested:
        region_id = _REGION_ALIASES.get(str(value).strip().casefold())
        if region_id is None:
            raise TreasuryIngestionError(
                f"Unsupported region {value!r}; supported pilot regions are "
                f"{', '.join(PILOT_REGIONS)}"
            )
        resolved.append(PILOT_REGIONS[region_id])
    if len({region.region_id for region in resolved}) != len(resolved):
        raise TreasuryIngestionError("Requested regions contain duplicates")
    return resolved


def _cutoff_from_path(path: Path) -> pd.Timestamp:
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", path.name)
    if not match:
        raise TreasuryIngestionError(
            "Archive filename must contain its reporting cutoff as DD.MM.YYYY"
        )
    day, month, year = map(int, match.groups())
    return pd.Timestamp(year=year, month=month, day=day)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quality_event(
    *,
    check_id: str,
    severity: str,
    status: str,
    message: str,
    observed_value: object,
    expected_value: object,
    source_vintage: str,
    region_id: str | None = None,
    period: pd.Timestamp | None = None,
) -> dict[str, object]:
    return {
        "check_id": check_id,
        "table_name": "budget_execution",
        "region_id": region_id,
        "period": period,
        "severity": severity,
        "status": status,
        "message": message,
        "observed_value": observed_value,
        "expected_value": expected_value,
        "source_vintage": source_vintage,
    }


def ingest_treasury_archive(
    archive_path: str | Path,
    *,
    regions: Iterable[str] | None = None,
    retrieved_at_utc: datetime | str | None = None,
    audit_outer_archive: bool = True,
) -> TreasuryIngestionResult:
    """Ingest one KBSRF Form 0503317 quarterly snapshot.

    Parameters
    ----------
    archive_path:
        Original outer ``КБСРФ`` ZIP.  The file is read but never modified.
    regions:
        Pilot region IDs or English/Russian names.  Defaults to Kaluga and
        Sverdlovsk oblasts.
    retrieved_at_utc:
        Optional explicit retrieval timestamp for provenance.  It remains null
        when unknown rather than silently treating filesystem mtime as truth.
    audit_outer_archive:
        Check every outer member CRC.  A failure in an unused ancillary member
        is recorded as a medium-severity event but does not invalidate intact
        Form 0503317 observations.
    """

    path = Path(archive_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    selected_regions = _resolve_regions(regions)
    cutoff = _cutoff_from_path(path)
    period = (cutoff - pd.offsets.Day(1)).normalize()
    digest = _sha256_file(path)
    source_vintage = f"kbsrf-0503317-{cutoff:%Y%m%d}-{digest[:12]}"
    source_id = f"{SOURCE_ID_PREFIX}_{cutoff:%Y%m%d}_{digest[:12]}"
    events: list[dict[str, object]] = []

    try:
        with ZipFile(path) as outer:
            bad_outer_member = outer.testzip() if audit_outer_archive else None
            html_member = _find_html_member(outer)
            nested_bytes = outer.read(html_member)  # validates this member CRC
    except BadZipFile as exc:
        raise TreasuryIngestionError(f"Unreadable Treasury archive: {path.name}") from exc

    if bad_outer_member is not None:
        events.append(
            _quality_event(
                check_id="outer_archive_crc",
                severity="medium",
                status="failed",
                message=(
                    "An unused ancillary member has a CRC failure; the selected "
                    "Form 0503317 HTML member is intact and remains usable."
                ),
                observed_value=_decode_zip_name(bad_outer_member),
                expected_value="all outer members pass CRC",
                source_vintage=source_vintage,
            )
        )
    else:
        events.append(
            _quality_event(
                check_id="outer_archive_crc",
                severity="low",
                status="passed" if audit_outer_archive else "not_run",
                message=(
                    "All outer members passed CRC."
                    if audit_outer_archive
                    else "Full outer-member CRC audit was disabled."
                ),
                observed_value="no CRC failures" if audit_outer_archive else None,
                expected_value="all outer members pass CRC",
                source_vintage=source_vintage,
            )
        )

    try:
        with ZipFile(BytesIO(nested_bytes)) as reports:
            catalogue_bytes = reports.read("TerrList_428m.html")
            catalogue = _parse_catalogue(catalogue_bytes)
            report_payloads = {}
            for region in selected_regions:
                filename = catalogue.get(region.name_ru)
                if filename is None:
                    raise TreasuryIngestionError(
                        f"Region {region.name_ru!r} is missing from territory catalogue"
                    )
                report_payloads[region.region_id] = reports.read(filename)
    except (BadZipFile, KeyError) as exc:
        raise TreasuryIngestionError(
            "The nested Form 0503317 HTML archive is incomplete or corrupt"
        ) from exc

    rows: list[dict[str, object]] = []
    catalog_output = {region.name_ru: catalogue[region.name_ru] for region in selected_regions}
    for region in selected_regions:
        extracted = _extract_metrics(report_payloads[region.region_id])
        for metric in METRICS:
            plan, actual = extracted[metric.revenue_code]
            rows.append(
                {
                    "region_id": region.region_id,
                    "region_name_en": region.name_en,
                    "region_name_ru": region.name_ru,
                    "period": period,
                    "frequency": "quarterly",
                    "budget_level": "consolidated_regional_budget",
                    "revenue_code": metric.revenue_code,
                    "revenue_name_en": metric.name_en,
                    "revenue_name_ru": metric.name_ru,
                    "actual_ytd_rub": float(actual),
                    "approved_plan_rub": float(plan),
                    "revised_plan_rub": pd.NA,
                    "source_id": source_id,
                    "source_vintage": source_vintage,
                    "revision_status": "latest",
                    "quality_status": "verified",
                    "is_official": True,
                }
            )

        total_plan, total_actual = extracted["TOTAL_REVENUE"]
        own_plan, own_actual = extracted["10000000000000000"]
        transfer_plan, transfer_actual = extracted["20000000000000000"]
        plan_gap = total_plan - own_plan - transfer_plan
        actual_gap = total_actual - own_actual - transfer_actual
        tolerance = Decimal("0.02")
        passed = abs(plan_gap) <= tolerance and abs(actual_gap) <= tolerance
        events.append(
            _quality_event(
                check_id="revenue_components_reconcile",
                severity="high" if not passed else "low",
                status="passed" if passed else "failed",
                message=(
                    "Total revenue reconciles to tax/non-tax plus non-repayable receipts."
                    if passed
                    else "Published total does not reconcile to the two top-level components."
                ),
                observed_value=f"plan_gap={plan_gap}; actual_gap={actual_gap}",
                expected_value="absolute gaps <= 0.02 RUB",
                source_vintage=source_vintage,
                region_id=region.region_id,
                period=period,
            )
        )

    budget = pd.DataFrame(rows, columns=BUDGET_COLUMNS)
    key = ["region_id", "period", "budget_level", "revenue_code", "source_vintage"]
    if budget.duplicated(key).any():
        raise TreasuryIngestionError("Duplicate canonical budget observations detected")
    if budget[["actual_ytd_rub", "approved_plan_rub"]].isna().any().any():
        raise TreasuryIngestionError("Required plan or actual values are missing")

    events.append(
        _quality_event(
            check_id="canonical_grain",
            severity="critical",
            status="passed",
            message="Canonical budget grain is unique and required measures are populated.",
            observed_value=f"{len(budget)} rows; 0 duplicate keys",
            expected_value=f"{len(selected_regions) * len(METRICS)} unique rows",
            source_vintage=source_vintage,
            period=period,
        )
    )

    retrieved = (
        pd.to_datetime(retrieved_at_utc, utc=True)
        if retrieved_at_utc is not None
        else pd.NaT
    )
    notes = (
        "Parsed nested Form 0503317 HTML; territory filenames resolved through "
        "TerrList_428m.html. approved_plan_rub is the approved assignment reported "
        "at the cutoff, not necessarily the originally enacted plan."
    )
    if bad_outer_member is not None:
        notes += " Outer package has a CRC failure in an unused ancillary member."
    sources = pd.DataFrame(
        [
            {
                "source_id": source_id,
                "publisher": "Federal Treasury of Russia",
                "dataset_name": "KBSRF Form 0503317 regional budget execution",
                "source_url": SOURCE_URL,
                "local_path": str(path),
                "retrieved_at_utc": retrieved,
                "reporting_cutoff": cutoff,
                "publication_date": pd.NaT,
                "units": "RUB",
                "sha256": digest,
                "file_size_bytes": path.stat().st_size,
                "is_official": True,
                "notes": notes,
            }
        ],
        columns=SOURCE_COLUMNS,
    )
    quality = pd.DataFrame(events, columns=QUALITY_EVENT_COLUMNS)
    return TreasuryIngestionResult(budget, sources, quality, catalog_output)


def ingest_treasury_archives(
    archive_paths: Iterable[str | Path],
    *,
    regions: Iterable[str] | None = None,
    retrieved_at_utc: datetime | str | None = None,
    audit_outer_archive: bool = True,
    continue_on_error: bool = False,
) -> TreasuryIngestionResult:
    """Combine multiple immutable quarterly snapshots into canonical tables.

    ``continue_on_error`` is intended for a directory-level refresh containing
    many independent vintages.  A corrupt package is quarantined as a visible
    high-severity quality event while intact vintages remain usable.  Direct
    callers retain fail-fast behavior by default.
    """

    results: list[TreasuryIngestionResult] = []
    failures: list[dict[str, object]] = []
    for archive_path in archive_paths:
        try:
            results.append(
                ingest_treasury_archive(
                    archive_path,
                    regions=regions,
                    retrieved_at_utc=retrieved_at_utc,
                    audit_outer_archive=audit_outer_archive,
                )
            )
        except (TreasuryIngestionError, BadZipFile) as exc:
            if not continue_on_error:
                raise
            path = Path(archive_path)
            failures.append(
                {
                    "check_id": "treasury_archive_quarantined",
                    "table_name": "budget_execution",
                    "region_id": None,
                    "period": None,
                    "severity": "high",
                    "status": "warning",
                    "message": f"{path.name} was quarantined and not used: {exc}",
                    "observed_value": path.name,
                    "expected_value": "readable Form 0503317 archive",
                    "source_vintage": path.name,
                }
            )
    if not results:
        raise TreasuryIngestionError("At least one Treasury archive is required")
    budget = pd.concat([result.budget_execution for result in results], ignore_index=True)
    sources = pd.concat([result.source_metadata for result in results], ignore_index=True)
    quality = pd.concat([result.quality_events for result in results], ignore_index=True)
    if failures:
        quality = pd.DataFrame(
            [*quality.to_dict("records"), *failures],
            columns=QUALITY_EVENT_COLUMNS,
        )
    key = ["region_id", "period", "budget_level", "revenue_code", "source_vintage"]
    if budget.duplicated(key).any():
        raise TreasuryIngestionError("Duplicate observations across Treasury archives")
    catalogue = {
        name: filename
        for result in results
        for name, filename in result.region_catalog.items()
    }
    return TreasuryIngestionResult(
        budget[list(BUDGET_COLUMNS)],
        sources[list(SOURCE_COLUMNS)],
        quality[list(QUALITY_EVENT_COLUMNS)],
        catalogue,
    )


# Short alias for callers that prefer the source name over the archive detail.
ingest_treasury = ingest_treasury_archive
