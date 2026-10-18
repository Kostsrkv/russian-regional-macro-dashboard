"""Executable companion for regional concordance and macro candidate checks."""
from pathlib import Path
import sys
import nbformat as nbf
from nbclient import NotebookClient
from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpec

ROOT = Path(__file__).resolve().parents[1]
out = ROOT / "outputs/expanded_macro_candidate_2026-09-29_v2"
nb = nbf.v4.new_notebook(cells=[
 nbf.v4.new_markdown_cell("# Expanded regional macro candidate\n\n## tl;dr\nThe fiscal preview covers 78 regions. Tax and production extraction is staged separately. Mining is absent from newer FNS schemas, so aggregate PIT growth across the coverage change must not be shown. No promotion or deployment."),
 nbf.v4.new_markdown_cell("## Context & Methods\nExact/typography-normalized source labels plus six explicit Rosstat aliases link Treasury to the latest FNS and Rosstat files. Five overlapping-territory cases and eight separate-geography cases are held back.\n\n### Key assumptions\nProject IDs based on FNS codes are not ISO codes. This mapping does not certify unchanged historical boundaries or completeness. Missing values stay missing. The macro adapter uses exact matching FNS schemas and source-native monthly industrial indices. Audit code: `scripts/build_regional_concordance.py` and `scripts/build_expanded_macro_candidate.py`."),
 nbf.v4.new_code_cell("from pathlib import Path\nimport json\nimport pandas as pd\nroot = Path.cwd()\nout = root / 'outputs/expanded_macro_candidate_2026-09-29_v2'\nmanifest = json.loads((out/'manifest.json').read_text())\nfns = pd.read_parquet(out/'pit_receipts.parquet')\nip = pd.read_parquet(out/'industrial_production.parquet')\ncrosswalk = pd.read_csv(root/'outputs/regional_concordance_2026-09-29_v2/region_concordance.csv')\nprint(crosswalk.groupby('status').size().to_string())\nprint(json.dumps(manifest['tables'], indent=2))"),
 nbf.v4.new_markdown_cell("## Data\n`coverage.csv` lists each region's coverage. `sources.csv` retains filenames, hashes and source vintages. The concordance folder also contains sheet/row evidence for Rosstat labels. English names are not invented for new regions; original Russian names are retained."),
 nbf.v4.new_code_cell("assert crosswalk.eligible_for_candidate_profile.sum() == 78\nassert not crosswalk.eligible_for_national_aggregation.any()\nassert not fns.duplicated(['region_id','period','okved_section','source_vintage']).any()\nassert not ip.duplicated(['region_id','period','okved_section','source_vintage']).any()\nassert all(v['mismatches']==0 for v in manifest['pilot_parity'].values())\nprint('Pilot control comparisons:', manifest['pilot_parity'])\nprint('Missing derived PIT flows:', int(fns.pit_flow_rub.isna().sum()))"),
 nbf.v4.new_markdown_cell("## Results\nSection counts below expose the change in coverage rather than treating an absent sector as zero."),
 nbf.v4.new_code_cell("coverage = fns.groupby(['period','okved_section']).region_id.nunique().unstack()\nprint(coverage[['B','C','D','E']].to_string())\nassert coverage.loc[pd.Timestamp('2025-03-31'),'B'] == 78\nassert pd.isna(coverage.loc[pd.Timestamp('2026-03-31'),'B'])\nlatest=ip[ip.period.eq('2026-07-31')]\nmissing=[]\nfor region, group in latest.groupby('region_id'):\n    for section in sorted(set('BCDE')-set(group.okved_section)):\n        missing.append({'region':region,'section':section})\nprint('Missing latest industrial sections:',missing)"),
 nbf.v4.new_markdown_cell("## Takeaways\nThe fiscal layer can be explored now. Before broad macro pages are enabled, reconcile tax coverage and reported totals, assess missing/suppressed production values and distinguish monthly indices from quarterly tax flows. More source dates cannot compensate for absent schema fields. This notebook uses small diagnostic tables, not economic rankings."),
])
nb.metadata["kernelspec"]={"name":"python3","display_name":"Python 3","language":"python"}
nbf.validate(nb)
km=KernelManager()
km._kernel_spec=KernelSpec(argv=[sys.executable,"-m","ipykernel_launcher","-f","{connection_file}"], display_name="Python 3",language="python")
NotebookClient(nb,km=km,timeout=90,resources={"metadata":{"path":str(ROOT)}}).execute()
nbf.write(nb,out/"expanded_macro_audit.ipynb")
print("Executed",out/"expanded_macro_audit.ipynb")
