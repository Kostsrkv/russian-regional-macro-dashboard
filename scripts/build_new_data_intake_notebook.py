"""Build and execute a small companion audit without importing broken nbformat.

Uses the installed, official nbformat 4.5 JSON schema via fastjsonschema.
Code cells execute sequentially in one namespace, not in a Jupyter kernel.
Inputs and the application release are read-only; only audit outputs are written.
"""

from __future__ import annotations

import contextlib
import html
import io
import json
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import fastjsonschema


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/new_data_intake_2026-10-05"


def markdown(cell_id, source):
    return {"id": cell_id, "cell_type": "markdown", "metadata": {},
            "source": textwrap.dedent(source).strip()}


def code(cell_id, source):
    return {"id": cell_id, "cell_type": "code", "metadata": {},
            "source": textwrap.dedent(source).strip(), "execution_count": None,
            "outputs": []}


def render_markdown(source):
    """Render the companion's simple headings, paragraphs and lists locally."""
    blocks, paragraph, items = [], [], []

    def inline(value):
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(value))

    def flush():
        if paragraph:
            blocks.append('<p>' + inline(' '.join(paragraph)) + '</p>')
            paragraph.clear()
        if items:
            blocks.append('<ul>' + ''.join('<li>' + inline(item) + '</li>' for item in items) + '</ul>')
            items.clear()

    for line in source.splitlines():
        heading = re.match(r"^(#{1,3})\s+(.+)", line)
        if heading:
            flush()
            level = len(heading[1])
            blocks.append(f'<h{level}>' + inline(heading[2]) + f'</h{level}>')
        elif not line.strip():
            flush()
        elif line.startswith('- '):
            if paragraph:
                flush()
            items.append(line[2:])
        elif items:
            items[-1] += ' ' + line.strip()
        else:
            paragraph.append(line.strip())
    flush()
    return '\n'.join(blocks)


def main():
    prices = json.loads((ROOT / "outputs/new_prices_audit_2026-10-05/summary.json").read_text())
    fns = json.loads((ROOT / "outputs/new_fns_audit_2026-10-05/summary.json").read_text())
    cells = [
        markdown("summary", f"""
        # New regional data: intake review, 5 October 2026

        ## tl;dr
        The core fuel-price candidate contains {prices['eligible_core_fuel_observations_expected']:,}
        observations for {prices['mapped_eligible_regions']} eligible regions and
        {prices['months']} months, January 2022–August 2026, with no missing core prices.
        Fuel levels are not CPI. Large monthly movements are screening flags for review,
        not corrected observations. The new FNS schemas do not establish PIT identity
        or match the existing wide H1/nine-month files; no PIT history is promoted.
        The WU record is historical catalogue metadata, not a current CPI export.
        Raw files and the live dashboard release remain unchanged.
        """),
        markdown("methods", """
        ## Context & Methods
        This is a data-quality and readiness companion, not an economic forecast.
        It rechecks grain, monthly coverage, identifiers, numeric values, source hashes,
        selected original-cell lineage and FNS schema agreement. Diagnostic tables
        suffice for these checks; no economic chart is warranted.

        ### Key Assumptions
        - Eligible geography follows the existing 78-region project concordance,
          not a claim of complete national coverage.
        - The month key identifies a monthly observation. The workbooks state
          end-of-period average consumer prices, not a daily price or monthly CPI.
        - National, district, regional and surveyed-city observations are separate.
          No city average is substituted for a regional published observation.
        - A >10% absolute month-on-month change is only a transparent review screen.
          Source-reported positive prices are not automatically discarded.
        - Original download URLs/times and publication dates were not supplied for
          every local file. A SHA-256 hash identifies bytes, not authenticity or licensing.
        """),
        markdown("data", """
        ## Data
        Inputs: five `sred_potreb_cen_*.xlsx` workbooks; their source manifest, candidate
        Parquet and raw-cell checks in `outputs/new_prices_audit_2026-10-05`; FNS CSVs
        and evidence in `outputs/new_fns_audit_2026-10-05`; current release pointer
        `data/dashboard/current.json`; WU review in `wu_catalogue_review.json` alongside
        this notebook. Reference catalogues and the 2007 CPI manual are not observations.

        Rebuild source evidence with the two `audit_new_*_2026_10_05.py` scripts.
        Then run `.venv/bin/python scripts/build_new_data_intake_notebook.py` from
        the project root. Requirements: pandas, a Parquet engine and fastjsonschema
        in the existing project environment. No network access or new downloads.
        """),
        code("load", """
        from pathlib import Path
        import csv, hashlib, json
        import pandas as pd

        root = next(p for p in [Path.cwd(), *Path.cwd().parents]
                    if (p / 'scripts/audit_new_prices_2026_10_05.py').is_file())
        price_out = root / 'outputs/new_prices_audit_2026-10-05'
        fns_out = root / 'outputs/new_fns_audit_2026-10-05'
        prices = json.loads((price_out / 'summary.json').read_text())
        fns = json.loads((fns_out / 'summary.json').read_text())
        panel = pd.read_parquet(price_out / 'fuel_prices_eligible_regions_candidate.parquet')
        current = json.loads((root / 'data/dashboard/current.json').read_text())
        assert current['release_path'] == 'releases/2026-10-02-cloud-v1'
        print('Current release:', current['release_path'])
        print('Audit status:', prices['status'], '|', fns['status'])
        print('Candidate regional fuel rows:', len(panel))
        """),
        markdown("results", """
        ## Results
        ### 1. Core fuel coverage and grain
        Use the declared grade codes 7802 (AI-92), 7803 (AI-95) and 7804 (diesel).
        Prices are RUB/litre. Identifiers and product labels, not fixed columns,
        determine the join; header positions shift between workbooks.
        """),
        code("fuel-grain", """
        core_codes = {'7802', '7803', '7804'}
        core = panel[panel.product_code.isin(core_codes)].copy()
        assert panel.geography_scope.eq('region').all()
        assert panel.eligible_for_existing_dashboard.all()
        assert not panel.duplicated(['region_id', 'period', 'product_code']).any()
        concordance = pd.read_csv(root / 'outputs/regional_concordance_2026-09-29_v2/region_concordance.csv', dtype=str)
        eligible = set(concordance.loc[concordance.eligible_for_candidate_profile.eq('True'), 'region_id'].dropna())
        periods = pd.date_range('2022-01-01', '2026-08-01', freq='MS')
        assert set(core.region_id) == eligible and len(eligible) == 78
        assert set(core.period) == set(periods) and len(periods) == 56
        assert len(core) == len(eligible) * len(periods) * len(core_codes)
        assert core.price_rub_per_litre.notna().all()
        assert core.price_rub_per_litre.gt(0).all()
        assert core.groupby(['region_id', 'product_code']).size().eq(56).all()
        print(core.groupby('fuel_grade').agg(
            observations=('period', 'size'), regions=('region_id', 'nunique'),
            months=('period', 'nunique')).to_string())
        print('PASS: unique region × month × fuel, complete consecutive months, positive prices')
        """),
        markdown("price-review", """
        ### 2. Missing values and temporal screening
        The optional AI-98+ series is not universally observed. Price movements are
        computed only for the same region/grade and genuinely adjacent months.
        A screening flag does not establish an error, fuel shortage or cause.
        """),
        code("fuel-screen", """
        missing = panel[panel.price_rub_per_litre.isna()]
        assert len(missing) == 112 and missing.product_code.eq('7806').all()
        print('Optional missing AI-98+ observations:')
        print(missing.groupby('region_id').size().rename('missing_months').to_string())
        ordered = core.sort_values(['region_id', 'product_code', 'period']).copy()
        keys = ['region_id', 'product_code']
        grouped = ordered.groupby(keys)
        prior_price = grouped.price_rub_per_litre.shift(1)
        prior_period = grouped.period.shift(1)
        month_number = ordered.period.dt.year * 12 + ordered.period.dt.month
        prior_month_number = prior_period.dt.year * 12 + prior_period.dt.month
        adjacent = month_number.sub(prior_month_number).eq(1) & prior_price.gt(0)
        ordered['mom_pct'] = ordered.price_rub_per_litre.div(prior_price).sub(1).mul(100).where(adjacent)
        flagged = ordered[ordered.mom_pct.abs().gt(10)].copy()
        print('Matched adjacent-month changes:', int(ordered.mom_pct.notna().sum()))
        print('Review flags |MoM| > 10%:', len(flagged))
        print('Largest rise/fall (%):', round(ordered.mom_pct.max(), 2), '/', round(ordered.mom_pct.min(), 2))
        assert len(flagged) == 120
        print('Values retained; screening flags are not corrections.')
        """),
        markdown("lineage", """
        ### 3. Raw-source immutability and extraction checks
        Independent raw XLSX XML checks compare saved numeric cells with parser outputs.
        Candidate rows retain filename, SHA-256, sheet and cell.
        """),
        code("source-checks", """
        def sha256(path):
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            return digest.hexdigest()

        sources = json.loads((price_out / 'source_manifest.json').read_text())
        assert all(sha256(root / rec['filename']) == rec['sha256'] for rec in sources)
        assert all(sha256(root / rec['file']) == rec['sha256'] for rec in fns['profiles'])
        checks = json.loads((price_out / 'independent_raw_cell_checks.json').read_text())
        assert len(checks) == prices['independent_raw_cell_checks'] == 164
        assert all(check['passed'] for check in checks)
        for field in ['source_file', 'source_sha256', 'source_sheet', 'source_cell']:
            assert core[field].notna().all()
        manifest_hashes = {rec['filename']: rec['sha256'] for rec in sources}
        assert all(manifest_hashes[name] == digest for name, digest in
                   core[['source_file', 'source_sha256']].drop_duplicates().itertuples(index=False, name=None))
        print('Source hashes unchanged:', len(sources) + len(fns['profiles']), 'files')
        print('Independent parser-vs-raw-XML checks:', len(checks), 'passed')
        print('Core source-cell lineage: complete')
        """),
        markdown("tax-checks", """
        ### 4. FNS field identity, not just parseability
        Dictionary field counts must agree with raw data headers, and tax-measure
        definitions must establish PIT. A matching filename or an all-zero numerical
        fingerprint cannot identify an unknown tax measure.
        """),
        code("fns-schema", """
        widths = {}
        for cutoff in ['20250101', '20250701', '20251001']:
            with (root / f'structure-{cutoff}.csv').open(encoding='utf-8-sig', newline='') as stream:
                schema = list(csv.DictReader(stream))
            widths[cutoff] = len(schema)
            assert len({r['field name'] for r in schema}) == len(schema)
            assert not any('налог на доходы физических лиц' in r['russian description'].lower()
                           for r in schema)
            print(cutoff, '| schema fields:', len(schema), '| explicit PIT descriptions: 0')
        assert widths == {'20250101': 105, '20250701': 91, '20251001': 91}
        for cutoff, expected in [('20250701', 2818), ('20251001', 2786)]:
            with (root / f'data-{cutoff}-structure-{cutoff}.csv').open(encoding='utf-8-sig', newline='') as stream:
                header = next(csv.reader(stream, delimiter=','))
            assert len(header) == expected and len(header) != widths[cutoff]
            print(cutoff, '| existing data fields:', len(header), '| new schema mismatch confirmed')
        replacement = next(p for p in fns['profiles'] if p['file'].endswith('copy 3.csv'))
        assert replacement['malformed_width_rows'] == 10
        assert replacement['duplicate_region_keys'] == 7
        assert fns['numeric_fingerprint_summary']['nonzero_narrow_columns_with_exact_vector_matches'] == 0
        print('FY2024 replacement: malformed records:', replacement['malformed_width_rows'],
              '| conflicting regional keys:', replacement['duplicate_region_keys'])
        print('Verified PIT flows restored by this batch: none')
        """),
        markdown("catalogue", """
        ### 5. WU catalogue evidence and CPI limitation
        The catalogue identifies consumer-price indicator EMISS 31074 but its
        advertised archived XLS/SDMX downloads return HTML. Metadata dates and year
        filters do not validate observation coverage. Current CPI export access
        was not achieved. This notebook makes no current inflation estimates.
        """),
        code("wu-record", """
        wu = json.loads((root / 'outputs/new_data_intake_2026-10-05/wu_catalogue_review.json').read_text())
        assert wu['integration_status'] == 'metadata_only_not_observations'
        assert all(r['inspected_format'] == 'html' for r in wu['archived_distributions'])
        print('Catalogue dataset:', wu['title_ru'])
        print('Original indicator:', wu['original_indicator'])
        print('Metadata publication / modification:', wu['metadata_published'], '/', wu['metadata_modified'])
        print('Archived distributions: HTML, not XLS or SDMX')
        print('Current regional CPI, wages and PPI were not supplied by this batch.')
        """),
        markdown("takeaways", """
        ## Takeaways
        - Fuel candidates can support a region-first RUB/litre page with dated
          levels and same-grade matched price changes after review of flagged moves.
        - AI-98+ absence must remain missing; national and city observations must
          never be merged into regional totals.
        - Product prices can describe selected-item price changes. They cannot
          produce official all-items/product CPI or deflate wages/PIT without
          separately validated indices and their appropriate bases.
        - FNS additions are held outside validated PIT. The missing July 2026 pair
          is not requested or fabricated; existing fiscal/production work can continue.
        - No release, Git push or deployment is performed by this audit.

        Execution disclosure: cells run sequentially in one Python namespace,
        without a Jupyter kernel. The official nbformat 4.5 JSON schema is validated
        with fastjsonschema because importing nbformat/nbclient in the local
        environment times out inside an optional URI-format dependency. Open the
        notebook in Jupyter and use Run All, or run
        `.venv/bin/python -m jupyter nbconvert --execute --to notebook --inplace
        outputs/new_data_intake_2026-10-05/intake_audit.ipynb`
        after repairing that environment to check full kernel execution.
        """),
    ]
    notebook = {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {"kernelspec": {"name": "python3", "language": "python", "display_name": "Python 3"},
                     "language_info": {"name": "python", "version": sys.version.split()[0]}},
        "cells": cells,
    }
    namespace = {"__name__": "__notebook__"}
    count = 0
    with contextlib.chdir(ROOT):
        for cell in cells:
            if cell["cell_type"] != "code":
                continue
            count += 1
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                exec(compile(cell["source"], f"intake-audit-cell-{count}", "exec"), namespace)
            cell["execution_count"] = count
            cell["outputs"] = [{"output_type": "stream", "name": "stdout", "text": output.getvalue()}]
    schema_path = ROOT / ".venv/lib/python3.12/site-packages/nbformat/v4/nbformat.v4.5.schema.json"
    fastjsonschema.compile(json.loads(schema_path.read_text()))(notebook)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "intake_audit.ipynb").write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")
    blocks = []
    for cell in cells:
        if cell["cell_type"] == "markdown":
            blocks.append('<section class="markdown">' + render_markdown(cell['source']) + '</section>')
        else:
            blocks.append('<details><summary>Code cell ' + str(cell['execution_count']) + '</summary><pre>' +
                          html.escape(cell['source']) + '</pre></details>')
            blocks.append('<section class="result"><pre>' + html.escape(cell['outputs'][0]['text']) + '</pre></section>')
    preview = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Regional data intake audit</title><style>body{font-family:system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 20px;line-height:1.6;color:#20242a;background:#fafafa}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}section{margin:18px 0}h1{font-size:26px}h2{font-size:21px;margin-top:30px}h3{font-size:18px}code,.result pre,details pre{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:14px}.result{border-left:3px solid #285993;padding:4px 14px;background:#f0f4f8}details{font-size:14px}summary{cursor:pointer}li{margin-bottom:8px}</style><main>' + '\n'.join(blocks) + '</main></html>'
    (OUT / "intake_audit_preview.html").write_text(preview, encoding="utf-8")
    status = {"executed_at_utc": datetime.now(timezone.utc).isoformat(),
              "execution": "sequential_shared_namespace_no_jupyter_kernel", "executed_code_cells": count,
              "format_validation": "official_nbformat_4_5_schema_via_fastjsonschema_passed",
              "saved_output_readback": "passed", "visual_inspection": "pending"}
    readback = json.loads((OUT / "intake_audit.ipynb").read_text())
    assert len([c for c in readback['cells'] if c['cell_type'] == 'code' and c['outputs']]) == count
    (OUT / "notebook_validation.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(json.dumps(status, indent=2))
    for cell in cells:
        if cell['cell_type'] == 'code':
            print(cell['outputs'][0]['text'])


if __name__ == "__main__":
    main()
