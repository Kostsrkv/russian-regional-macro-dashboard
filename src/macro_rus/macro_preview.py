"""Explicit, fail-closed loading of the regional macro review candidate."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .validation import GRAINS, validate_tables


def load_macro_candidate(root: Path):
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("status") != "candidate_not_promoted":
        raise ValueError("Expected an unpromoted macro candidate")
    sources = pd.read_csv(root / "sources.csv", keep_default_na=False)
    tables = {name: pd.read_parquet(root / f"{name}.parquet")
              for name in ("pit_receipts", "industrial_production")}
    if not validate_tables({**tables, "sources": sources}).is_valid:
        raise ValueError("Candidate failed canonical validation")
    region_sets = []
    names = []
    for name, frame in tables.items():
        expected = manifest["tables"][name]
        if len(frame) != expected["rows"] or frame.region_id.nunique() != manifest["eligible_regions"]:
            raise ValueError("Candidate coverage does not match manifest")
        grain = [column for column in GRAINS[name] if column != "source_vintage"]
        if frame.duplicated(grain).any():
            raise ValueError("Multiple observations at the selected-vintage grain")
        if not set(frame.source_id).issubset(set(sources.source_id)):
            raise ValueError("Candidate has orphan source identifiers")
        if not frame.quality_status.eq("warning").all():
            raise ValueError("Review candidate must retain warning status")
        for column in ("pit_ytd_rub", "pit_flow_rub", "index_value"):
            if column in frame and not np.isfinite(frame[column].dropna().astype(float)).all():
                raise ValueError("Non-finite candidate measure")
        region_sets.append(set(frame.region_id))
        names.append(frame[["region_id", "region_name_ru", "region_name_en"]].drop_duplicates())
    if region_sets[0] != region_sets[1]:
        raise ValueError("PIT and production region coverage differs")
    regions = pd.concat(names).drop_duplicates()
    if regions.region_id.duplicated().any():
        raise ValueError("Inconsistent candidate region names")
    if not sources.sha256.str.fullmatch(r"[0-9a-f]{64}").all():
        raise ValueError("Missing source hash")
    tables["sources"] = sources
    return tables, manifest
