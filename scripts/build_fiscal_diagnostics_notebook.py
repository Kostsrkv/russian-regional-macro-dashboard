"""Author and execute a bounded companion notebook for the fiscal diagnostics."""
from __future__ import annotations

import argparse
from pathlib import Path

import nbformat
from nbclient import NotebookClient


def create(package: Path, project: Path, kernel: str):
    notebook = nbformat.v4.new_notebook()
    notebook.metadata.kernelspec = {"name": kernel, "display_name": "Russian regional fiscal diagnostics", "language": "python"}
    md = nbformat.v4.new_markdown_cell
    code = nbformat.v4.new_code_cell
    notebook.cells = [
        md("# Regional fiscal accounting diagnostics\n\n## tl;dr\n\nThis companion reproduces five non-overlapping revenue components and their contribution to changes in actual regional budget balances. Results retain research-review status. Exact coverage and pilot values are printed below; no real-growth, debt-stock, military-contract or plan-shortfall claim is made."),
        md("## Context & Methods\n\n### Key assumptions\n\n- Nominal RUB, consolidated regional plus municipal budgets; territorial health-insurance funds excluded.\n- Q1 is compared with Q1, H1 with H1; Q2 = same-year H1 minus Q1. No interpolation.\n- Other own-source revenue = code 100 minus retained PIT and CIT. Other non-repayable receipts = code 200 minus code 202. Signed adjustments stay signed.\n- Five revenue changes sum to total revenue change; adding negative expenditure change gives the change in balance. This is accounting, not causality.\n- Transfers are from other budgets, not proven federal-only. Fiscal assignments at a cutoff do not establish original/revised budget paths."),
        code(f"""from pathlib import Path
import hashlib, json, os, sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from IPython.display import display, Image

PROJECT_ROOT = Path({str(project)!r})
PACKAGE = PROJECT_ROOT / {str(package.relative_to(project))!r}
EXECUTION_SOURCE = Path(os.environ.get('MACRO_RUS_ANALYSIS_SOURCE', str(PROJECT_ROOT / 'src')))
sys.path.insert(0, str(EXECUTION_SOURCE))
from macro_rus.fiscal_diagnostics import revenue_component_history, detailed_balance_bridge
from macro_rus.fiscal_view import load_candidate
from macro_rus.revenue_view import load_revenue_candidate
summary = json.loads((PACKAGE / 'summary.json').read_text())
print('Source release:', summary['source_release'])
print('Reporting periods:', ', '.join(summary['periods']))
print('Execution source:', EXECUTION_SOURCE)
print('Publication and promotion: not performed')
plt.rcParams.update({{'font.family': 'DejaVu Sans', 'font.size': 11, 'axes.spines.top': False, 'axes.spines.right': False}})
"""),
        md("## Data\n\n### Verify the immutable processed inputs\n\nThe package contains copies of the six fiscal/revenue payloads from the pinned 5 October research release. Original source archives are unchanged. Hash checks validate extraction inputs, not independent source authenticity."),
        code("""release = json.loads((PACKAGE / 'inputs' / 'manifest.json').read_text())
for name in ('fiscal/fiscal_observations.csv.gz', 'fiscal/manifest.json',
             'revenue/budget_execution.parquet', 'revenue/sources.csv',
             'revenue/lineage.csv.gz', 'revenue/manifest.json'):
    content = (PACKAGE / 'inputs' / name).read_bytes()
    assert hashlib.sha256(content).hexdigest() == release['payloads'][name]['sha256']
fiscal, fiscal_manifest = load_candidate(PACKAGE / 'inputs' / 'fiscal')
revenue, revenue_manifest = load_revenue_candidate(PACKAGE / 'inputs' / 'revenue')
print(f'{fiscal.region_id.nunique()} regions; {fiscal.region_id.nunique() * fiscal.period.nunique()} region-cutoffs; six input hashes matched')
display(pd.DataFrame({'Fiscal records': [len(fiscal)], 'Revenue records': [len(revenue['budget_execution'])]}))
"""),
        md("## Results\n\n### Recompute all revenue components and check the saved analysis"),
        code("""histories = [revenue_component_history(revenue, basis) for basis in ('YTD', 'period_flow')]
components = pd.concat(histories, ignore_index=True)
saved = pd.read_csv(PACKAGE / 'revenue_components.csv', parse_dates=['period', 'prior_period'])
keys = ['region_id', 'period', 'amount_basis', 'component_code']
check = components[keys + ['amount_rub', 'change_yoy_rub']].merge(
    saved[keys + ['amount_rub', 'change_yoy_rub']], on=keys, validate='one_to_one', suffixes=('_run', '_saved'))
for measure in ('amount_rub', 'change_yoy_rub'):
    np.testing.assert_allclose(check[measure + '_run'], check[measure + '_saved'], atol=.02, rtol=0, equal_nan=True)
print('Recomputed components:', len(components))
print('Available detailed bridges:', summary['available_bridges'])
print('Unavailable bridges:', summary['unavailable_bridges'], '(prior-year windows unavailable, not zero)')
print('Signed negative component levels retained:', summary['negative_component_levels_preserved'])
"""),
        md("### Initial pilot profiles\n\nKaluga and Sverdlovsk are retained as transparent inspection cases, not selected because of an outcome and not presented as a ranking. Each history uses the same H1 window."),
        code("""latest = pd.Timestamp(summary['latest_period'])
pilots = {'RU-KLU': 'Kaluga Oblast', 'RU-SVE': 'Sverdlovsk Oblast'}
profiles = pd.read_csv(PACKAGE / 'latest_regional_fiscal_profiles.csv', parse_dates=['period'])
preview = profiles.loc[profiles.region_id.isin(pilots), ['region_id', 'revenue_rub', 'expenditure_rub', 'balance_rub', 'balance_change_rub']].copy()
for name in preview.columns[1:]:
    preview[name] = preview[name] / 1e9
preview = preview.rename(columns={'region_id': 'Region', 'revenue_rub': 'Revenue (RUB bn)',
    'expenditure_rub': 'Spending (RUB bn)', 'balance_rub': 'Balance (RUB bn)', 'balance_change_rub': 'YoY balance change (RUB bn)'})
preview['Region'] = preview.Region.map(pilots)
display(preview.round(3))
fig, axes = plt.subplots(2, 2, figsize=(13, 9), layout='constrained')
fig.suptitle('Regional fiscal accounts · nominal RUB · independent regional scales', fontsize=13)
fig.supxlabel('Source: Federal Treasury · Q1/H1 2023–2026 · research-review data' + chr(10) + 'Accounting contributions, not causal effects; regional + municipal budgets excluding health-insurance funds.', fontsize=9)
for row_index, (region_id, name) in enumerate(pilots.items()):
    history = fiscal.loc[fiscal.region_id.eq(region_id) & fiscal.period.dt.month.eq(latest.month)
                         & fiscal.kind.eq('summary') & fiscal.metric_code.isin(['revenue_total', 'expenditure_total'])]
    values = history.pivot(index='period', columns='metric_code', values='actual_ytd_rub').sort_index() / 1e9
    x = np.arange(len(values))
    axis = axes[row_index, 0]
    axis.bar(x - .18, values.revenue_total, width=.36, color='#2455A4', label='Revenue')
    axis.bar(x + .18, values.expenditure_total, width=.36, color='#C66A19', label='Spending')
    axis.set_xticks(x, values.index.strftime('%Y'))
    axis.set_title(name + ': same H1 window')
    axis.set_ylabel('Nominal RUB bn')
    axis.set_ylim(bottom=0)
    axis.legend(frameon=False)
    bridge, reason = detailed_balance_bridge(fiscal, revenue, region_id, latest)
    assert reason is None
    change = bridge.loc[bridge.role.eq('change')].copy()
    axis = axes[row_index, 1]
    short_labels = ['Retained PIT', 'Corporate income tax', 'Other own-source', 'Transfers', 'Other non-repayable', 'Expenditure effect']
    plotted = change.value_rub.to_numpy() / 1e9
    axis.barh(short_labels, plotted, color='#2455A4')
    axis.axvline(0, color='#475569', linewidth=.8)
    axis.invert_yaxis()
    axis.set_title(name + f': H1 {latest.year} minus H1 {latest.year - 1}')
    axis.set_xlabel('Contribution to balance change (RUB bn)')
    axis.margins(x=.30)
    for index, value in enumerate(plotted):
        axis.annotate(f'{value:+.2f}', (value, index), xytext=(4 if value >= 0 else -4, 0),
                      textcoords='offset points', ha='left' if value >= 0 else 'right', va='center')
figure_path = PACKAGE / 'pilot_fiscal_diagnostics.png'
fig.savefig(figure_path, dpi=150)
plt.close(fig)
display(Image(filename=str(figure_path)))
"""),
        md("### Independent reconciliation and missingness checks"),
        code("""complete = components.loc[components.comparison_complete]
assert complete.change_reconciliation_gap_rub.abs().max() <= .02
bridges = pd.read_csv(PACKAGE / 'detailed_balance_bridges.csv')
for _, group in bridges.groupby(['region_id', 'period', 'amount_basis']):
    assert abs(group.loc[group.role.eq('change'), 'value_rub'].sum() - group.balance_change_rub.iloc[0]) <= .02
    assert group.quality_status.eq('candidate_not_promoted').all()
coverage = pd.read_csv(PACKAGE / 'bridge_coverage.csv')
display(coverage.groupby('amount_basis').available.agg(['count', 'sum']).rename(columns={'count': 'Windows inspected', 'sum': 'Bridges available'}))
print('All saved bridges reconcile; absent prior-year comparisons remain unavailable.')
"""),
        md("## Takeaways\n\nThe outputs identify which reported revenue components account for movement in actual balances, alongside expenditure changes. They do not establish why those receipts changed.\n\nNext: prepare concise regional notes from the checked profiles and combine them with source-native production and fuel context. CPI, wages, debt stocks and original/amended budget paths remain missing inputs. No national totals or rankings are produced."),
    ]
    destination = package / "fiscal_diagnostics.ipynb"
    nbformat.validate(notebook)
    NotebookClient(notebook, kernel_name=kernel, timeout=300,
                   resources={"metadata": {"path": str(project)}}).execute()
    nbformat.validate(notebook)
    nbformat.write(notebook, destination)
    print(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--kernel", default="python3")
    args = parser.parse_args()
    create(args.package.resolve(), args.project.resolve(), args.kernel)
