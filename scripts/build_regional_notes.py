"""Build source-backed regional notes locally, without downloading or publishing."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from macro_rus.cloud_release import resolve_dashboard_config
from macro_rus.cumulative_production import load_cumulative_candidate
from macro_rus.fiscal_view import load_candidate
from macro_rus.fuel_prices import load_fuel_candidate
from macro_rus.regional_notes import LIMITATIONS, make_regional_note, prepare_note_data
from macro_rus.revenue_view import load_revenue_candidate
from macro_rus.revenue_view import CIT, NON_REPAYABLE, OWN, PIT, TRANSFERS


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(release_dir: Path, output_dir: Path) -> dict:
    # A manually supplied directory must be the selected, checksummed release.
    config = resolve_dashboard_config(release_dir.parents[3], {})
    if config.candidates["fiscal"] is None or config.candidates["fiscal"].parent.resolve() != release_dir.resolve():
        raise ValueError("Input must be the current checksummed dashboard research release")
    manifest_file = release_dir / "manifest.json"
    release = json.loads(manifest_file.read_text())
    selected = [name for name in release["payloads"] if name.split("/")[0] in
                {"fiscal", "revenue", "cumulative", "fuel"}]
    inputs = output_dir / "inputs"
    for name in ["manifest.json", *selected]:
        target = inputs / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and digest(target) != digest(release_dir / name):
            raise ValueError(f"Refusing to replace a different source input: {name}")
        shutil.copyfile(release_dir / name, target)
    fiscal, _ = load_candidate(inputs / "fiscal")
    revenue, _ = load_revenue_candidate(inputs / "revenue")
    production, _ = load_cumulative_candidate(inputs / "cumulative")
    fuel, _ = load_fuel_candidate(inputs / "fuel", eligible_region_ids=fiscal.region_id.unique())
    prepared = prepare_note_data(fiscal, revenue, production, fuel)
    names = fiscal[["region_id", "region_name_ru"]].drop_duplicates().set_index("region_id").region_name_ru.to_dict()
    records, claims, coverage = [], [], []
    latest = fiscal.period.max()
    notes_dir = output_dir / "notes"
    notes_dir.mkdir(exist_ok=True)
    for region_id, name in sorted(names.items()):
        periods = sorted(fiscal.loc[fiscal.region_id.eq(region_id), "period"].unique())
        for period in periods:
            for basis in ("YTD", "period_flow"):
                note = make_regional_note(prepared, region_id, period, basis, name)
                records.append(note.as_dict())
                for claim in note.claims:
                    claims.append({"region_id": region_id, "region_name_ru": name, "period": note.period,
                                   "amount_basis": basis, "window": note.window, **claim,
                                   "evidence": json.dumps(claim["evidence"], ensure_ascii=False, sort_keys=True)})
                lookup = {c["claim_code"]: c for c in note.claims}
                coverage.append(dict(region_id=region_id, period=note.period, amount_basis=basis, window=note.window,
                                     fiscal_current=lookup["budget_balance"]["availability"],
                                     fiscal_yoy=lookup["budget_balance_change"]["availability"],
                                     production=lookup["production_TOTAL"]["availability"],
                                     production_reason=lookup["production_TOTAL"]["reason"],
                                     fuel_grades_reported=sum(lookup[f"fuel_{grade}_price"]["availability"] == "available"
                                                              for grade in ("AI-92", "AI-95", "Diesel"))))
                if pd.Timestamp(period) == latest and basis == "YTD":
                    (notes_dir / f"{region_id}_{note.period}_{note.window}.md").write_text(note.markdown())
    # Independently recompute all claims from original selected source rows,
    # rather than using the note preparation/decomposition helpers again.
    prices = fuel.set_index(["region_id", "period", "fuel_grade"]).price_rub_per_litre
    indexes = production.set_index(["region_id", "period", "okved_section"]).index_value
    budget = revenue["budget_execution"].set_index(["region_id", "period", "revenue_code"]).actual_ytd_rub
    accounts = fiscal.set_index(["region_id", "period", "kind", "metric_code"]).actual_ytd_rub
    component_formulas = {"retained_pit": [(PIT, 1)], "cit": [(CIT, 1)],
                          "other_own": [(OWN, 1), (PIT, -1), (CIT, -1)],
                          "transfers": [(TRANSFERS, 1)], "other_non_repayable": [(NON_REPAYABLE, 1), (TRANSFERS, -1)]}
    checks = 0
    context_checks = 0
    for claim in claims:
        date = pd.Timestamp(claim["period"])
        code, value = claim["claim_code"], claim["value"]
        region = claim["region_id"]

        def amount(series, endpoint, suffix):
            result = series.get((region, endpoint, *suffix), np.nan)
            if claim["window"] == "Q2":
                result -= series.get((region, pd.Timestamp(endpoint.year, 3, 31), *suffix), np.nan)
            return result

        if code.startswith("fuel_"):
            grade = code.removeprefix("fuel_").rsplit("_", 1)[0]
            key = (claim["region_id"], date.replace(day=1), grade)
            current = prices.get(key, np.nan)
            if code.endswith("_price"):
                expected = current
            else:
                previous = prices.get((key[0], key[1] - pd.DateOffset(years=1), grade), np.nan)
                expected = (current / previous - 1) * 100
        elif code.startswith("production_"):
            expected = (np.nan if claim["window"] == "Q2" else
                        indexes.get((claim["region_id"], date, code.removeprefix("production_")), np.nan) - 100)
        elif code.startswith("component_"):
            formula = component_formulas[code.removeprefix("component_")]
            expected = sum(coefficient * (amount(budget, date, (source_code,))
                           - amount(budget, date - pd.DateOffset(years=1), (source_code,)))
                           for source_code, coefficient in formula)
        elif code.startswith("transfer_share"):
            expected = amount(budget, date, (TRANSFERS,)) / amount(budget, date, ("TOTAL_REVENUE",)) * 100
            if code.endswith("_change"):
                previous = date - pd.DateOffset(years=1)
                expected -= amount(budget, previous, (TRANSFERS,)) / amount(budget, previous, ("TOTAL_REVENUE",)) * 100
        elif code.startswith("spending_"):
            suffix = ("expenditure_function", code.removeprefix("spending_"))
            expected = amount(accounts, date, suffix) - amount(accounts, date - pd.DateOffset(years=1), suffix)
        else:
            suffix = ("summary", code.removesuffix("_change"))
            expected = amount(accounts, date, suffix)
            if code.endswith("_change"):
                expected -= amount(accounts, date - pd.DateOffset(years=1), suffix)
        if not ((value is None and pd.isna(expected)) or
                (value is not None and np.isclose(value, expected, atol=.02 if claim["units"] == "RUB" else 1e-10, rtol=0))):
            raise ValueError(f"Independent note-claim check failed: {region} {date:%Y-%m-%d} {code}")
        checks += 1
        context_checks += code.startswith(("fuel_", "production_"))
    (output_dir / "regional_notes.json").write_text(json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    pd.DataFrame(claims).to_csv(output_dir / "claims.csv", index=False, encoding="utf-8-sig", float_format="%.17g")
    pd.DataFrame(coverage).to_csv(output_dir / "coverage.csv", index=False, encoding="utf-8-sig")
    outputs = {name: {"sha256": digest(output_dir / name)} for name in ("regional_notes.json", "claims.csv", "coverage.csv")}
    summary = dict(status="analysis_completed_not_promoted", source_release=release["release_id"],
                   source_manifest_sha256=digest(manifest_file), regions=len(names), notes=len(records),
                   claim_rows=len(claims), independent_claim_checks=checks, independent_context_checks=context_checks,
                   latest_period=pd.Timestamp(latest).strftime("%Y-%m-%d"), latest_markdown_notes=len(names),
                   source_payloads_verified_and_copied=len(selected), outputs=outputs,
                   limitations=list(LIMITATIONS), source_payloads={name: release["payloads"][name] for name in selected})
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    options = parser.parse_args()
    result = build(options.release_dir.resolve(), options.output_dir.resolve())
    print(json.dumps({key: result[key] for key in ("status", "regions", "notes", "claim_rows", "independent_context_checks")}, indent=2))
