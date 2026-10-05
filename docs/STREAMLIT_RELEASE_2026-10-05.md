# Streamlit fuel-preview release — 2026-10-05

## Scope

- Existing application: https://russian-regional-macro-dashboard-ek8aw68yuz7dgfkjcyg2jb.streamlit.app/
- Repository: `Kostsrkv/russian-regional-macro-dashboard`, branch `main`, entrypoint `app.py`.
- Release: `data/dashboard/releases/2026-10-05-cloud-v1`, selected by `data/dashboard/current.json`.
- Existing macro/fiscal payloads remain byte-for-byte identical to the 2026-10-02 snapshot.
- Fuel preview adds 21,840 observations: 78 eligible regions, 56 months, five source categories. AI-92, AI-95 and diesel are the default plotted grades; AI-98 and above is optional. The all-petrol aggregate is not mixed into grade charts.
- The 20 release payloads total 1,540,733 bytes. Raw workbooks, archives, private audit outputs and credentials are excluded.
- Fuel history labels adapt to chart width; monthly observations and exact-month comparisons are unchanged.

## Analytical limitations

Publication is not promotion to independently verified data. Prices are spatial-average end-of-month RUB/litre, not a monthly time average, official CPI or daily pump quotes. Source URLs and publication/retrieval dates were not recorded. Raw-cell checks support extraction fidelity, not source authenticity or economic explanations. Large monthly changes remain flagged. Optional missing prices remain missing rather than zero. No causal shortage or sanctions claims are generated.

## Release checks

The loader pins the outer manifest checksum and every payload checksum. The fuel envelope also pins its processed dataset. Invalid checksums or envelopes fail closed. Tests cover existing-data parity, null preservation, grain, exact-calendar changes, filters, exports, responsive date ticks, private-free cloud startup and rollback.

Run the complete test suite from the publication checkout, which contains no private `outputs/` directory:

```bash
python -m pytest
```

GitHub Actions repeats the tests with Python 3.12. After pushing, verify the public app displays research release `2026-10-05`, the fuel page loads for multiple regions, and history labels remain legible at desktop and narrow widths. Deployment completion requires those live checks; a successful local build alone is not sufficient.

## Data-only rollback

Keep the new code and replace `data/dashboard/current.json` with:

```json
{
  "schema_version": 1,
  "release_id": "2026-10-02",
  "release_path": "releases/2026-10-02-cloud-v1",
  "manifest_sha256": "986e4984851b276c2880ad907644a253200a17e927d46c0c068115795e85c1ce"
}
```

Test, commit and push the pointer change. The prior snapshot contains no public fuel page. Do not overwrite immutable release directories or rewrite shared Git history. Code regressions require a normal Git revert as described in `DEPLOYMENT.md`.
