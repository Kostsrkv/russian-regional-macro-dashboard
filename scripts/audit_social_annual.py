"""Offline, reproducible social-spending and annual-production candidate audit."""
from pathlib import Path
import json
import sys

import nbformat
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from macro_rus.annual_production import read_annual, select_vintages, GRAIN
from macro_rus.fiscal_view import load_candidate
from macro_rus.provenance import sha256_file
from macro_rus.social_spending import social_history


def audit():
    mapping = pd.read_csv(ROOT / "outputs/regional_concordance_2026-09-29_v2/region_concordance.csv")
    records = mapping.loc[mapping.eligible_for_candidate_profile.eq(True)].to_dict("records")
    inputs = [ROOT / "ind_sub_2015-2025.xlsx", ROOT / "ind_sub_2020-2025.xlsx"]
    parsed = [read_annual(path, records) for path in inputs]
    frames, sources = zip(*parsed)
    selected = select_vintages(frames)
    overlap = frames[0].merge(frames[1], on=GRAIN, suffixes=("_earlier", "_later"), validate="one_to_one")
    overlap["difference_index_points"] = overlap.index_value_later - overlap.index_value_earlier
    overlap["changed"] = overlap.difference_index_points.abs().gt(1e-9) | overlap.index_value_later.isna().ne(overlap.index_value_earlier.isna())
    fiscal, fiscal_manifest = load_candidate(ROOT / "outputs/regional_concordance_2026-09-29_v2/fiscal_preview")
    social = pd.concat([social_history(fiscal, r["region_id"], period) for r in records for period in [pd.Timestamp("2026-03-31"), pd.Timestamp("2026-06-30")]], ignore_index=True)
    assert not social.duplicated(["region_id", "period", "metric_code"]).any()
    assert social.actual_ytd_rub.notna().all()
    assert not selected.duplicated(GRAIN).any()
    assert len(selected) == len(records) * 11 * 5
    assert all(sha256_file(path) == source["sha256"] for path, source in zip(inputs, sources))
    return selected, overlap, sources, social, fiscal_manifest


def run(output):
    output = Path(output)
    if output.exists():
        raise FileExistsError("Use a fresh candidate directory; audit outputs are immutable.")
    annual, overlap, sources, social, fiscal_manifest = audit()
    output.mkdir(parents=True)
    annual.to_parquet(output / "industrial_production_annual.parquet", index=False)
    annual.to_csv(output / "industrial_production_annual.csv", index=False)
    overlap.to_csv(output / "vintage_comparison.csv", index=False)
    annual.loc[annual.index_value.isna()].to_csv(output / "missing_observations.csv", index=False)
    social.to_csv(output / "social_spending.csv", index=False)
    coverage = annual.groupby(["region_id", "okved_section"]).agg(observations=("period", "size"), numeric_observations=("index_value", "count"), earliest=("period", "min"), latest=("period", "max")).reset_index()
    coverage.to_csv(output / "coverage.csv", index=False)
    summary = dict(status="candidate_not_promoted", blocking_failures=0, rows=len(annual), regions=annual.region_id.nunique(),
        annual_years=sorted(annual.period.dt.year.unique().tolist()), numeric_observations=int(annual.index_value.notna().sum()),
        unavailable_observations=int(annual.index_value.isna().sum()), overlapping_observations=len(overlap),
        changed_overlap_observations=int(overlap.changed.sum()), social_rows=len(social),
        social_region_periods=len(social[["region_id", "period"]].drop_duplicates()),
        dataset_sha256=sha256_file(output / "industrial_production_annual.parquet"), sources=sources,
        fiscal_candidate="outputs/regional_concordance_2026-09-29_v2/fiscal_preview",
        fiscal_manifest_sha256=sha256_file(ROOT / "outputs/regional_concordance_2026-09-29_v2/fiscal_preview/manifest.json"),
        limitations=["78 eligible non-overlapping regional identifiers; no national sums or rankings.",
            "Latest dated annual vintage wins, including missing cells; older numbers never fill later suppression.",
            "Headline 2020–2025 uses explicit 2023 GVA weights; 2015–2019 comes from an older vintage with an unstated weight base. No constant-method output-level chain is constructed.",
            "Broad-sector sheets do not explicitly state a weight base. All footnotes are preserved in the source manifest.",
            "Differences across vintages may reflect reweighting and revisions; they are not classified as economic changes.",
            "2025 in the February workbook is a first estimate; the August workbook does not explicitly label it final.",
            "Workbook update dates are known; original download URLs and retrieval times are not established.",
            "Social expenditure is nominal and excludes federal budgets and territorial health-insurance funds; social policy is not all social protection.",
            "Original/amended plans, regional CPI, wages and fuel prices are not supplied by this audit.",
            "No data promotion, download, deployment or external exchange performed."])
    (output / "manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    cells = [nbformat.v4.new_markdown_cell("# Social spending and annual production: offline audit\n\nPurpose: validate existing source workbooks and same-window budget functions. Candidate only; no downloads or deployment. Missing observations are retained. See the manifest for methodology boundaries and source footnotes."),
        nbformat.v4.new_code_cell(f"from pathlib import Path\nimport sys, json\nimport numpy as np\nimport pandas as pd\nROOT = Path({str(ROOT)!r})\nsys.path.insert(0, str(ROOT / 'scripts'))\nfrom audit_social_annual import audit\nannual, overlap, sources, social, fiscal_manifest = audit()\nprint('Reproduced source audit successfully')"),
        nbformat.v4.new_markdown_cell("## Coverage and missing source cells\n\nPublished annual YoY indices, not averaged monthly observations. Source markers remain unavailable; their original text and sheet footnotes are preserved."),
        nbformat.v4.new_code_cell("display(annual.groupby('okved_section').agg(rows=('index_value','size'), numeric=('index_value','count')))\ndisplay(annual.loc[annual.index_value.isna(), ['region_id','period','okved_section','raw_value','source_file','source_cell']].head(20))"),
        nbformat.v4.new_markdown_cell("## Overlapping vintages\n\nAugust 2026 supersedes February 2026 in 2020–2025, including unavailable values. Differences can reflect reweighting as well as statistical revisions."),
        nbformat.v4.new_code_cell("display(overlap.groupby('okved_section').agg(overlap=('changed','size'), changed=('changed','sum')))\ndisplay(pd.DataFrame([{'file':s['source_file'], 'vintage':s['source_vintage'], 'sha256':s['sha256']} for s in sources]))\nassert annual.loc[annual.period.dt.year.ge(2020),'source_vintage'].eq('2026-08-26').all()\nassert np.allclose(annual.growth_yoy_pct, annual.index_value-100, equal_nan=True)"),
        nbformat.v4.new_markdown_cell("## Social spending: hand calculation and scope checks\n\nThree disjoint parent functions (1000, 0700, 0900); expenditure shares use the complete expenditure total. Q1 and H1 are never compared with each other. Nominal YoY requires the same window one calendar year earlier and a positive prior amount."),
        nbformat.v4.new_code_cell("from macro_rus.fiscal_view import load_candidate\nfiscal, _ = load_candidate(ROOT / 'outputs/regional_concordance_2026-09-29_v2/fiscal_preview')\nfor region in ['RU-KLU','RU-SVE']:\n for date in ['2026-03-31','2026-06-30']:\n  sample = social.loc[social.region_id.eq(region) & social.period.eq(date)]\n  for _, row in sample.iterrows():\n   raw = fiscal.loc[fiscal.region_id.eq(region) & fiscal.period.eq(date) & fiscal.metric_code.eq(row.metric_code) & fiscal.kind.eq('expenditure_function')].iloc[0]\n   assert row.actual_ytd_rub == raw.actual_ytd_rub\n   assert np.isclose(row.share_pct, raw.actual_ytd_rub/row.total_expenditure_rub*100)\n   assert np.isclose(row.growth_yoy_pct, (raw.actual_ytd_rub/row.prior_year_actual_rub-1)*100)\ndisplay(social.loc[social.region_id.isin(['RU-KLU','RU-SVE']) & social.period.eq('2026-06-30'), ['region_id','function','actual_ytd_rub','share_pct','growth_yoy_pct']])\nprint('PASS: eligible regions, fiscal scope, grain, source hashes, missingness and hand calculations')")]
    notebook = nbformat.v4.new_notebook(cells=cells, metadata={"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}})
    nbformat.write(notebook, output / "source_audit.ipynb")
    print(json.dumps({k: v for k, v in summary.items() if k not in ["sources", "limitations"]}, indent=2))


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else ROOT / "outputs/annual_social_candidate_2026-10-02")
