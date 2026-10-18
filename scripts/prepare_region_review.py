"""Prepare a region-concordance review queue from observed Treasury coverage."""
from pathlib import Path
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from macro_rus.regions import REGION_RECORDS


def build_review(coverage):
    reviewed = {r["region_name_ru"]: r for r in REGION_RECORDS}
    result = []
    for (key, name), rows in coverage.groupby(["territory_key", "source_territory_name_ru"]):
        known = reviewed.get(name, {})
        result.append(dict(
            territory_key=key, source_territory_name_ru=name,
            region_id=known.get("region_id", ""), fns_code=known.get("fns_code", ""),
            okato_code=known.get("okato_code", ""), region_name_en=known.get("region_name_en", ""),
            mapping_status="existing_pilot_mapping" if known else "needs_source_concordance",
            geography_review="pending_territorial_scope_and_overlap_review",
            observed_periods=rows.period.nunique(), accounting_passed_periods=int(rows.status.eq("accounting_passed").sum()),
            earliest_period=rows.period.min(), latest_period=rows.period.max(),
            eligible_for_national_aggregation=False,
        ))
    return pd.DataFrame(result)


if __name__ == "__main__":
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "outputs/treasury_coverage_2026-09-29"
    destination = folder / "region_crosswalk_review.csv"
    if destination.exists():
        raise FileExistsError("Preserve the existing review queue")
    review = build_review(pd.read_csv(folder / "coverage.csv"))
    review.to_csv(destination, index=False)
    print(review.mapping_status.value_counts().to_string())
