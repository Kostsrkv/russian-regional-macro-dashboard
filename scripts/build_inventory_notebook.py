"""Create an inspectable companion for the bounded local inventory audit."""
from pathlib import Path
import nbformat as nbf
from nbclient import NotebookClient
from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpec
import sys
import os

root=Path(__file__).resolve().parents[1]
out=root/'outputs'/os.environ.get('MACRO_AUDIT_OUTPUT','data_inventory_2026-09-26')
nb=nbf.v4.new_notebook(cells=[
 nbf.v4.new_markdown_cell('# Regional macro data inventory\n\n## Summary\nThis is a bounded inventory, not certification of all raw files. Access, parsing, and economic validity are different checks. No source files or promoted datasets were modified. The current output directory identifies the run.'),
 nbf.v4.new_markdown_cell('## Context and methods\nThe audit enumerates project data/document files, excluding the Git directory, virtual environment, bytecode, and audit outputs. It records file sizes and cloud state, hashes readable files, checks CSV shape and exact schema headers, and profiles readable Parquet tables. Source reads have an eight-second timeout.\n\n### Key assumptions\nFilename dates identify release labels, not independently verified reporting periods. A missing calendar snapshot does not prove FNS published it. Filesystem timestamps are not source retrieval dates.\n\nTo refresh the underlying evidence, run `python scripts/inventory_audit.py --probe-cloud` from the project root after downloading the folder. Required packages: pandas, openpyxl, pyarrow; xlrd for legacy Excel.'),
 nbf.v4.new_code_cell("from pathlib import Path\nimport json\nimport pandas as pd\nroot = Path.cwd()\naudit = root / " + repr(str(out.relative_to(root))) + "\nsummary = json.loads((audit / 'summary.json').read_text())\nprint(json.dumps(summary, indent=2))"),
 nbf.v4.new_markdown_cell('## Data and coverage'),
 nbf.v4.new_code_cell("inventory = pd.read_csv(audit / 'file_inventory.csv')\nprint(inventory.groupby(['family', 'read_status']).size().to_string())\nprint('\\nContent-inspection errors:')\nprint(inventory.loc[inventory.read_status.eq('error'), ['path','error']].head(8).to_string(index=False))"),
 nbf.v4.new_markdown_cell('## Results\nExact row counts and missingness below refer only to successfully read promoted files.'),
 nbf.v4.new_code_cell("profiles = json.loads((audit / 'promoted_profiles.json').read_text())\nfor p in profiles:\n    print(p['table'], 'rows=',p['rows'], 'duplicate keys=',p.get('duplicate_grain_rows'))\n    print('Missing:', {k:v for k,v in p['null_counts'].items() if v})\n    print('Periods:', len(p.get('period',[])), 'Regions:', p.get('region_id',[]))\nassert len(inventory) == summary['files']\nfns = inventory[inventory.release_label.notna()]\nassert fns.release_label.nunique() == summary['fns_distinct_releases']\nassert len(set(fns.loc[~fns.matching_schema_present.fillna(False).astype(bool),'schema_label'])) == summary['fns_missing_schema_count']"),
 nbf.v4.new_markdown_cell('## Download priorities'),
 nbf.v4.new_code_cell("checklist = pd.read_csv(audit / 'download_checklist.csv')\nprint(checklist.groupby(['priority','source']).size().to_string())\nprint(checklist.head(12)[['priority','source','file','reason']].to_string(index=False))"),
 nbf.v4.new_markdown_cell('## Takeaways\nComplete missing FNS schemas before interpreting historical columns. Do not interpret missing CPI or quarter flows as zero. Readability does not establish semantic correctness: inspect malformed historical CSVs and archive CRC findings separately. No data was uploaded or shared.'),
])
if (out/'reconciliation.json').exists():
    nb.cells.insert(-1, nbf.v4.new_code_cell("reconciliation = json.loads((audit / 'reconciliation.json').read_text())\nfor name, result in reconciliation['tables'].items():\n    print(name, {k:v for k,v in result.items() if k != 'periods'})\n    assert result['different_or_unmatched_rows'] == 0\nprint('Rebuild reproduces current pipeline values; this is not independent certification of methodology.')"))
nb.metadata['kernelspec']={'name':'python3','display_name':'Python 3','language':'python'}
for cell in nb.cells:
    cell.source = cell.source.replace('eight-second timeout', '30-second timeout')
    cell.source = cell.source.replace("inventory.loc[inventory.read_status.eq('error'), ['path','error']]", "inventory.reindex(columns=['path','error','read_status']).loc[inventory.read_status.eq('error'), ['path','error']]")
nbf.validate(nb)
km=KernelManager();km._kernel_spec=KernelSpec(argv=[sys.executable,'-m','ipykernel_launcher','-f','{connection_file}'],display_name='Python 3',language='python')
client=NotebookClient(nb,km=km,timeout=90,resources={'metadata':{'path':str(root)}})
client.execute()
nbf.write(nb,out/'inventory_audit.ipynb')
print('Notebook validated and executed:',out/'inventory_audit.ipynb')
