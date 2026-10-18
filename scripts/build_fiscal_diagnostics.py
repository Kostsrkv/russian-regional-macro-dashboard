"""Build a private, reproducible accounting-analysis package from a pinned release.

No source download, release promotion or publication is performed. Every copied
input is verified against the existing publication manifest before calculation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from macro_rus.fiscal_analysis import _revenue_frame, spending_function_history, transfer_history
from macro_rus.fiscal_diagnostics import detailed_balance_bridge, revenue_component_history
from macro_rus.fiscal_view import load_candidate
from macro_rus.revenue_view import load_revenue_candidate


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(release_dir: Path, output_dir: Path) -> dict:
    manifest_path = release_dir / "manifest.json"
    release = json.loads(manifest_path.read_text())
    selected = ["fiscal/fiscal_observations.csv.gz", "fiscal/manifest.json",
                "revenue/budget_execution.parquet", "revenue/sources.csv",
                "revenue/lineage.csv.gz", "revenue/manifest.json"]
    if release.get("review_status") != "candidate_not_promoted":
        raise ValueError("Unexpected source review status")
    for name in selected:
        path = release_dir / name
        entry = release["payloads"][name]
        if digest(path) != entry["sha256"] or path.stat().st_size != entry["bytes"]:
            raise ValueError(f"Source payload changed: {name}")
    output_dir.mkdir(parents=True, exist_ok=True)
    inputs = output_dir / "inputs"
    for name in selected:
        destination = inputs / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and digest(destination) != digest(release_dir / name):
            raise ValueError(f"Refusing to overwrite a different input: {destination.name}")
        shutil.copyfile(release_dir / name, destination)
    shutil.copyfile(manifest_path, inputs / "manifest.json")
    fiscal, _ = load_candidate(inputs / "fiscal")
    revenue, _ = load_revenue_candidate(inputs / "revenue")
    joined_revenue = _revenue_frame(revenue)
    histories, functions, transfers, bridges, coverage = [], [], [], [], []
    for basis in ("YTD", "period_flow"):
        history = revenue_component_history(revenue, basis)
        histories.append(history)
        functions.append(spending_function_history(fiscal, basis))
        transfers.append(transfer_history(revenue, basis))
        for region_id in sorted(fiscal.region_id.unique()):
            regional_fiscal = fiscal.loc[fiscal.region_id.eq(region_id)].copy()
            regional_revenue = joined_revenue.loc[joined_revenue.region_id.eq(region_id)].copy()
            for period in sorted(regional_fiscal.period.unique()):
                bridge, reason = detailed_balance_bridge(regional_fiscal, regional_revenue, region_id, period, basis)
                coverage.append({"region_id": region_id, "period": pd.Timestamp(period),
                                 "amount_basis": basis, "available": reason is None,
                                 "reason": reason or "", "quality_status": "candidate_not_promoted"})
                if reason is None:
                    if abs(bridge.loc[bridge.role.eq("change"), "value_rub"].sum()
                           - bridge.balance_change_rub.iloc[0]) > .02:
                        raise ValueError("Independent signed bridge check failed")
                    bridges.append(bridge)
    components = pd.concat(histories, ignore_index=True)
    bridge_data = pd.concat(bridges, ignore_index=True)
    function_data = pd.concat(functions, ignore_index=True)
    transfer_data = pd.concat(transfers, ignore_index=True)
    bridge_coverage = pd.DataFrame(coverage)
    # An independently expressed pivot calculation checks every component and
    # every matched-year delta against the original selected source values.
    budget = revenue["budget_execution"].copy()
    budget["period"] = pd.to_datetime(budget.period)
    original = budget.pivot(index=["region_id", "period"], columns="revenue_code", values="actual_ytd_rub")
    checks = 0
    for row in components.itertuples(index=False):
        codes, coefficients = json.loads(row.source_codes), json.loads(row.source_coefficients)
        expected = sum(original.loc[(row.region_id, row.period), c] * w for c, w in zip(codes, coefficients))
        if row.amount_basis == "period_flow" and row.period.month == 6:
            start = pd.Timestamp(row.period.year, 3, 31)
            expected -= sum(original.loc[(row.region_id, start), c] * w for c, w in zip(codes, coefficients))
        if not np.isclose(row.amount_rub, expected, atol=.02, rtol=0):
            raise ValueError("Independent source-level component recomputation failed")
        if pd.notna(row.change_yoy_rub):
            prior = row.period - pd.DateOffset(years=1)
            expected_prior = sum(original.loc[(row.region_id, prior), c] * w for c, w in zip(codes, coefficients))
            if row.amount_basis == "period_flow" and row.period.month == 6:
                prior_start = pd.Timestamp(prior.year, 3, 31)
                expected_prior -= sum(original.loc[(row.region_id, prior_start), c] * w for c, w in zip(codes, coefficients))
            if not np.isclose(row.change_yoy_rub, expected - expected_prior, atol=.02, rtol=0):
                raise ValueError("Independent same-window change recomputation failed")
        checks += 1
    outputs = {"revenue_components.csv": components, "detailed_balance_bridges.csv": bridge_data,
               "spending_functions.csv": function_data, "transfer_history.csv": transfer_data,
               "bridge_coverage.csv": bridge_coverage}
    for name, data in outputs.items():
        data.to_csv(output_dir / name, index=False, encoding="utf-8-sig", float_format="%.17g")
    profile_rows = []
    names = fiscal[["region_id", "region_name_ru"]].drop_duplicates().set_index("region_id").region_name_ru.to_dict()
    latest = fiscal.period.max()
    for region_id in sorted(names):
        selected = components.loc[components.region_id.eq(region_id) & components.period.eq(latest) & components.amount_basis.eq("YTD")]
        bridge = bridge_data.loc[bridge_data.region_id.eq(region_id) & bridge_data.period.eq(latest) & bridge_data.amount_basis.eq("YTD")]
        if bridge.empty:
            continue
        totals = fiscal.loc[fiscal.region_id.eq(region_id) & fiscal.period.eq(latest) & fiscal.kind.eq("summary")].set_index("metric_code").actual_ytd_rub
        first = bridge.iloc[0]
        profile = {"region_id": region_id, "region_name_ru": names[region_id], "period": latest,
                   "amount_basis": "YTD", "revenue_rub": totals.revenue_total,
                   "expenditure_rub": totals.expenditure_total, "balance_rub": totals.budget_balance,
                   "prior_balance_rub": first.start_balance_rub, "balance_change_rub": first.balance_change_rub,
                   "expenditure_change_rub": first.expenditure_change_rub,
                   "quality_status": "candidate_not_promoted"}
        for row in selected.itertuples(index=False):
            profile[row.component_code + "_rub"] = row.amount_rub
            profile[row.component_code + "_change_rub"] = row.change_yoy_rub
        profile_rows.append(profile)
    profiles = pd.DataFrame(profile_rows).sort_values("region_name_ru")
    profiles.to_csv(output_dir / "latest_regional_fiscal_profiles.csv", index=False, encoding="utf-8-sig", float_format="%.17g")
    summary = {
        "status": "analysis_completed_not_promoted", "source_release": release["release_id"],
        "source_manifest_sha256": digest(manifest_path), "regions": fiscal.region_id.nunique(),
        "periods": sorted(fiscal.period.dt.strftime("%Y-%m-%d").unique()),
        "component_rows": len(components), "independent_component_checks": checks,
        "available_bridges": int(bridge_coverage.available.sum()),
        "unavailable_bridges": int((~bridge_coverage.available).sum()),
        "bridge_rows": len(bridge_data), "spending_function_rows": len(function_data),
        "latest_period": latest.strftime("%Y-%m-%d"), "latest_profile_rows": len(profiles),
        "source_payloads_verified": 6,
        "negative_component_levels_preserved": int(components.negative_amount_flag.sum()),
        "comparisons": "Q1 to Q1, H1 to H1, derived Q2 to Q2; no interpolation",
        "scope": "consolidated regional + municipal budgets, excluding territorial health-insurance funds",
        "limitations": ["Nominal amounts; CPI unavailable", "Accounting contributions are not causal effects",
                        "No plan-based shortfall assessment", "Transfer origin is not established as exclusively federal",
                        "Financing flows are not debt stocks", "No national aggregation or regional rankings"],
        "outputs": {name: {"rows": len(data), "sha256": digest(output_dir / name)} for name, data in outputs.items()},
    }
    summary["outputs"]["latest_regional_fiscal_profiles.csv"] = {"rows": len(profiles), "sha256": digest(output_dir / "latest_regional_fiscal_profiles.csv")}
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    options = parser.parse_args()
    result = build(options.release_dir, options.output_dir)
    print(json.dumps({name: value for name, value in result.items() if name not in {"outputs", "limitations"}}, indent=2))
