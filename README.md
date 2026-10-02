# Russian Regional Macro Dashboard

A region-first Streamlit dashboard for local review and Streamlit Community Cloud, monitoring three core signals:

1. FNS Form 1-NOM PIT receipts attributed to employers' main OKVED industry.
2. Rosstat regional industrial-production indices.
3. Treasury Form 0503317 regional revenue execution against the plan reported at each cutoff.

Kaluga and Sverdlovsk oblasts were the initial coverage pilot. The current published research release contains 78 eligible regions, with additional source-backed expenditure, social-spending, balance and financing views. Comparison is optional; the default view explains one selected region through its own history. All expanded datasets retain their provisional review status; publication is not independent verification.

## Analytical boundaries

- PIT by industry is a fiscal receipt proxy associated with the tax agent's registered main OKVED. It is not a direct output, employment, productivity, or welfare measure.
- Treasury plan values are the approved assignments reported at each cutoff and may already include amendments. A fiscal shortfall claim requires an original/revised-plan distinction and a historical seasonal baseline.
- Industrial production is shown as a separate cross-check. Divergence from PIT is not automatically attributed to sanctions or any other cause.
- Missing observations remain missing. The pipeline never interpolates unavailable monthly or quarterly releases.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m macro_rus.cli build --raw-dir . --output-dir data/promoted/current
.venv/bin/streamlit run app.py
```

The application reads the processed release pinned by `data/dashboard/current.json` and does not need raw workbooks, a VPN or live access to Russian websites. Explicit local directory overrides retain the private review workflow. Without a release pointer, the legacy promoted-data default remains available.

## Source workflow

Preserve downloaded files unchanged. The repeatable workflow is:

```text
download -> ingest -> validate -> analyst review -> promote -> run/deploy
```

Every promoted vintage includes a manifest with file hashes and validation results. To roll back, point the app at an earlier promoted directory through `MACRO_RUS_DATA_DIR`.

## Expected source files

- FNS open-data CSV releases named `data-YYYYMMDD-structure-YYYYMMDD.csv` and their matching schema files.
- Rosstat regional industrial-production XLSX workbooks (`ind_sub_*.xlsx` and, where needed, `ind_baza_*.xlsx`).
- Treasury `KBSRF`/`КБСРФ` outer ZIPs containing Form 0503317 Excel and HTML packages.

The FNS parser deliberately ingests a release only when its exact
`structure-YYYYMMDD.csv` schema is present. A newer schema is never applied to
an older release. The current research release contains 2025 Q1, 2025 year-end
and 2026 Q1 FNS snapshots. Quarterly comparisons require genuine matching
snapshots; full-year totals are not treated as Q4 flows. Incomplete mining PIT
coverage remains unavailable rather than being converted to zero.

Do not commit or redistribute raw source files unless their redistribution terms have been reviewed.

## Tests

```bash
.venv/bin/pytest
```

The executed data-quality companion is
`notebooks/data_quality_audit.ipynb`. Free-host setup and rollback instructions
are in `DEPLOYMENT.md`.
