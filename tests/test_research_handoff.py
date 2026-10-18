"""Small deterministic boundary checks for the research-package workflow."""
from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


build = module("handoff_builder", "build_research_handoff.py")
verifier = module("handoff_verifier", "verify_research_package.py")


class HandoffTests(unittest.TestCase):
    def pit(self):
        return pd.DataFrame([
            dict(region_id="A", period="2025-03-31", okved_section="B", pit_ytd_rub=0., pit_flow_rub=0.),
            dict(region_id="A", period="2025-12-31", okved_section="C", pit_ytd_rub=10., pit_flow_rub=None),
        ])

    def test_missing_flow_and_missing_mining_are_explicit_not_zero(self):
        row = build.coverage_rows("pit_receipts", self.pit())[0]
        self.assertEqual(row["reported_primary_values"], 2)
        self.assertEqual(row["missing_period_flows"], 1)
        self.assertEqual(row["periods_present"], 2)
        self.assertIn("Mining B absent", row["known_limit"])

    def test_duplicate_selected_vintage_rejected(self):
        frame = self.pit()
        with self.assertRaisesRegex(ValueError, "grain"):
            build.validate_table("pit_receipts", pd.concat([frame, frame.iloc[[0]]]), {"A"})

    def test_foreign_region_rejected(self):
        with self.assertRaisesRegex(ValueError, "coverage"):
            build.validate_table("pit_receipts", self.pit(), {"B"})

    def test_negative_adjustments_retained_but_boolean_and_bad_text_rejected(self):
        frame = self.pit()
        frame["pit_ytd_rub"] = [-1., 10.]
        build.validate_table("pit_receipts", frame, {"A"})
        for value in (True, "invalid", float("inf")):
            frame["pit_ytd_rub"] = [value, 10.]
            with self.assertRaisesRegex(ValueError, "Invalid"):
                build.validate_table("pit_receipts", frame, {"A"})

    def test_unknown_dictionary_field_blocks_unexplained_schema_drift(self):
        frame = self.pit()
        frame["unknown_field"] = "x"
        with self.assertRaises(KeyError):
            build.dictionary_rows("pit_receipts", frame)

    def test_fiscal_scope_mismatch_rejected(self):
        frame = pd.DataFrame([dict(region_id="A", period="2025-03-31", budget_level="federal_budget",
                                   kind="summary", metric_code="budget_balance", actual_ytd_rub=1,
                                   frequency="YTD", units="RUB")])
        with self.assertRaisesRegex(ValueError, "perimeter"):
            build.validate_table("fiscal_observations", frame, {"A"})

    def production(self, frequency="monthly", period="2025-01-31"):
        basis = "same_year_previous_year_pct" if frequency == "annual" else "same_month_previous_year_pct"
        return pd.DataFrame([dict(region_id="A", period=period, okved_section=section,
                                  frequency=frequency, index_measure=basis, index_value=value)
                             for section, value in [("TOTAL", 105.), ("B", None), ("C", 0.), ("D", 101.), ("E", 99.)]])

    def test_production_history_is_optional_and_has_distinct_contracts(self):
        self.assertEqual(len(build.CORE_DATASETS), 5)
        self.assertEqual(len(build.DATASETS), 7)
        build.validate_table("industrial_production_monthly", self.production(), {"A"})
        build.validate_table("industrial_production_annual", self.production("annual", "2025-12-31"), {"A"})
        with self.assertRaisesRegex(ValueError, "basis"):
            build.validate_table("industrial_production_cumulative", self.production(), {"A"})

    def test_production_month_end_and_annual_year_end_are_required(self):
        with self.assertRaisesRegex(ValueError, "endpoint"):
            build.validate_table("industrial_production_monthly", self.production(period="2025-01-01"), {"A"})
        with self.assertRaisesRegex(ValueError, "completed calendar year"):
            build.validate_table("industrial_production_annual", self.production("annual", "2025-06-30"), {"A"})

    def test_production_invalid_scope_and_negative_index_rejected(self):
        frame = self.production()
        frame.loc[0, "okved_section"] = "A"
        with self.assertRaisesRegex(ValueError, "scope"):
            build.validate_table("industrial_production_monthly", frame, {"A"})
        frame = self.production()
        frame.loc[0, "index_value"] = -1
        with self.assertRaisesRegex(ValueError, "Negative"):
            build.validate_table("industrial_production_monthly", frame, {"A"})

    def test_production_index_growth_reconciliation(self):
        frame = self.production("annual", "2025-12-31")
        frame["growth_yoy_pct"] = frame.index_value - 100
        build.validate_table("industrial_production_annual", frame, {"A"})
        frame.loc[0, "growth_yoy_pct"] = 6
        with self.assertRaisesRegex(ValueError, "growth disagrees"):
            build.validate_table("industrial_production_annual", frame, {"A"})

    def test_calendar_holes_and_explicit_missing_prices_are_separate(self):
        frame = self.production().drop(index=[4])
        row = build.coverage_rows("industrial_production_monthly", frame)[0]
        self.assertEqual(row["missing_primary_values"], 1)
        self.assertEqual(row["reported_primary_values"], 3)
        self.assertEqual(row["missing_calendar_rows"], 1)
        self.assertEqual(row["expected_calendar_rows"], 5)
        self.assertEqual(len(frame), 4)  # No missing-value rows or zero fill fabricated.

    def fixture_package(self, root):
        output = root / "package"
        (output / "metadata").mkdir(parents=True)
        (output / "provenance").mkdir()
        rows = [dict(fns_code="04", rows=0, included=False, missing=None, name="Край, область")]
        metadata = root / "metadata.json"
        metadata.write_text(json.dumps({"dataset_catalog": rows}, ensure_ascii=False))
        with (output / "metadata/dataset_catalog.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerow(dict(fns_code="04", rows=0, included="false", missing="", name="Край, область"))
        for name in ("START_HERE.md", "JOIN_GUIDE.md", "MEASURES.md", "verify_package.py"):
            (output / name).write_text("Fixture\n")
        checks = dict(source_release="fixture", source_payloads_verified=0)
        (output / "provenance/handoff_checks.json").write_text(json.dumps(checks))
        return output, metadata

    def test_archive_and_verifier_preserve_null_zero_unicode_and_leading_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, metadata = self.fixture_package(root)
            result = build.seal(output, metadata, root / "package.zip")
            self.assertEqual(result["metadata_rows"], {"dataset_catalog": 1})
            self.assertEqual(verifier.verify(output)["status"], "internal_review_draft_not_sent")
            (output / "MEASURES.md").write_text("Changed\n")
            with self.assertRaisesRegex(ValueError, "Changed"):
                verifier.verify(output)

    def test_csv_type_loss_and_unexpected_payloads_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, metadata = self.fixture_package(root)
            path = output / "metadata/dataset_catalog.csv"
            text = path.read_text(encoding="utf-8-sig")
            path.write_text(text.replace("04,", "4,"), encoding="utf-8-sig")
            with self.assertRaisesRegex(ValueError, "loss"):
                build.seal(output, metadata, root / "package.zip")

    def test_private_machine_paths_block_package_sealing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, metadata = self.fixture_package(root)
            (output / "START_HERE.md").write_text("Private path: /Users/example/private.csv\n")
            with self.assertRaisesRegex(ValueError, "Private path"):
                build.seal(output, metadata, root / "package.zip")

    def test_existing_archive_blocks_before_any_package_manifest_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, metadata = self.fixture_package(root)
            (output / "manifest.json").write_text("Existing manifest\n")
            archive = root / "package.zip"
            archive.write_bytes(b"Existing archive")
            with self.assertRaisesRegex(ValueError, "overwrite"):
                build.seal(output, metadata, archive)
            self.assertEqual((output / "manifest.json").read_text(), "Existing manifest\n")
            self.assertEqual(archive.read_bytes(), b"Existing archive")

    def test_preparation_date_survives_manifest_and_archive(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, metadata = self.fixture_package(root)
            checks_path = output / "provenance/handoff_checks.json"
            checks = json.loads(checks_path.read_text())
            checks["prepared_on"] = "2026-10-13"
            checks_path.write_text(json.dumps(checks))
            archive = root / "package.zip"
            build.seal(output, metadata, archive)
            self.assertEqual(json.loads((output / "manifest.json").read_text())["prepared_on"], "2026-10-13")
            with zipfile.ZipFile(archive) as zipped:
                self.assertTrue(all(member.date_time[:3] == (2026, 10, 13) for member in zipped.infolist()))


if __name__ == "__main__":
    unittest.main()
