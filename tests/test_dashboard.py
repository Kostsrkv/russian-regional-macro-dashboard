from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from macro_rus.dashboard import (  # noqa: E402
    DataBundle,
    available_regions,
    classify_revenue,
    fiscal_snapshot,
    industry_snapshot,
    pit_summary,
)


class DashboardTransformTests(unittest.TestCase):
    def test_region_list_prefers_dimension_names(self) -> None:
        bundle = DataBundle(
            root=Path("."),
            tables={
                "regions": pd.DataFrame(
                    [
                        {
                            "region_id": "RU-KLU",
                            "region_name_en": "Kaluga Oblast",
                            "region_name_ru": "Калужская область",
                        }
                    ]
                )
            },
        )
        self.assertEqual(available_regions(bundle)[0]["region_name_en"], "Kaluga Oblast")

    def test_pit_yoy_uses_same_period_last_year(self) -> None:
        frame = pd.DataFrame(
            {
                "period": pd.to_datetime(["2025-06-30", "2026-06-30"]),
                "pit_flow_rub": [100.0, 125.0],
            }
        )
        summary = pit_summary(frame)
        self.assertEqual(summary["value"], 125.0)
        self.assertAlmostEqual(summary["yoy_pct"], 25.0)

    def test_industry_snapshot_preserves_missing_prior(self) -> None:
        frame = pd.DataFrame(
            {
                "period": pd.to_datetime(["2026-06-30"]),
                "industry_name_en": ["Manufacturing"],
                "industry_name_ru": ["Обрабатывающие производства"],
                "okved_section": ["C"],
                "pit_flow_rub": [200.0],
            }
        )
        result = industry_snapshot(frame)
        self.assertTrue(pd.isna(result.loc[0, "change_rub"]))
        self.assertAlmostEqual(result.loc[0, "share_pct"], 100.0)

    def test_revenue_classifier_handles_kbk_and_russian_name(self) -> None:
        self.assertEqual(
            classify_revenue({"revenue_code": "10102000010000110"}),
            "PIT",
        )
        self.assertEqual(
            classify_revenue({"revenue_name_ru": "Налоговые и неналоговые доходы"}),
            "Own-source revenue",
        )

    def test_fiscal_snapshot_prefers_revised_plan(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "period": pd.Timestamp("2026-06-30"),
                    "budget_level": "Consolidated regional budget",
                    "revenue_code": "10102000010000110",
                    "actual_ytd_rub": 50.0,
                    "approved_plan_rub": 90.0,
                    "revised_plan_rub": 100.0,
                }
            ]
        )
        result = fiscal_snapshot(frame)
        self.assertEqual(result.loc[0, "plan_basis"], "Revised plan")
        self.assertAlmostEqual(result.loc[0, "execution_pct"], 50.0)


if __name__ == "__main__":
    unittest.main()
