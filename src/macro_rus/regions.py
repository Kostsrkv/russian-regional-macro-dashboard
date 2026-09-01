"""Pilot-region reference data shared by ingestion and presentation layers."""

from __future__ import annotations

import pandas as pd

from .contracts import REGION_COLUMNS


REGION_RECORDS = (
    {
        "region_id": "RU-KLU",
        "region_name_en": "Kaluga Oblast",
        "region_name_ru": "Калужская область",
        "fns_code": "40",
        "okato_code": "29000000000",
        "federal_district": "Central Federal District",
        "economic_profile": (
            "A diversified manufacturing region with automotive, machinery, "
            "pharmaceutical and logistics activity."
        ),
        "coverage_start": pd.Timestamp("2017-01-01"),
        "coverage_end": pd.NaT,
    },
    {
        "region_id": "RU-SVE",
        "region_name_en": "Sverdlovsk Oblast",
        "region_name_ru": "Свердловская область",
        "fns_code": "66",
        "okato_code": "65000000000",
        "federal_district": "Ural Federal District",
        "economic_profile": (
            "A large industrial region centred on metals, mining, machinery, "
            "energy and business services."
        ),
        "coverage_start": pd.Timestamp("2017-01-01"),
        "coverage_end": pd.NaT,
    },
)


def regions_frame() -> pd.DataFrame:
    """Return a fresh canonical region dimension."""

    return pd.DataFrame(REGION_RECORDS, columns=REGION_COLUMNS)


REGION_NAME_TO_ID = {
    record["region_name_ru"].casefold(): record["region_id"]
    for record in REGION_RECORDS
}

