"""Build and execute the compact companion to the full catalogue audit."""
from pathlib import Path
import sys
import nbformat as nbf
from nbclient import NotebookClient
from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpec

ROOT = Path(__file__).resolve().parents[1]
folder = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "outputs/treasury_coverage_2026-09-29"
nb = nbf.v4.new_notebook(cells=[
    nbf.v4.new_markdown_cell("# Treasury coverage audit\n\n## tl;dr\nThis notebook checks the saved catalogue-wide extraction. Accounting passage is not approval for a national map. See computed coverage and exceptions below; nothing is promoted."),
    nbf.v4.new_markdown_cell("## Context & Methods\nEvery catalogue entry in eight local Treasury archives is attempted, retaining errors rather than skipping them silently. Grain: source-territory label × reporting period × metric. Values are nominal RUB, year-to-date.\n\n### Key assumptions\nSource-local hashed labels are not official region IDs. Catalogue positions are not region codes. No national total is computed; territorial scope and overlapping budgets need review. Snapshot plans are not original budgets. Rebuild with `python scripts/audit_treasury_coverage.py NEW_OUTPUT_DIRECTORY`."),
    nbf.v4.new_code_cell("from pathlib import Path\nimport json\nimport pandas as pd\naudit = Path(" + repr(str(folder)) + ")\nsummary = json.loads((audit / 'summary.json').read_text())\ncoverage = pd.read_csv(audit / 'coverage.csv')\nobservations = pd.read_csv(audit / 'staging_observations.csv', dtype={'metric_code': str})\nchecks = pd.read_csv(audit / 'checks.csv')\nprint({k:summary[k] for k in ['archives','source_territories','territory_periods','observations','status_counts','plan_warning_count','absent_territory_periods']})"),
    nbf.v4.new_markdown_cell("## Data\nArchive identities, SHA-256 hashes, source HTML members and cutoff dates are preserved in the CSVs and summary. Cutoffs are derived from filenames; source publication/retrieval dates remain unknown."),
    nbf.v4.new_code_cell("print(coverage.groupby(['period','status']).size().unstack(fill_value=0).to_string())\nassert len(coverage) == summary['territory_periods']\nassert len(observations) == summary['observations']\nassert not coverage.duplicated(['territory_key','period']).any()\nassert not observations.duplicated(['territory_key','period','kind','metric_code']).any()\nassert observations[['actual_ytd_rub','source_sha256','source_member']].notna().all().all()"),
    nbf.v4.new_markdown_cell("## Results\nThese compact count tables are the audit result, not economic rankings. Full exceptions are retained in the coverage and checks CSVs."),
    nbf.v4.new_code_cell("print(checks.groupby(['check','blocking','passed']).size().to_string())\nfailed = coverage[coverage.status.ne('accounting_passed')]\nprint('\\nFailed territory-periods:', len(failed))\nprint(failed[['source_territory_name_ru','period','status','error']].head(12).to_string(index=False))\nexpected = coverage.source_territory_name_ru.nunique() * coverage.period.nunique()\nassert expected - len(coverage) == summary['absent_territory_periods']"),
    nbf.v4.new_markdown_cell("## Takeaways\nUse only eligible observations after reviewing the exceptions and completing an explicit region concordance. Do not sum all source territories or interpret financing as debt stock. Plan discrepancies remain separate from actual-accounting checks. The pilot dashboard is unchanged."),
])
nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
nbf.validate(nb)
km = KernelManager()
km._kernel_spec = KernelSpec(argv=[sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"], display_name="Python 3", language="python")
NotebookClient(nb, km=km, timeout=90, resources={"metadata": {"path": str(ROOT)}}).execute()
nbf.write(nb, folder / "coverage_audit.ipynb")
print("Executed and saved", folder / "coverage_audit.ipynb")
