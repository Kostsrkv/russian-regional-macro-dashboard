import importlib.util
from pathlib import Path

import pytest
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("coverage_audit", ROOT / "scripts/audit_treasury_coverage.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_source_keys_are_stable_not_catalogue_positions():
    name = "Калужская область"
    assert audit.territory_key(name) == audit.territory_key(name)
    assert audit.territory_key(name) != audit.territory_key("Свердловская область")
    assert audit.territory_key(name).startswith("treasury-label-")


def test_previous_run_is_immutable(tmp_path):
    with pytest.raises(FileExistsError):
        audit.run(tmp_path)


def test_review_queue_does_not_invent_region_codes():
    spec = importlib.util.spec_from_file_location("region_review", ROOT / "scripts/prepare_region_review.py")
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)
    data = pd.DataFrame([
        dict(territory_key="x", source_territory_name_ru="Калужская область", period="2026-06-30", status="accounting_passed"),
        dict(territory_key="y", source_territory_name_ru="Unmapped fixture", period="2026-06-30", status="parse_failed"),
    ])
    result = review.build_review(data).set_index("territory_key")
    assert result.loc["x", "region_id"] == "RU-KLU"
    assert result.loc["y", "region_id"] == ""
    assert not result.eligible_for_national_aggregation.any()
