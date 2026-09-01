"""Ingest FNS Form 1-NOM PIT receipts using the release-specific schema."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

from .contracts import PIT_COLUMNS, SOURCE_COLUMNS
from .metrics import add_ytd_flows
from .provenance import sha256_file
from .regions import REGION_RECORDS


DATA_PATTERN = re.compile(
    r"^data-(?P<release>\d{8})-structure-(?P<schema>\d{8})(?: \(\d+\))?\.csv$"
)

SECTION_LABELS = {
    "A": ("Agriculture, forestry and fishing", "Сельское, лесное хозяйство, охота, рыболовство и рыбоводство"),
    "B": ("Mining and quarrying", "Добыча полезных ископаемых"),
    "C": ("Manufacturing", "Обрабатывающие производства"),
    "D": ("Electricity, gas and steam", "Обеспечение электрической энергией, газом и паром"),
    "E": ("Water, sewerage and waste", "Водоснабжение, водоотведение и обращение с отходами"),
    "F": ("Construction", "Строительство"),
    "G": ("Wholesale and retail trade", "Торговля оптовая и розничная"),
    "H": ("Transportation and storage", "Транспортировка и хранение"),
    "I": ("Accommodation and food services", "Гостиницы и общественное питание"),
    "J": ("Information and communication", "Информация и связь"),
    "K": ("Finance and insurance", "Финансы и страхование"),
    "L": ("Real estate", "Операции с недвижимым имуществом"),
    "M": ("Professional, scientific and technical activities", "Профессиональная, научная и техническая деятельность"),
    "N": ("Administrative and support services", "Административная деятельность и сопутствующие услуги"),
    "O": ("Public administration and social security", "Государственное управление и социальное обеспечение"),
    "P": ("Education", "Образование"),
    "Q": ("Health and social work", "Здравоохранение и социальные услуги"),
    "R": ("Arts, culture and recreation", "Культура, спорт, досуг и развлечения"),
    "S": ("Other services", "Предоставление прочих видов услуг"),
    "T-U": ("Other household and extraterritorial activities", "Остальные виды экономической деятельности"),
    "UNMAPPED": ("Unclassified / no OKVED", "Не распределено по ОКВЭД"),
}

CYRILLIC_SECTION = {"А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H"}


@dataclass(frozen=True)
class FNSIngestionResult:
    pit_receipts: pd.DataFrame
    source_metadata: pd.DataFrame
    skipped_files: tuple[str, ...]


def _delimiter(path: Path) -> str:
    first_line = path.open("r", encoding="utf-8-sig", errors="replace").readline()
    return ";" if first_line.count(";") > first_line.count(",") else ","


def _data_path_sort_key(path: Path) -> tuple[str, str, int, str]:
    """Prefer the canonical filename before browser-created numbered copies."""

    match = DATA_PATTERN.match(path.name)
    if match is None:
        return ("", "", 2, path.name)
    is_numbered_copy = int(bool(re.search(r" \(\d+\)\.csv$", path.name)))
    return (match.group("release"), match.group("schema"), is_numbered_copy, path.name)


def _normalise_section(token: str) -> str:
    token = CYRILLIC_SECTION.get(token.upper(), token.upper())
    return token if token in set("ABCDEFGHIJKLMNOPQRS") else token


def _pit_fields(schema: pd.DataFrame) -> tuple[dict[str, str], list[str]]:
    descriptions = schema["russian description"].fillna("").astype(str)
    total_pit = descriptions.str.contains("налог на доходы физических лиц", case=False)
    total_pit &= descriptions.str.contains(r"\bвсего\b", case=False, regex=True)
    total_pit &= ~descriptions.str.contains("в том числе", case=False)
    candidates = schema.loc[total_pit, ["field name", "russian description"]]

    section_fields: dict[str, str] = {}
    unmapped: list[str] = []
    for row in candidates.itertuples(index=False):
        field_name, description = str(row[0]), str(row[1]).strip()
        lowered = description.casefold()
        if "не распределен" in lowered or "не имеющ" in lowered:
            unmapped.append(field_name)
            continue
        match = re.match(r"^\s*([A-ZА-Я])\s+", description)
        if not match:
            continue
        section = _normalise_section(match.group(1))
        if section == "T" and re.search(r"\bU\s*99", description, re.I):
            section = "T-U"
        if section in SECTION_LABELS and section not in section_fields:
            # The published schema lists the broad section total before its children.
            section_fields[section] = field_name
    return section_fields, unmapped


def _selected_regions(regions: Iterable[str] | None) -> list[dict[str, object]]:
    requested = {str(value).casefold() for value in regions or ()}
    if not requested:
        return list(REGION_RECORDS)
    selected = []
    for record in REGION_RECORDS:
        aliases = {
            str(record["region_id"]).casefold(),
            str(record["region_name_en"]).casefold(),
            str(record["region_name_ru"]).casefold(),
            str(record["fns_code"]).casefold(),
        }
        if aliases & requested:
            selected.append(record)
    return selected


def ingest_fns_result(
    raw_dir: str | Path,
    regions: Iterable[str] | None = None,
) -> FNSIngestionResult:
    """Ingest only releases with their exact matching schema.

    This strict gate prevents applying a newer 2,816-field layout to older
    releases whose column positions differ. Duplicate downloads are removed by
    content hash. Values in Form 1-NOM are converted from thousand rubles to
    rubles.
    """

    root = Path(raw_dir)
    selected = _selected_regions(regions)
    codes = {str(record["fns_code"]): record for record in selected}
    names = {str(record["region_name_ru"]).casefold(): record for record in selected}
    frames: list[pd.DataFrame] = []
    sources: list[dict[str, object]] = []
    skipped: list[str] = []
    seen_hashes: set[str] = set()

    for path in sorted(root.glob("data-*-structure-*.csv"), key=_data_path_sort_key):
        match = DATA_PATTERN.match(path.name)
        if not match:
            # Browser-created "copy" files are duplicate local artifacts, not
            # distinct official releases and should not inflate coverage gaps.
            continue
        schema_path = root / f"structure-{match.group('schema')}.csv"
        if not schema_path.exists():
            skipped.append(f"{path.name}: missing {schema_path.name}")
            continue
        digest = sha256_file(path)
        if digest in seen_hashes:
            skipped.append(f"{path.name}: duplicate content")
            continue
        seen_hashes.add(digest)

        schema = pd.read_csv(schema_path, encoding="utf-8-sig", dtype=str)
        section_fields, unmapped_fields = _pit_fields(schema)
        if not section_fields:
            raise ValueError(f"No top-level PIT fields found in {schema_path.name}")
        usecols = ["GA", "GB", *section_fields.values(), *unmapped_fields]
        data = pd.read_csv(
            path,
            sep=_delimiter(path),
            encoding="utf-8-sig",
            dtype=str,
            usecols=lambda column: column in usecols,
            low_memory=False,
        )
        region_rows = data.loc[
            data["GA"].astype(str).str.strip().isin(codes)
            | data["GB"].astype(str).str.strip().str.casefold().isin(names)
        ]
        release_date = pd.to_datetime(match.group("release"), format="%Y%m%d")
        period = release_date - pd.offsets.Day(1)
        source_id = f"fns_1nom_{match.group('release')}_{digest[:12]}"
        vintage = f"fns-1nom-{match.group('release')}-{digest[:12]}"
        for source_row in region_rows.to_dict("records"):
            record = codes.get(str(source_row.get("GA", "")).strip()) or names.get(
                str(source_row.get("GB", "")).strip().casefold()
            )
            if record is None:
                continue
            for section, field_name in section_fields.items():
                value = pd.to_numeric(source_row.get(field_name), errors="coerce")
                if pd.isna(value):
                    continue
                label_en, label_ru = SECTION_LABELS[section]
                frames.append(
                    pd.DataFrame(
                        [
                            {
                                "region_id": record["region_id"],
                                "region_name_en": record["region_name_en"],
                                "region_name_ru": record["region_name_ru"],
                                "reporting_cutoff": release_date,
                                "period": period,
                                "frequency": "quarterly",
                                "okved_raw": section,
                                "okved_section": section,
                                "industry_name_en": label_en,
                                "industry_name_ru": label_ru,
                                "pit_ytd_rub": float(value) * 1000.0,
                                "pit_flow_rub": pd.NA,
                                "cpi_index": pd.NA,
                                "source_id": source_id,
                                "source_vintage": vintage,
                                "revision_status": "original",
                                "quality_status": "verified",
                                "is_official": True,
                            }
                        ]
                    )
                )
            if unmapped_fields:
                values = pd.to_numeric(
                    pd.Series([source_row.get(field) for field in unmapped_fields]),
                    errors="coerce",
                )
                if values.notna().any():
                    label_en, label_ru = SECTION_LABELS["UNMAPPED"]
                    frames.append(
                        pd.DataFrame(
                            [
                                {
                                    "region_id": record["region_id"],
                                    "region_name_en": record["region_name_en"],
                                    "region_name_ru": record["region_name_ru"],
                                    "reporting_cutoff": release_date,
                                    "period": period,
                                    "frequency": "quarterly",
                                    "okved_raw": "UNMAPPED",
                                    "okved_section": "UNMAPPED",
                                    "industry_name_en": label_en,
                                    "industry_name_ru": label_ru,
                                    "pit_ytd_rub": float(values.sum()) * 1000.0,
                                    "pit_flow_rub": pd.NA,
                                    "cpi_index": pd.NA,
                                    "source_id": source_id,
                                    "source_vintage": vintage,
                                    "revision_status": "original",
                                    "quality_status": "verified",
                                    "is_official": True,
                                }
                            ]
                        )
                    )
        sources.append(
            {
                "source_id": source_id,
                "publisher": "Federal Tax Service of Russia (FNS)",
                "dataset_name": "Form 1-NOM: tax receipts by main economic activity",
                "source_url": "https://www.nalog.gov.ru/opendata/7707329152-1nomo/",
                "local_path": str(path.resolve()),
                "retrieved_at_utc": pd.NaT,
                "reporting_cutoff": release_date,
                "publication_date": release_date,
                "units": "RUB (source: thousand RUB)",
                "sha256": digest,
                "file_size_bytes": path.stat().st_size,
                "is_official": True,
                "notes": f"Parsed with exact schema {schema_path.name}.",
            }
        )

    pit = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=PIT_COLUMNS)
    if not pit.empty:
        pit = add_ytd_flows(
            pit,
            value_column="pit_ytd_rub",
            group_columns=["region_id", "okved_raw"],
            date_column="period",
            output_column="pit_flow_rub",
        )
        pit = pit.loc[:, list(PIT_COLUMNS)].sort_values(
            ["region_id", "period", "okved_section"], kind="stable"
        ).reset_index(drop=True)
    source_frame = pd.DataFrame(sources, columns=SOURCE_COLUMNS)
    return FNSIngestionResult(pit, source_frame, tuple(skipped))


def ingest_fns(raw_dir: str | Path, regions: Iterable[str] | None = None) -> pd.DataFrame:
    """Return canonical PIT rows; see :func:`ingest_fns_result` for provenance."""

    return ingest_fns_result(raw_dir, regions).pit_receipts
