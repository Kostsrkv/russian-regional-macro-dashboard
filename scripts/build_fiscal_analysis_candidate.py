"""Build a fresh, portable fiscal-analysis review candidate from validated inputs.

No download, promotion, deployment or overwrite. The two existing candidate
loaders validate actuals and lineage before any output is created.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from macro_rus.fiscal_analysis import (BASES, budget_balance_bridge,
    derive_fiscal_flows, spending_function_history, transfer_history)
from macro_rus.fiscal_view import load_candidate
from macro_rus.provenance import sha256_file
from macro_rus.revenue_view import load_revenue_candidate


def build_candidate(fiscal_root: Path, revenue_root: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(f"Candidate output already exists; no overwrite: {output}")
    fiscal, fiscal_manifest = load_candidate(fiscal_root)
    revenue, revenue_manifest = load_revenue_candidate(revenue_root)
    if set(fiscal.region_id) != set(revenue["budget_execution"].region_id):
        raise ValueError("Fiscal and revenue eligible populations differ")
    keys = ["region_id", "period", "budget_level"]
    ft = fiscal.loc[fiscal.kind.eq("summary") & fiscal.metric_code.eq("revenue_total"), keys + ["actual_ytd_rub"]]
    rt = revenue["budget_execution"].loc[lambda x: x.revenue_code.eq("TOTAL_REVENUE"), keys + ["actual_ytd_rub"]]
    parity = ft.merge(rt, on=keys, how="outer", validate="one_to_one", indicator=True, suffixes=("_fiscal", "_revenue"))
    if not parity._merge.eq("both").all() or (parity.actual_ytd_rub_fiscal - parity.actual_ytd_rub_revenue).abs().gt(.02).any():
        raise ValueError("Fiscal and revenue actual totals fail same-window parity")
    parity["gap_rub"] = parity.actual_ytd_rub_fiscal - parity.actual_ytd_rub_revenue
    frames = {"fiscal_flows.csv": derive_fiscal_flows(fiscal)}
    frames["spending_function_history.csv"] = pd.concat(
        [spending_function_history(fiscal, basis) for basis in BASES], ignore_index=True)
    frames["transfer_history.csv"] = pd.concat(
        [transfer_history(revenue, basis) for basis in BASES], ignore_index=True)
    bridges, unavailable = [], []
    for region_id, regional in fiscal.groupby("region_id"):
        for period in sorted(regional.period.unique()):
            for basis in BASES:
                rows, reason = budget_balance_bridge(regional, region_id, period, basis)
                if reason:
                    unavailable.append({"region_id": region_id, "period": pd.Timestamp(period),
                                        "amount_basis": basis, "reason": reason})
                else:
                    bridges.append(rows)
    frames["budget_balance_bridges.csv"] = pd.concat(bridges, ignore_index=True)
    frames["balance_bridge_unavailable.csv"] = pd.DataFrame(unavailable)
    spending = frames["spending_function_history.csv"]
    grouped = spending.groupby(keys + ["amount_basis", "window"])
    spending_checks = grouped.agg(
        functions=("metric_code", "size"), functions_complete=("functions_complete", "all"),
        comparison_complete=("comparison_complete", "all"), share_sum_pct=("share_pct", lambda x: x.sum(min_count=14)),
        spending_reconciliation_gap_rub=("spending_reconciliation_gap_rub", "first"),
        change_reconciliation_gap_rub=("change_reconciliation_gap_rub", "first"),
        total_spending_rub=("total_spending_rub", "first"),
        total_spending_change_rub=("total_spending_change_rub", "first"),
        contribution_sum_rub=("contribution_to_spending_change_rub", lambda x: x.sum(min_count=14)),
        contribution_growth_sum_pp=("contribution_to_spending_growth_pp", lambda x: x.sum(min_count=14)),
    ).reset_index()
    if not spending_checks.functions.eq(14).all() or not spending_checks.functions_complete.all():
        raise ValueError("The complete source candidate must retain all 14 spending functions")
    if not np.allclose(spending_checks.share_sum_pct, 100, atol=1e-8):
        raise ValueError("Spending shares no longer reconcile")
    frames["spending_accounting_checks.csv"] = spending_checks
    frames["fiscal_revenue_parity_checks.csv"] = parity.drop(columns="_merge")
    population = pd.DataFrame({"region_id": sorted(fiscal.region_id.unique())})
    frames["eligible_regions.csv"] = population
    region = "RU-SVE" if "RU-SVE" in set(fiscal.region_id) else sorted(fiscal.region_id)[0]
    sample = fiscal.loc[fiscal.region_id.eq(region) & fiscal.metric_code.isin(["revenue_total", "expenditure_total", "budget_balance"])
                        & fiscal.period.dt.year.eq(2026)].sort_values(["period", "metric_code"])
    q1, h1 = [sample.loc[sample.period.dt.month.eq(month)].set_index("metric_code").actual_ytd_rub for month in (3, 6)]
    hand_check = {"region_id": region, "period": "2026-06-30", "basis": "period_flow",
                  "q1_revenue_rub": q1.revenue_total, "h1_revenue_rub": h1.revenue_total,
                  "q2_revenue_rub": h1.revenue_total - q1.revenue_total,
                  "q1_expenditure_rub": q1.expenditure_total, "h1_expenditure_rub": h1.expenditure_total,
                  "q2_expenditure_rub": h1.expenditure_total - q1.expenditure_total,
                  "q1_balance_rub": q1.budget_balance, "h1_balance_rub": h1.budget_balance,
                  "q2_balance_rub": h1.budget_balance - q1.budget_balance}
    output.mkdir(parents=True, exist_ok=False)
    for filename, frame in frames.items():
        frame.to_csv(output / filename, index=False)
    (output / "hand_check.json").write_text(json.dumps(hand_check, indent=2) + "\n")
    grains = {
        "fiscal_flows.csv": "region_id, period, budget_level, kind, metric_code (period_flow)",
        "spending_function_history.csv": "region_id, period, budget_level, amount_basis, metric_code (14 top-level functions)",
        "transfer_history.csv": "region_id, period, budget_level, amount_basis",
        "budget_balance_bridges.csv": "region_id, period, budget_level, amount_basis, step (4 steps)",
        "balance_bridge_unavailable.csv": "region_id, period, amount_basis",
        "spending_accounting_checks.csv": "region_id, period, budget_level, amount_basis",
        "fiscal_revenue_parity_checks.csv": "region_id, period, budget_level",
        "eligible_regions.csv": "region_id",
    }
    manifest = {
        "status": "candidate_not_promoted", "generated_at": datetime.now(timezone.utc).isoformat(),
        "eligible_regions": len(population), "source_region_periods": fiscal_manifest["region_periods"],
        "periods": sorted(fiscal.period.dt.strftime("%Y-%m-%d").unique().tolist()),
        "budget_level": "consolidated_regional_budget", "units": "RUB", "bases": list(BASES),
        "scope": "Regional + municipal budgets; excludes federal budgets and territorial health-insurance funds",
        "blocking_failures": 0, "checks": {"source_loaders_passed": True, "same_population": True,
            "revenue_total_parity_rows": len(parity), "spending_function_checks": len(spending_checks),
            "available_bridges": len(bridges), "unavailable_prior_year_windows": len(unavailable)},
        "inputs": [{"path": str(path.relative_to(ROOT)), "sha256": sha256_file(path)} for path in
                   [fiscal_root / "fiscal_observations.csv", fiscal_root / "manifest.json",
                    revenue_root / "budget_execution.parquet", revenue_root / "lineage.csv", revenue_root / "manifest.json"]],
        "source_manifests": {"fiscal_status": fiscal_manifest["status"], "revenue_status": revenue_manifest["status"]},
        "derived_hashes": {filename: sha256_file(output / filename) for filename in [*frames, "hand_check.json"]},
        "datasets": {filename: {"rows": len(frame), "grain": grains[filename], "columns": frame.columns.tolist()}
                     for filename, frame in frames.items()},
        "methods": {"flows": "Q1 = Q1 YTD; Q2 = H1 YTD - same-year Q1 YTD, exact region/kind/code/perimeter; missing endpoints remain missing",
            "shares": "Function actual / total expenditure; same-window prior year; share change in percentage points",
            "contributions": "Signed function current - prior; sum equals total spending change when all 14 are complete",
            "balance": "Current balance = prior balance + change in revenue - change in expenditure; exact endpoint accounting checks",
            "transfers": "Specific 202 transfers from other budgets / total revenue; own-source levels from 100 tax and non-tax revenue"},
        "limitations": ["Nominal actuals only; no original-budget/amendment history, plan shortfall, debt stock or federal-origin inference",
                        "Reporting cutoffs are source archive cutoffs, not publication or retrieval timestamps",
                        "2023 same-window prior-year comparison unavailable; no invented bridge"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fiscal-root", type=Path, default=ROOT / "outputs/regional_concordance_2026-09-29_v2/fiscal_preview")
    parser.add_argument("--revenue-root", type=Path, default=ROOT / "outputs/regional_revenue_candidate_2026-09-30")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/fiscal_analysis_candidate_2026-10-02")
    args = parser.parse_args()
    manifest = build_candidate(args.fiscal_root, args.revenue_root, args.output)
    print(json.dumps({"output": str(args.output), "eligible_regions": manifest["eligible_regions"], "checks": manifest["checks"]}, indent=2))
