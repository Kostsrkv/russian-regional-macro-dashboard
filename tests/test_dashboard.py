from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from macro_rus.dashboard import (  # noqa: E402
    DataBundle,
    _format_pct,
    available_regions,
    classify_revenue,
    cross_check_snapshot,
    fiscal_snapshot,
    industry_snapshot,
    pit_summary,
    source_summary,
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
        self.assertTrue(pd.isna(result.loc[0, "yoy_pct"]))
        self.assertAlmostEqual(result.loc[0, "share_pct"], 100.0)

    def test_cross_check_preserves_missing_pit_change_instead_of_zero(self) -> None:
        pit = pd.DataFrame(
            {
                "period": pd.to_datetime(["2026-03-31"]),
                "industry_name_en": ["Manufacturing"],
                "industry_name_ru": ["Обрабатывающие производства"],
                "okved_section": ["C"],
                "pit_flow_rub": [200.0],
            }
        )
        production = pd.DataFrame(
            {
                "period": pd.to_datetime(["2026-03-31"]),
                "industry_name_en": ["Manufacturing"],
                "industry_name_ru": ["Обрабатывающие производства"],
                "okved_section": ["C"],
                "index_measure": ["same_month_previous_year_pct"],
                "index_value": [95.5],
                "source_vintage": ["rosstat-20260401"],
            }
        )
        result = cross_check_snapshot(pit, production)["data"]
        self.assertTrue(pd.isna(result.loc[0, "pit_yoy_pct"]))
        self.assertTrue(pd.isna(result.loc[0, "pit_change_rub"]))

    def test_cross_check_uses_production_at_pit_cutoff(self) -> None:
        pit = pd.DataFrame(
            {
                "period": pd.to_datetime(["2025-03-31", "2026-03-31"]),
                "industry_name_en": ["Manufacturing", "Manufacturing"],
                "industry_name_ru": ["Обрабатывающие производства"] * 2,
                "okved_section": ["C", "C"],
                "pit_flow_rub": [100.0, 125.0],
            }
        )
        production = pd.DataFrame(
            {
                "period": pd.to_datetime(["2026-03-31", "2026-07-31"]),
                "industry_name_en": ["Manufacturing", "Manufacturing"],
                "industry_name_ru": ["Обрабатывающие производства"] * 2,
                "okved_section": ["C", "C"],
                "index_measure": ["same_month_previous_year_pct"] * 2,
                "index_value": [95.5, 110.0],
                "source_vintage": ["rosstat-20260401", "rosstat-20260801"],
            }
        )
        result = cross_check_snapshot(pit, production)
        self.assertEqual(result["production_period"], pd.Timestamp("2026-03-31"))
        self.assertAlmostEqual(result["data"].loc[0, "index_value"], 95.5)
        self.assertAlmostEqual(result["data"].loc[0, "pit_yoy_pct"], 25.0)

    def test_source_summary_keeps_dataset_periods_separate(self) -> None:
        bundle = DataBundle(
            root=Path("."),
            tables={
                "pit_receipts": pd.DataFrame(
                    {
                        "region_id": ["RU-SVE"],
                        "period": pd.to_datetime(["2026-03-31"]),
                        "source_id": ["fns"],
                        "source_vintage": ["fns-q1"],
                    }
                ),
                "industrial_production": pd.DataFrame(
                    {
                        "region_id": ["RU-SVE"],
                        "period": pd.to_datetime(["2026-07-31"]),
                        "source_id": ["rosstat"],
                        "source_vintage": ["rosstat-jul"],
                    }
                ),
            },
        )
        summary = source_summary(bundle, "RU-SVE", ("pit_receipts", "industrial_production"))
        self.assertIn("PIT: 31 Mar 2026", summary["periods"])
        self.assertIn("Industrial production: 31 Jul 2026", summary["periods"])

    def test_missing_number_format_is_never_nan(self) -> None:
        self.assertEqual(_format_pct(float("nan")), "Not available")

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
