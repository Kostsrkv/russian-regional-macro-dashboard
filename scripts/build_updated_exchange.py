"""Build a fresh local research exchange and execute its portable audit.

Only processed review candidates are exported. Local raw sources are read for
small independent cell checks; their contents are never copied into the bundle.
Existing outputs are immutable. Archive hashes live in an external sidecar so
neither the archive nor its internal manifest hashes itself.
"""
from __future__ import annotations

import argparse
from io import BytesIO, StringIO
import json
from pathlib import Path
import re
import shutil
import sys
import textwrap
from zipfile import ZipFile, ZIP_DEFLATED, ZipInfo

import nbformat
from nbclient import NotebookClient
import numpy as np
import pandas as pd
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from macro_rus.cumulative_production import load_cumulative_candidate
from macro_rus.period_alignment import matched_pit_production
from macro_rus.provenance import sha256_file
from macro_rus.ingest_treasury import _RowsParser, _find_html_member, _parse_decimal
from macro_rus.ingest_fns import _pit_fields, _delimiter
from macro_rus.annual_production import load_annual_candidate, read_annual, select_vintages
from macro_rus.fiscal_view import load_candidate
from macro_rus.social_spending import social_history

BASELINE = "outputs/research_exchange_2026-09-30"
ANNUAL = "outputs/annual_social_candidate_2026-10-02"
CUMULATIVE = "outputs/cumulative_production_candidate_2026-10-02"
FISCAL = "outputs/fiscal_analysis_candidate_2026-10-02"
PILOTS = {"RU-KLU", "RU-SVE"}
PRIVATE_PATTERN = re.compile(r"(?:/Users/|/home/|/private/|/tmp/|[A-Za-z]:(?:\\|/)+(?:Users|Windows|Documents|Temp)\b|file://|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{8,})", re.I)

DEFINITIONS = {
    "source_sha256": "SHA-256 of the complete original source file (workbook, CSV or Treasury archive); source contents are excluded.",
    "source_vintage": "Exact selected publication/release vintage; workbook update dates are not retrieval dates or final-estimate declarations.",
    "amount_basis": "YTD means cumulative January-to-cutoff; period_flow means Q1 = Q1 YTD or Q2 = H1 minus exact same-year Q1.",
    "frequency": "Source reporting interval/basis: monthly, annual, quarterly or YTD; read with the exact index_measure or amount_basis.",
    "index_measure": "Published production comparison basis: same month, whole year or January-to-cutoff window versus the corresponding prior-year period = 100.",
    "change_yoy_rub": "Current actual minus prior-year same-window actual for the same amount_basis, nominal RUB.",
    "flow_status": "How the Q1/Q2 actual flow was derived or why an exact endpoint is missing.",
    "table": "CSV table name without the extension.", "field": "Exact CSV column name.",
    "dtype": "Pandas dtype at package construction; import codes as text where documented.",
    "definition": "Meaning, comparison basis and relevant interpretation constraint.",
    "missing_values": "Count of empty/unavailable cells in this field, never converted to zero.",
    "rows": "Number of records in this table or coverage group.",
    "periods": "Number of distinct observation windows, or source-native period list in basis evidence.",
    "earliest": "Earliest observation period end, not publication or retrieval time.",
    "latest": "Latest observation period end, not publication or retrieval time.",
    "observations": "Number of source observations, including missing source markers.",
    "reported": "Number of numeric source observations.",
    "numeric_observations": "Count of numeric observations, excluding suppressed/missing source cells.",
    "missing": "Number of unavailable observations, not economic zeros.",
    "window_start": "First day of the stated window; production YTD always begins on 1 January.",
    "window_end": "Last day included in a derived fiscal Q1/Q2 flow.",
    "window_months": "Number of months from January to the production reporting cutoff.",
    "window": "Q1, H1, or Q2; read with amount_basis to distinguish YTD from quarter flows.",
    "basis": "YTD or period_flow; period_flow uses Q1 = Q1 YTD and Q2 = H1 minus Q1.",
    "prior_period": "Exactly the same reporting window one calendar year earlier.",
    "source_sheet": "Exact workbook sheet title; never interpreted as a zero-based sheet position.",
    "source_schema_file": "Exact FNS structure CSV used by the audit-time direct cell check; original parse-time schema hash is not established.",
    "source_schema_sha256": "SHA-256 of the FNS schema at this audit's execution time; not asserted to be the original parse-time schema hash.",
    "source_cell": "One-based Excel A1 source cell locator.",
    "source_region_name": "Original source regional label, including territorial qualifiers.",
    "region_match_method": "Explicit alias or typography normalization used to match the canonical key.",
    "raw_value": "Original single source-cell value or unavailable marker; not a raw file.",
    "raw_year_header": "Original workbook year header, including any footnote marker.",
    "raw_window_header": "Original workbook January-to-cutoff header, including footnote markers.",
    "scope_qualifier": "Source territorial-scope qualifier retained without silent stripping.",
    "availability": "Explicit observed/missing/matched state; unavailable values remain empty.",
    "missing_source_value": "True when the selected original source cell is unavailable.",
    "year_header_has_footnote": "Whether the source year-header text contains a footnote marker.",
    "window_header_has_footnote": "Whether the source window-header text contains a footnote marker.",
    "revision_state": "Published source/revision status retained by the candidate, not a final-estimate assertion.",
    "difference_index_points": "Later minus earlier published index, in index points; may include reweighting.",
    "changed": "Whether overlapping vintages differ numerically or in availability.",
    "index_difference_from_selected": "This vintage minus the selected index, in points; not economic growth.",
    "is_selected_vintage": "Whether this original row is from the selected later publication vintage.",
    "comparison_status": "Unchanged/revised/missing comparison state across source vintages.",
    "title": "Original workbook sheet title.", "comparison_basis": "Original source text defining the index denominator.",
    "window_headers": "JSON list of original source window headers, including footnotes.",
    "year_headers": "JSON list of original source year headers, including footnotes.",
    "footnotes": "JSON list of the source sheet footnotes; preserve alongside index_base.",
    "raw_region_label": "Original regional label read during the independent source-cell check.",
    "raw_index_value": "Index read directly from the original workbook cell for the independent check.",
    "candidate_index_value": "Processed candidate index checked against the original cell.",
    "passed": "Whether the named numerical or evidence check passed its documented tolerance.",
    "check": "Accounting or data-quality rule name.", "gap_rub": "Signed residual in nominal RUB; .02 RUB tolerance for accounting.",
    "blocking": "Whether failure prevents use of the review candidate.",
    "function": "English name of the top-level spending function; social policy is not all welfare.",
    "function_name_en": "English label of one of the 14 disjoint top-level budget spending functions.",
    "share_pct": "Function actual divided by total expenditure for the same region/window, times 100.",
    "share_change_pp": "Current function share minus prior same-window share, in percentage points.",
    "total_expenditure_rub": "Complete published expenditure total used as the social-share denominator, nominal RUB.",
    "prior_year_actual_rub": "Observed expenditure actual for exactly the same window one year earlier, nominal RUB.",
    "prior_year_share_pct": "Function actual divided by total expenditure in the prior same-window year, times 100.",
    "total_spending_rub": "Complete expenditure denominator for this exact basis/window, nominal RUB.",
    "total_spending_change_rub": "Current minus prior same-window total expenditure, nominal RUB.",
    "amount_rub": "Nominal actual for this amount_basis; YTD or derived Q1/Q2 flow.",
    "contribution_to_spending_change_rub": "Signed current-minus-prior function amount; all 14 sum to total change.",
    "contribution_to_spending_growth_pp": "Function amount change divided by prior total expenditure, times 100.",
    "functions_complete": "All 14 top-level expenditure amounts are observed in this window.",
    "comparison_complete": "All 14 function amounts and both total denominators are observed in current and prior-year same-window observations.",
    "functions": "Number of top-level budget functions in this reconciliation group; expected 14.",
    "share_sum_pct": "Sum of all 14 disjoint function shares; expected 100 with a positive total.",
    "spending_reconciliation_gap_rub": "All 14 function amounts minus total expenditure, nominal RUB.",
    "change_reconciliation_gap_rub": "Sum of all 14 function changes minus total expenditure change, nominal RUB.",
    "contribution_sum_rub": "Sum of 14 signed function contributions, nominal RUB.",
    "contribution_growth_sum_pp": "Sum of 14 contributions to expenditure growth, percentage points.",
    "step": "Ordered bridge step: prior balance, revenue change, minus expenditure change, current balance.",
    "label": "Bridge step display label.", "role": "Anchor or signed change; anchor values are not added as drivers.",
    "value_rub": "Signed bridge anchor/driver value, nominal RUB.",
    "from_rub": "Beginning level of a bridge step, nominal RUB.", "to_rub": "Ending level of a bridge step, nominal RUB.",
    "start_balance_rub": "Prior same-window budget balance; surplus positive and deficit negative.",
    "end_balance_rub": "Current same-window budget balance; surplus positive and deficit negative.",
    "balance_change_rub": "Current minus prior same-window budget balance, nominal RUB.",
    "revenue_change_rub": "Current minus prior same-window revenue total, nominal RUB.",
    "expenditure_change_rub": "Current minus prior same-window expenditure; bridge driver has the opposite sign.",
    "reconciliation_gap_rub": "Balance change minus revenue change plus expenditure change, nominal RUB.",
    "reason": "Explicit explanation of an unavailable calculation; no invented prior window.",
    "total_revenue_rub": "Published total revenue for this exact basis/window, nominal RUB.",
    "transfers_rub": "Specific code 202 transfers from other budgets; does not identify federal origin.",
    "own_source_rub": "Code 100 tax/non-tax revenue; not total revenue minus code 202.",
    "transfer_share_pct": "Code 202 transfers divided by total revenue in the same window, times 100.",
    "transfer_share_change_pp": "Current minus prior same-window transfer share, percentage points.",
    "industry": "English B–E section label for the same-window PIT/production comparison.",
    "pit_current_rub": "Reported nominal industry PIT YTD for the current January-to-cutoff window.",
    "pit_prior_rub": "Reported nominal industry PIT for the same January-to-cutoff window one year earlier.",
    "pit_change_rub": "Current reported PIT minus observed prior same-window PIT, nominal RUB.",
    "pit_yoy_pct": "100*(current PIT/prior same-window PIT-1), withheld for absent/nonpositive prior.",
    "production_yoy_pct": "Source-published January-to-cutoff production index minus 100; not a monthly average.",
    "units_pit": "PIT monetary unit: nominal RUB.",
    "units_production": "Index basis: previous-year same period = 100.",
    "RUB bn": "Nominal actual expenditure divided by 1e9; presentation-only scale.",
    "Year": "Calendar year of the observation window, not source publication year.",
    "locator": "Exact original cell locator or zero-based parsed HTML row/column or named CSV field.",
    "locator_type": "Excel A1 cell, parsed HTML cell, or source CSV named field.",
    "metric": "Metric/section checked against a direct source cell.",
    "raw_numeric_value": "Numerical value directly read from an original local source cell.",
    "scale_to_output": "Unit multiplier: FNS thousand RUB to RUB = 1000, otherwise 1.",
    "output_value": "Value in the processed CSV compared with the independent source cell.",
    "hash_verified": "Original local source file SHA-256 was checked against the retained source hash.",
    "missing_value_count": "Number of unavailable metric values at this table/region grain.",
    "latest_metric_missing": "Whether the latest region-window has unavailable metric cells.",
    "grain": "Documented unique key; duplicate selected keys cause the build to fail.",
    "file": "Portable package-relative filename.",
    "source_group": "Origin candidate group; each candidate manifest hash is retained.",
    "retrieval_date": "Original source retrieval date when documented; unknown remains empty.",
}


def read_csv(path):
    return pd.read_csv(path, dtype={"region_id": str, "fns_code": str, "metric_code": str,
        "revenue_code": str, "source_line_code": str, "source_sheet": str}, low_memory=False)


def definition(field, old, table=""):
    if field == "growth_yoy_pct":
        if table.startswith(("industrial_production_annual", "industrial_production_cumulative", "annual_", "cumulative_")):
            return "Published annual or January-to-cutoff production index minus 100, percent; not an average of monthly growth rates."
        return "100*(current actual/prior-year same-window actual-1), only with an observed positive prior denominator; nominal amounts."
    if field in DEFINITIONS:
        return DEFINITIONS[field]
    if field in old:
        return old[field]
    if field.endswith("_source_evidence") or field == "source_evidence":
        return "JSON source endpoint evidence: original file basename/hash, archive member, cutoff and row/column; Q2 retains both Q1 and H1 endpoints."
    for suffix in ("_earlier", "_later", "_fiscal", "_revenue"):
        if field.endswith(suffix):
            return definition(field[:-len(suffix)], old, table) + " " + suffix[1:].capitalize() + " observation in the comparison."
    for prefix in ("pit_current_", "pit_prior_", "production_", "selected_", "start_", "end_", "prior_"):
        if field.startswith(prefix):
            return definition(field[len(prefix):], old, table) + " " + prefix[:-1].replace("_", " ").capitalize() + " endpoint."
    for stem in ("transfers", "own_source", "total_revenue"):
        if field == stem + "_change_yoy_rub":
            return "Current minus prior same-window " + definition(stem + "_rub", old, table)
        if field == stem + "_growth_yoy_pct":
            return "100*(current/prior same-window-1), only when prior is positive; measure: " + definition(stem + "_rub", old, table)
    raise ValueError(f"Missing precise field definition: {field}")


def privacy_check(path):
    if path.suffix.lower() not in {".csv", ".json", ".ipynb", ".html", ".md", ".py"}:
        raise ValueError(f"Unexpected portable file type: {path.name}")
    if PRIVATE_PATTERN.search(path.read_text(encoding="utf-8-sig")):
        raise ValueError(f"Machine path or credential-like text in portable artifact: {path.name}")


def verify_recorded_hashes(directory, hashes):
    for filename, expected in hashes.items():
        if Path(filename).name != filename or sha256_file(directory / filename) != expected:
            raise ValueError(f"Candidate recorded file hash differs: {directory.name}/{filename}")


def compare_processed(expected, observed, keys, name):
    # Round-trip with the same CSV parser to normalize dates, codes, markers and
    # dtype inference before comparing all cells; no intermediate file is saved.
    normalized = read_csv(StringIO(expected.to_csv(index=False)))
    if set(normalized.columns) != set(observed.columns):
        raise ValueError(f"Candidate column schema differs: {name}")
    try:
        pd.testing.assert_frame_equal(normalized.sort_values(keys).reset_index(drop=True),
            observed[normalized.columns].sort_values(keys).reset_index(drop=True),
            check_dtype=False, check_exact=False, rtol=1e-14, atol=1e-6)
    except AssertionError as exc:
        raise ValueError(f"Candidate cells differ from independent reproduction: {name}") from exc


def verify_annual_exports(root, tables, manifest):
    selected, _ = load_annual_candidate(root / ANNUAL)
    grain = ["region_id","period","okved_section"]
    compare_processed(selected, tables["industrial_production_annual"], grain, "annual CSV/parquet")
    records = tables["regions"].to_dict("records")
    original = []
    for source in manifest["sources"]:
        path = root / source["source_file"]
        if sha256_file(path) != source["sha256"]:
            raise ValueError("Annual original source hash differs")
        frame, _ = read_annual(path, records)
        original.append(frame)
    fresh = select_vintages(original)
    compare_processed(fresh, tables["industrial_production_annual"], grain, "annual source reproduction")
    overlap = original[0].merge(original[1], on=grain, suffixes=("_earlier","_later"), validate="one_to_one")
    overlap["difference_index_points"] = overlap.index_value_later-overlap.index_value_earlier
    overlap["changed"] = overlap.difference_index_points.abs().gt(1e-9) | overlap.index_value_later.isna().ne(overlap.index_value_earlier.isna())
    compare_processed(overlap, tables["annual_vintage_comparison"], grain, "annual vintage comparison")
    compare_processed(fresh.loc[fresh.index_value.isna()], tables["annual_missing_observations"], grain, "annual missing register")
    coverage = fresh.groupby(["region_id","okved_section"]).agg(observations=("period","size"),
        numeric_observations=("index_value","count"), earliest=("period","min"),latest=("period","max")).reset_index()
    compare_processed(coverage, tables["annual_coverage"], ["region_id","okved_section"], "annual coverage")
    fiscal_root = root / manifest["fiscal_candidate"]
    if sha256_file(fiscal_root / "manifest.json") != manifest["fiscal_manifest_sha256"]:
        raise ValueError("Annual social fiscal source manifest differs")
    fiscal, _ = load_candidate(fiscal_root)
    social = pd.concat([social_history(fiscal, record["region_id"], date) for record in records
        for date in [pd.Timestamp("2026-03-31"),pd.Timestamp("2026-06-30")]], ignore_index=True)
    compare_processed(social, tables["social_spending"], ["region_id","period","metric_code"], "social spending")


def direct_source_checks(root, tables):
    checks, workbooks, archive_rows = [], {}, {}

    def source_path(name, digest):
        path = root / name
        if not path.is_file() or sha256_file(path) != digest:
            raise ValueError(f"Original source missing or hash differs: {name}")
        return path

    def add(table, row, raw, locator, kind, scale=1):
        output = row.index_value if "index_value" in row else row.actual_ytd_rub if "actual_ytd_rub" in row else row.pit_ytd_rub
        value = float(raw) * scale
        if not np.isclose(value, float(output), atol=.02 if scale == 1000 or kind == "parsed_html_cell" else 1e-9, rtol=0):
            raise ValueError(f"Direct source mismatch: {table} {locator}")
        checks.append(dict(table=table, region_id=row.region_id, period=str(row.period)[:10],
            metric=str(row.get("okved_section", row.get("metric_code", ""))), source_file=row.source_file,
            source_sha256=row.source_sha256, source_member=row.get("source_member"),
            source_sheet=row.get("source_sheet"), source_schema_file=row.get("source_schema_file"),
            source_schema_sha256=row.get("source_schema_sha256"), locator=locator, locator_type=kind,
            raw_numeric_value=float(raw), scale_to_output=scale, output_value=float(output),
            hash_verified=True, passed=True))

    for table in ("industrial_production_annual", "industrial_production_cumulative"):
        data = tables[table]
        sample = data.loc[data.region_id.isin(PILOTS) & data.index_value.notna()
                          & data.period.astype(str).str[:10].isin(
                              ["2015-12-31", "2025-12-31"] if table.endswith("annual") else ["2025-03-31", "2026-03-31"])]
        for _, row in sample.iterrows():
            key = row.source_file
            if key not in workbooks:
                workbooks[key] = load_workbook(source_path(key, row.source_sha256), read_only=True, data_only=True)
            raw = workbooks[key][str(row.source_sheet)][row.source_cell].value
            add(table, row, raw, f"{row.source_sheet}!{row.source_cell}", "excel_a1_cell")
    for workbook in workbooks.values():
        workbook.close()

    fiscal = tables["fiscal_observations"]
    sample = fiscal.loc[fiscal.region_id.isin(PILOTS) & fiscal.period.astype(str).str.startswith(("2025", "2026"))
        & fiscal.metric_code.isin(["revenue_total", "expenditure_total", "budget_balance", "0700", "0900", "1000"])]
    for _, row in sample.iterrows():
        key = (row.source_file, row.source_member)
        if key not in archive_rows:
            with ZipFile(source_path(row.source_file, row.source_sha256)) as outer:
                payload = outer.read(_find_html_member(outer))
            with ZipFile(BytesIO(payload)) as inner:
                parser = _RowsParser()
                parser.feed(inner.read(row.source_member).decode("cp1251"))
                archive_rows[key] = parser.rows
        ordinal, col = int(row.source_row_index), int(row.actual_column_index)
        raw = _parse_decimal(archive_rows[key][ordinal][col], field="direct source check")
        add("fiscal_observations", row, raw, f"row={ordinal};column={col}", "parsed_html_cell")

    pit, sources = tables["pit_receipts"], tables["sources"].set_index("source_id")
    regions = tables["regions"].set_index("region_id")
    for sid, selected in pit.loc[pit.region_id.isin(PILOTS) & pit.okved_section.eq("C")
                                 & pit.period.astype(str).str.endswith("03-31")].groupby("source_id"):
        source = sources.loc[sid]
        path = source_path(source.local_path, source.sha256)
        schema_match = re.search(r"structure-(\d{8})", path.name)
        schema_path = root / f"structure-{schema_match.group(1)}.csv"
        schema = read_csv(schema_path)
        sections, _ = _pit_fields(schema)
        raw = pd.read_csv(path, sep=_delimiter(path), dtype=str, encoding="utf-8-sig", low_memory=False)
        for _, row in selected.iterrows():
            code = str(regions.loc[row.region_id].fns_code).lstrip("0")
            mask = raw.GA.str.strip().str.lstrip("0").eq(code)
            if mask.sum() != 1:
                raise ValueError("PIT source row is not unique")
            position = raw.index[mask][0]
            evidence = row.copy()
            evidence["source_file"], evidence["source_sha256"] = path.name, source.sha256
            evidence["source_schema_file"], evidence["source_schema_sha256"] = schema_path.name, sha256_file(schema_path)
            add("pit_receipts", evidence, raw.loc[position, sections["C"]],
                f"csv_row={position + 2};field={sections['C']}", "csv_named_field", 1000)
    return pd.DataFrame(checks)


def notebook(output, table_grains):
    cells = []
    def md(value):
        cells.append(nbformat.v4.new_markdown_cell(textwrap.dedent(value).strip()))
    def code(value):
        cells.append(nbformat.v4.new_code_cell(textwrap.dedent(value).strip()))
    md("""# Regional economy: combined source and metric audit

    ## tl;dr
    This local review package preserves the 78 eligible canonical regional keys.
    It adds published annual and January-to-cutoff production, same-window nominal
    PIT comparisons, and fiscal YTD/Q1/Q2 accounting. Missing source markers and
    absent prior windows are retained. Nothing is promoted or shared externally.
    The checks below execute against the exported CSVs, not hidden application state.

    ## Context & Methods
    ### Key Assumptions
    PIT and budgets are nominal RUB. Production indices keep their source denominator.
    January–March is Q1; January–June is H1. A standalone Q2 production index is not
    constructed. Fiscal Q2 is same-year H1 minus Q1 with both endpoints retained.
    Consolidated regional plus municipal budgets exclude federal budgets and
    territorial health-insurance funds. No national aggregation is authorized.

    Re-run with Python, pandas, numpy, nbformat, nbclient, nbconvert and ipykernel.
    Set PACKAGE_ROOT to the unpacked package directory via the first cell, or launch
    from that directory. The executable script accepts a package path as argv[1].
    Original raw workbooks/archives are excluded. Small independently checked cells,
    their locators and original-file hashes are in source_cell_spotchecks.csv.
    """)
    code("""
    from pathlib import Path
    import hashlib, json, sys
    import numpy as np
    import pandas as pd
    from IPython.display import display
    pd.set_option('display.max_rows', 12)
    pd.set_option('display.max_columns', 12)
    pd.set_option('display.width', 120)
    def discover_package():
        choices = [Path(sys.argv[1])] if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else []
        choices += [Path.cwd(), Path.cwd() / 'research_exchange_2026-10-02',
                    Path.cwd() / 'outputs' / 'research_exchange_2026-10-02']
        for candidate in choices:
            if (candidate / 'input_checksums.json').is_file():
                return candidate
        raise FileNotFoundError('Launch from the unpacked package, or supply its directory as argv[1].')
    PACKAGE_ROOT = discover_package()
    def load(name):
        return pd.read_csv(PACKAGE_ROOT / (name + '.csv'), dtype={'region_id':str,
            'fns_code':str, 'metric_code':str, 'revenue_code':str, 'source_sheet':str}, low_memory=False)
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    checksums = json.loads((PACKAGE_ROOT / 'input_checksums.json').read_text())
    assert all(digest(PACKAGE_ROOT / name) == value for name, value in checksums.items())
    print('PASS: processed input checksums; no remote reads or credentials required')
    """)
    md("## Data\n### 1. Canonical regional scope, grain and latest missingness\nThe two historical pilot IDs coexist with FNS project IDs; regions.csv is the canonical key set. Code columns retain leading zeros. Coverage is per region, so no sum across geographic scopes is implied.")
    code(f"""
    regions = load('regions')
    canonical = set(regions.region_id)
    assert len(canonical) == 78 and not regions.region_id.duplicated().any()
    assert not regions.eligible_for_national_aggregation.any()
    grains = {table_grains!r}
    summary = []
    for name, keys in grains.items():
        data = load(name)
        assert not data.duplicated(keys).any(), name
        assert set(data.region_id).issubset(canonical), name
        summary.append(dict(table=name, rows=len(data), regions=data.region_id.nunique(),
                            latest=data.period.max() if 'period' in data else None))
    display(pd.DataFrame(summary))
    coverage = load('coverage_registry')
    display(coverage.groupby('table').agg(regions=('region_id','nunique'),
        missing_cells=('missing_value_count','sum'), latest_missing_regions=('latest_metric_missing','sum')))
    print('PASS: exported region scope and unique selected grains')
    """)
    md("### 2. Field dictionary and retained source evidence\nThe complete field_dictionary.csv covers every column in every included CSV, including audit registers. The earlier data_dictionary.csv is preserved as a baseline artifact. Workbook footnotes and archive endpoint evidence remain inspectable.")
    code("""
    dictionary = load('field_dictionary')
    documented = set(zip(dictionary.table, dictionary.field))
    actual = {(path.stem, field) for path in PACKAGE_ROOT.glob('*.csv') for field in pd.read_csv(path, nrows=0).columns}
    assert documented == actual
    assert dictionary.definition.notna().all()
    annual = load('industrial_production_annual')
    cumulative = load('industrial_production_cumulative')
    for data in [annual, cumulative]:
        assert data[['source_file','source_sha256','source_sheet','source_cell']].notna().all().all()
        assert np.allclose(data.growth_yoy_pct, data.index_value - 100, equal_nan=True)
    display(load('source_register')[['source_file','source_vintage','source_sha256','retrieval_date']].drop_duplicates().head(12))
    display(annual.loc[annual.index_value.isna(), ['region_id','period','okved_section','raw_value','source_cell']].head(12))
    print('PASS: complete field definitions, production cell lineage and preserved missing markers')
    """)
    md("### 3. Independent pilot source cells\nAt build time the original local files were hashed and these cells were read directly: annual/YTD production, industry PIT and Treasury actuals in Kaluga and Sverdlovsk. This is a sample of source extraction, not a claim that every original cell was independently re-read.")
    code("""
    cells = load('source_cell_spotchecks')
    assert cells.passed.all() and cells.hash_verified.all()
    assert np.allclose(cells.raw_numeric_value * cells.scale_to_output, cells.output_value, atol=.02, rtol=0)
    sampled_tables = {name:load(name) for name in cells.table.unique()}
    source_by_id = load('sources').set_index('source_id')
    for row in cells.itertuples(index=False):
        data = sampled_tables[row.table]
        metric_field = 'metric_code' if row.table == 'fiscal_observations' else 'okved_section'
        value_field = 'index_value' if row.table.startswith('industrial_production') else 'actual_ytd_rub' if row.table == 'fiscal_observations' else 'pit_ytd_rub'
        observation = data.loc[data.region_id.eq(row.region_id) & data.period.eq(row.period) & data[metric_field].eq(row.metric)]
        assert len(observation) == 1
        observation = observation.iloc[0]
        assert np.isclose(row.output_value,observation[value_field],atol=.02,rtol=0)
        if row.table == 'pit_receipts':
            source = source_by_id.loc[observation.source_id]
            assert row.source_sha256 == source.sha256 and row.source_file == source.local_path
            assert pd.notna(row.source_schema_sha256)
        else:
            assert row.source_sha256 == observation.source_sha256 and row.source_file == observation.source_file
            if row.table == 'fiscal_observations':
                assert row.source_member == observation.source_member
            else:
                assert str(row.source_sheet) == str(observation.source_sheet)
    display(cells.groupby('table').agg(checked_cells=('passed','size'), passed=('passed','all')))
    display(cells.loc[cells.table.eq('industrial_production_cumulative'),
        ['region_id','period','metric','source_sheet','locator','raw_numeric_value']].head(12))
    print('PASS: direct raw source cell samples and unit conversion')
    """)
    md("## Results\n### 4. Same-window PIT and production hand calculations\nPIT YoY uses observed prior January-to-cutoff PIT and a positive denominator. Production growth is the published cumulative index minus 100. Missing mining and unavailable prior PIT are not filled. The measures are descriptive and do not establish causality.")
    code("""
    pit = load('pit_receipts')
    matched = load('matched_pit_production')
    assert len(matched) == 78 * 3 * 4
    indexed_pit = pit.set_index(['region_id','period','okved_section'])
    indexed_production = cumulative.set_index(['region_id','period','okved_section'])
    for row in matched.itertuples(index=False):
        key = (row.region_id, row.period, row.okved_section)
        prior_key = (row.region_id, row.prior_period, row.okved_section)
        current = indexed_pit.loc[key].pit_ytd_rub if key in indexed_pit.index else np.nan
        prior = indexed_pit.loc[prior_key].pit_ytd_rub if prior_key in indexed_pit.index else np.nan
        index = indexed_production.loc[key].index_value if key in indexed_production.index else np.nan
        growth = (current - prior) / prior * 100 if pd.notna(current) and pd.notna(prior) and prior > 0 else np.nan
        assert np.isclose(row.pit_current_rub, current, equal_nan=True)
        assert np.isclose(row.pit_prior_rub, prior, equal_nan=True)
        assert np.isclose(row.pit_yoy_pct, growth, equal_nan=True)
        assert np.isclose(row.production_yoy_pct, index - 100, equal_nan=True)
    sample = matched.loc[matched.region_id.isin(['RU-KLU','RU-SVE']) & matched.period.eq('2026-03-31')]
    display(sample[['region_id','okved_section','pit_current_rub','pit_prior_rub','pit_yoy_pct','index_value','production_yoy_pct','availability']])
    assert matched.loc[matched.period.eq('2026-03-31') & matched.okved_section.eq('B'),'pit_current_rub'].isna().all()
    print('PASS: all 936 same-window comparisons independently recomputed from exported source values')
    """)
    md("### 5. Annual and cumulative revision comparison\nLater source vintages win, including missing cells. Index differences between publications may reflect reweighting and revisions; they are not economic changes between publication dates. The later headline uses explicit 2023 GVA weights; no constant-method output-level chain is constructed.")
    code("""
    revisions = load('annual_vintage_comparison')
    assert np.allclose(revisions.difference_index_points, revisions.index_value_later - revisions.index_value_earlier, equal_nan=True)
    overlap = load('cumulative_overlap_audit')
    assert np.allclose(overlap.index_difference_from_selected, overlap.index_value - overlap.selected_index_value, equal_nan=True)
    assert annual.loc[annual.period.ge('2020-01-01'),'source_vintage'].eq('2026-08-26').all()
    assert cumulative.loc[cumulative.period.ge('2020-01-01'),'source_vintage'].eq('2026-08-26').all()
    display(revisions.groupby('okved_section').agg(overlap=('changed','size'), changed=('changed','sum')))
    display(overlap.groupby('comparison_status').size().rename('source_rows').to_frame())
    print('PASS: retained original vintages, missingness and revision arithmetic')
    """)
    md("### 6. Fiscal Q1/Q2 flows\nQ1 equals Q1 YTD. Q2 is H1 minus same-year Q1, using exact region/kind/code/perimeter. Negative adjustments remain signed. The retained start/end evidence records both source archives and cells.")
    code("""
    fiscal = load('fiscal_observations')
    flows = load('fiscal_flows')
    keys = ['region_id','period','budget_level','kind','metric_code']
    raw_fiscal = fiscal.set_index(keys)
    for row in flows.itertuples(index=False):
        end = pd.Timestamp(row.period)
        current_key = (row.region_id, row.period, row.budget_level, row.kind, row.metric_code)
        current = raw_fiscal.loc[current_key].actual_ytd_rub
        if end.month == 3:
            expected = current
        else:
            start_key = (row.region_id, f'{end.year}-03-31', row.budget_level, row.kind, row.metric_code)
            expected = current - raw_fiscal.loc[start_key].actual_ytd_rub if start_key in raw_fiscal.index else np.nan
        assert np.isclose(row.quarter_flow_rub, expected, atol=.02, rtol=0, equal_nan=True)
        evidence = json.loads(row.flow_source_evidence)
        assert evidence['end']['source_sha256']
        endpoint = raw_fiscal.loc[current_key]
        for field in ['source_file','source_sha256','source_member','reporting_cutoff','source_row_index','actual_column_index']:
            assert evidence['end'][field] == endpoint[field]
        if end.month == 6:
            assert evidence['start'].get('source_sha256') or (pd.isna(row.quarter_flow_rub) and row.flow_status == 'missing endpoint')
            if start_key in raw_fiscal.index:
                endpoint = raw_fiscal.loc[start_key]
                for field in ['source_file','source_sha256','source_member','reporting_cutoff','source_row_index','actual_column_index']:
                    assert evidence['start'][field] == endpoint[field]
        else:
            assert evidence['start'] is None
    display(flows.loc[flows.region_id.isin(['RU-KLU','RU-SVE']) & flows.period.eq('2026-06-30')
        & flows.metric_code.isin(['revenue_total','expenditure_total','budget_balance']),
        ['region_id','metric_code','start_actual_ytd_rub','actual_ytd_rub','quarter_flow_rub']])
    print('PASS: every exported Q1/Q2 flow and endpoint lineage')
    """)
    md("### 7. All-function reconciliation, share changes and contributions\nThe 14 disjoint parent functions sum to total spending. Current and prior comparisons use the same window and basis. Function changes reconcile to the total expenditure change; contributions to growth use prior total expenditure, not individual prior function expenditure.")
    code("""
    spending = load('spending_function_history')
    flow_idx = flows.set_index(keys)
    def fiscal_amount(region, date, kind, metric, basis):
        key = (region,date,'consolidated_regional_budget',kind,metric)
        source = raw_fiscal if basis == 'YTD' else flow_idx
        if key not in source.index:
            return np.nan
        return source.loc[key].actual_ytd_rub if basis == 'YTD' else source.loc[key].quarter_flow_rub
    for row in spending.itertuples(index=False):
        actual = fiscal_amount(row.region_id,row.period,'expenditure_function',row.metric_code,row.amount_basis)
        prior = fiscal_amount(row.region_id,row.prior_period,'expenditure_function',row.metric_code,row.amount_basis)
        total = fiscal_amount(row.region_id,row.period,'summary','expenditure_total',row.amount_basis)
        prior_total = fiscal_amount(row.region_id,row.prior_period,'summary','expenditure_total',row.amount_basis)
        for exported, expected in [(row.amount_rub,actual),(row.prior_amount_rub,prior),
                                   (row.total_spending_rub,total),(row.prior_total_spending_rub,prior_total)]:
            assert np.isclose(exported,expected,atol=.02,rtol=0,equal_nan=True)
    group_keys = ['region_id','period','budget_level','amount_basis','window']
    grouped = spending.groupby(group_keys)
    assert grouped.size().eq(14).all()
    totals = grouped.agg(sum_functions=('amount_rub',lambda s:s.sum(min_count=14)),
        total=('total_spending_rub','first'), sum_changes=('change_yoy_rub',lambda s:s.sum(min_count=14)),
        total_change=('total_spending_change_rub','first'), share_sum=('share_pct',lambda s:s.sum(min_count=14)))
    assert np.allclose(totals.sum_functions, totals.total, atol=.02, rtol=0, equal_nan=True)
    assert np.allclose(totals.sum_changes, totals.total_change, atol=.02, rtol=0, equal_nan=True)
    assert np.allclose(totals.share_sum, 100, atol=1e-8)
    assert np.allclose(spending.share_pct,(spending.amount_rub/spending.total_spending_rub*100).where(spending.total_spending_rub.gt(0)),equal_nan=True)
    assert np.allclose(spending.prior_share_pct,(spending.prior_amount_rub/spending.prior_total_spending_rub*100).where(spending.prior_total_spending_rub.gt(0)),equal_nan=True)
    assert np.allclose(spending.change_yoy_rub,spending.amount_rub-spending.prior_amount_rub,equal_nan=True)
    assert np.allclose(spending.contribution_to_spending_change_rub,spending.change_yoy_rub,equal_nan=True)
    assert np.allclose(spending.growth_yoy_pct,(spending.change_yoy_rub/spending.prior_amount_rub*100).where(spending.prior_amount_rub.gt(0)),equal_nan=True)
    assert np.allclose(spending.share_change_pp, spending.share_pct-spending.prior_share_pct, equal_nan=True)
    assert np.allclose(spending.contribution_to_spending_growth_pp,
        (spending.change_yoy_rub/spending.prior_total_spending_rub*100).where(spending.prior_total_spending_rub.gt(0)), equal_nan=True)
    display(spending.loc[spending.region_id.eq('RU-SVE') & spending.period.eq('2026-06-30')
        & spending.amount_basis.eq('period_flow'), ['metric_code','function_name_en','amount_rub','share_pct','share_change_pp','contribution_to_spending_change_rub']])
    print('PASS: 14-function shares and all same-window change/contribution denominators')
    """)
    md("### 8. Budget balance bridge\nPositive balance means surplus. Prior balance plus the revenue change minus the expenditure change equals current balance. The bridge explains accounting contributions only. 2023 prior windows remain unavailable.")
    code("""
    bridges = load('budget_balance_bridges')
    assert bridges.groupby(group_keys).size().eq(4).all()
    endpoints = bridges.loc[bridges.step.eq(0)]
    for row in endpoints.itertuples(index=False):
        current = {metric:fiscal_amount(row.region_id,row.period,'summary',metric,row.amount_basis)
            for metric in ['revenue_total','expenditure_total','budget_balance']}
        prior = {metric:fiscal_amount(row.region_id,row.prior_period,'summary',metric,row.amount_basis)
            for metric in current}
        assert np.isclose(row.start_balance_rub,prior['budget_balance'],atol=.02,rtol=0)
        assert np.isclose(row.end_balance_rub,current['budget_balance'],atol=.02,rtol=0)
        assert np.isclose(row.revenue_change_rub,current['revenue_total']-prior['revenue_total'],atol=.02,rtol=0)
        assert np.isclose(row.expenditure_change_rub,current['expenditure_total']-prior['expenditure_total'],atol=.02,rtol=0)
    assert np.allclose(endpoints.start_balance_rub + endpoints.revenue_change_rub - endpoints.expenditure_change_rub,
        endpoints.end_balance_rub, atol=.02, rtol=0)
    assert np.allclose(bridges.loc[bridges.step.eq(2),'value_rub'], -bridges.loc[bridges.step.eq(2),'expenditure_change_rub'])
    assert bridges.reconciliation_gap_rub.abs().le(.02).all()
    display(bridges.loc[bridges.region_id.eq('RU-SVE') & bridges.period.eq('2026-06-30')
        & bridges.amount_basis.eq('period_flow'), ['step','label','value_rub','from_rub','to_rub']])
    display(load('balance_bridge_unavailable').groupby('amount_basis').size().rename('unavailable_windows').to_frame())
    print('PASS: complete accounting bridges and explicit unavailable prior windows')
    """)
    md("### 9. Transfer history and denominators\nCode 202 is specific transfers from other budgets and is nested in code 200 non-repayable receipts. Own-source revenue is code 100 tax/non-tax receipts. The three codes are never added together; code 202 does not establish federal origin.")
    code("""
    transfers = load('transfer_history')
    revenue = load('budget_revenue')
    revenue_idx = revenue.set_index(['region_id','period','revenue_code'])
    codes = {'transfers':'20200000000000000', 'own_source':'10000000000000000', 'total_revenue':'TOTAL_REVENUE'}
    for row in transfers.itertuples(index=False):
        for stem, code in codes.items():
            raw = revenue_idx.loc[(row.region_id,row.period,code)]
            expected = raw.actual_ytd_rub if row.amount_basis == 'YTD' else raw.quarter_flow_rub
            assert np.isclose(getattr(row,stem+'_rub'), expected, atol=.02, rtol=0)
    expected_share = (transfers.transfers_rub / transfers.total_revenue_rub*100).where(transfers.total_revenue_rub.gt(0))
    assert np.allclose(transfers.transfer_share_pct, expected_share, equal_nan=True)
    assert np.allclose(transfers.transfer_share_change_pp, transfers.transfer_share_pct-transfers.prior_transfer_share_pct, equal_nan=True)
    for stem in codes:
        change = transfers[stem+'_rub'] - transfers['prior_'+stem+'_rub']
        growth = (change / transfers['prior_'+stem+'_rub'] * 100).where(transfers['prior_'+stem+'_rub'].gt(0))
        assert np.allclose(transfers[stem+'_change_yoy_rub'], change, equal_nan=True)
        assert np.allclose(transfers[stem+'_growth_yoy_pct'], growth, equal_nan=True)
    display(transfers.loc[transfers.region_id.isin(['RU-KLU','RU-SVE']) & transfers.period.eq('2026-06-30'),
        ['region_id','amount_basis','transfers_rub','total_revenue_rub','transfer_share_pct','own_source_rub','own_source_growth_yoy_pct']])
    print('PASS: transfer/own-source code membership, same-window changes and denominators')
    """)
    md("""## Takeaways
    The processed sources and exported metrics pass the independent checks shown above.
    This is a candidate for review, not promotion or external exchange. PIT coverage changes
    and missing mining cells preclude an unqualified industrial PIT aggregate. Production
    vintages have different documented weight bases. Treasury snapshot plans do not identify
    original budgets or amendment history. Debt stocks, regional price deflators and wage/fuel
    datasets are absent. Source publication/retrieval dates remain unknown where not established.

    This arithmetic/source audit uses exact-value tables. Dashboard rendering, responsive
    behavior, browser downloads and accessibility require separate visual review in the main app.
    """)
    return nbformat.v4.new_notebook(cells=cells, metadata={"kernelspec": {
        "display_name": "Python 3", "language": "python", "name": "python3"}})


def run(output, root=ROOT, execute=True):
    output, root = Path(output), Path(root)
    archive = output.with_suffix(".zip")
    sidecar = output.with_suffix(".integrity.json")
    if any(path.exists() for path in (output, archive, sidecar)):
        raise FileExistsError("Use a fresh versioned package directory; no overwrite.")
    baseline = root / BASELINE
    manifests = {group: json.loads((root / folder / "manifest.json").read_text())
        for group, folder in {"baseline":BASELINE, "annual":ANNUAL, "cumulative":CUMULATIVE, "fiscal":FISCAL}.items()}
    verify_recorded_hashes(root / BASELINE, manifests["baseline"]["files"])
    verify_recorded_hashes(root / CUMULATIVE, manifests["cumulative"]["artifact_sha256"])
    verify_recorded_hashes(root / FISCAL, manifests["fiscal"]["derived_hashes"])
    cumulative, _ = load_cumulative_candidate(root / CUMULATIVE)
    tables = {path.stem: read_csv(path) for path in sorted(baseline.glob("*.csv"))}
    origins = {name + ".csv": str((baseline / (name + ".csv")).relative_to(root)) for name in tables}
    canonical = set(tables["regions"].region_id)
    additions = {
        "industrial_production_annual": (ANNUAL,"industrial_production_annual.csv"),
        "social_spending": (ANNUAL,"social_spending.csv"),
        "annual_missing_observations": (ANNUAL,"missing_observations.csv"),
        "annual_vintage_comparison": (ANNUAL,"vintage_comparison.csv"),
        "annual_coverage": (ANNUAL,"coverage.csv"),
        "industrial_production_cumulative": (CUMULATIVE,"industrial_production_cumulative.csv"),
        "cumulative_missing_observations": (CUMULATIVE,"missing_observations.csv"),
        "cumulative_vintage_observations": (CUMULATIVE,"vintage_observations.csv"),
        "cumulative_overlap_audit": (CUMULATIVE,"overlap_audit.csv"),
        "cumulative_source_basis_evidence": (CUMULATIVE,"source_basis_evidence.csv"),
        "cumulative_pilot_q1_handchecks": (CUMULATIVE,"pilot_q1_handchecks.csv"),
        "cumulative_coverage": (CUMULATIVE,"coverage.csv"),
    }
    for name in ["fiscal_flows","spending_function_history","transfer_history","budget_balance_bridges",
                 "balance_bridge_unavailable","spending_accounting_checks","fiscal_revenue_parity_checks"]:
        additions[name] = (FISCAL,name + ".csv")
    for name, (folder, filename) in additions.items():
        tables[name] = read_csv(root / folder / filename)
        origins[name + ".csv"] = folder + "/" + filename
    verify_annual_exports(root, tables, manifests["annual"])
    tables["matched_pit_production"] = matched_pit_production(tables["pit_receipts"], cumulative, tables["sources"])
    grains = {
        "pit_receipts": ["region_id","period","okved_section"],
        "industrial_production": ["region_id","period","okved_section","index_measure"],
        "industrial_production_annual": ["region_id","period","okved_section"],
        "industrial_production_cumulative": ["region_id","period","okved_section"],
        "matched_pit_production": ["region_id","period","okved_section"],
        "budget_revenue": ["region_id","period","revenue_code"],
        "fiscal_observations": ["region_id","period","kind","metric_code"],
        "social_spending": ["region_id","period","metric_code"],
        "fiscal_flows": ["region_id","period","kind","metric_code"],
        "spending_function_history": ["region_id","period","amount_basis","metric_code"],
        "transfer_history": ["region_id","period","amount_basis"],
        "budget_balance_bridges": ["region_id","period","amount_basis","step"],
    }
    for name, keys in grains.items():
        frame = tables[name]
        if frame.duplicated(keys).any() or set(frame.region_id) != canonical:
            raise ValueError(f"Canonical population or grain failure: {name}")
    tables["source_cell_spotchecks"] = direct_source_checks(root, tables)
    source_rows = []
    for _, row in tables["sources"].iterrows():
        vintages = [frame.loc[frame.source_id.eq(row.source_id),"source_vintage"].iloc[0]
            for frame in (tables["pit_receipts"],tables["industrial_production"],tables["budget_revenue"])
            if frame.source_id.eq(row.source_id).any()]
        source_rows.append(dict(source_group="baseline", source_id=row.source_id, source_file=row.local_path, source_sha256=row.sha256,
            source_vintage=vintages[0] if vintages else None, source_url=row.source_url,
            retrieval_date=row.retrieved_at_utc, notes=row.notes))
    for group in ("annual","cumulative"):
        for row in manifests[group]["sources"]:
            production = tables["industrial_production_annual" if group == "annual" else "industrial_production_cumulative"]
            sid = production.loc[production.source_file.eq(row["source_file"]),"source_id"].iloc[0]
            source_rows.append(dict(source_group=group, source_id=sid, source_file=row["source_file"], source_sha256=row["sha256"],
                source_vintage=row["source_vintage"], source_url=row["source_url"],
                retrieval_date=row.get("retrieval_date"), notes="Workbook update date is not a retrieval date or a final-estimate declaration."))
    tables["source_register"] = pd.DataFrame(source_rows)
    coverage = []
    metric_columns = {"pit_receipts":["pit_ytd_rub"], "industrial_production":["index_value"],
        "industrial_production_annual":["index_value"], "industrial_production_cumulative":["index_value"],
        "matched_pit_production":["pit_current_rub","pit_prior_rub","index_value"],
        "budget_revenue":["actual_ytd_rub"], "fiscal_observations":["actual_ytd_rub"],
        "social_spending":["actual_ytd_rub"], "fiscal_flows":["quarter_flow_rub"],
        "spending_function_history":["amount_rub"], "transfer_history":["transfers_rub","own_source_rub","total_revenue_rub"],
        "budget_balance_bridges":["value_rub"]}
    for name, keys in grains.items():
        for region, group in tables[name].groupby("region_id"):
            dates = pd.to_datetime(group.period)
            columns = metric_columns[name]
            coverage.append(dict(table=name, region_id=region, rows=len(group), periods=dates.nunique(),
                earliest=dates.min().date().isoformat(), latest=dates.max().date().isoformat(),
                missing_value_count=int(group[columns].isna().sum().sum()),
                latest_metric_missing=bool(group.loc[dates.eq(dates.max()),columns].isna().any().any()),
                grain=", ".join(keys), status="candidate_not_promoted"))
    tables["coverage_registry"] = pd.DataFrame(coverage)
    old_defs = tables["data_dictionary"].drop_duplicates("field").set_index("field").definition.to_dict()
    dictionary = []
    for name, frame in tables.items():
        for field in frame:
            dictionary.append(dict(table=name, field=field, dtype=str(frame[field].dtype),
                definition=definition(field, old_defs, name), missing_values=int(frame[field].isna().sum())))
    for field in ["table","field","dtype","definition","missing_values"]:
        dictionary.append(dict(table="field_dictionary", field=field, dtype="int64" if field == "missing_values" else "object",
            definition=definition(field, old_defs), missing_values=0))
    tables["field_dictionary"] = pd.DataFrame(dictionary)
    output.mkdir(parents=True, exist_ok=False)
    for name, frame in tables.items():
        if name + ".csv" in origins:
            shutil.copyfile(root / origins[name + ".csv"], output / (name + ".csv"))
        else:
            frame.to_csv(output / (name + ".csv"), index=False, encoding="utf-8-sig")
    footnotes = {group: manifests[group]["sources"] for group in ("annual","cumulative")}
    (output / "source_footnotes.json").write_text(json.dumps(footnotes, ensure_ascii=False, indent=2) + "\n")
    for group in ("annual","cumulative","fiscal"):
        (output / (group + "_candidate_manifest.json")).write_text(json.dumps(manifests[group], ensure_ascii=False, indent=2) + "\n")
    input_checksums = {path.name:sha256_file(path) for path in sorted(output.iterdir())}
    (output / "input_checksums.json").write_text(json.dumps(input_checksums, indent=2) + "\n")
    nb = notebook(output, grains)
    nbformat.validate(nb)
    if execute:
        NotebookClient(nb, timeout=180, kernel_name="python3", resources={"metadata":{"path":str(output)}}).execute()
        if any(out.output_type == "error" for cell in nb.cells if cell.cell_type == "code" for out in cell.outputs):
            raise ValueError("Notebook execution error")
    nbformat.write(nb, output / "combined_source_metric_audit.ipynb")
    from nbconvert import HTMLExporter  # Only the local notebook-export path needs this optional dependency.
    html, _ = HTMLExporter().from_notebook_node(nb)
    (output / "combined_source_metric_audit.html").write_text(html)
    script = "\n\n".join(cell.source for cell in nb.cells if cell.cell_type == "code") + "\n"
    (output / "rerun_audit.py").write_text(script)
    notes = f"""# Regional economy research exchange — 2 October 2026

Status: candidate_not_promoted. Private local review; not shared externally.
78 canonical regional keys; regions.csv governs scope and retains leading-zero FNS codes.
The historical pilot keys RU-KLU/RU-SVE are preserved. No national sums are authorized.
All baseline CSVs from research_exchange_2026-09-30 are preserved byte for byte.
field_dictionary.csv is the complete dictionary for every included CSV; the older
data_dictionary.csv and coverage.csv remain baseline records. coverage_registry.csv
adds per-table/per-region grain, latest coverage and missingness.

Annual production: 2015–2025, five scopes, 4,290 observations and 11 source-marker
missing cells. January-to-cutoff production: January 2015–July 2026, 54,210 cells
including 144 unavailable cells. Published indices define their own denominator;
annual growth and YTD growth are index minus 100. Monthly YoY is never averaged
to invent quarter output growth. Later vintage wins including missing cells.
Workbook footnotes, source update dates, source SHA-256, sheet and cell locators
are retained. Vintage differences may reflect reweighting and revisions.

matched_pit_production.csv covers all 936 region/period/section comparisons,
including absent mining. Reported nominal PIT YTD is matched with the production
January-to-cutoff window and an observed prior PIT window. Annual YTD is not Q4.
PIT coverage changes prevent an unqualified sum across industrial sections.

Fiscal actuals are nominal RUB for regional plus municipal budgets, excluding
federal budgets and territorial health-insurance funds. YTD is Q1 or H1.
period_flow is Q1 = Q1 YTD, Q2 = H1 minus same-year Q1; missing endpoints remain
missing and negative adjustments remain signed. The 14 disjoint parent functions
reconcile to total expenditure. Budget balance is revenue minus expenditure;
positive means surplus. Bridge drivers are revenue change minus spending change.
Transfer code 202 is nested in non-repayable receipts code 200 and does not
identify federal origin. Own-source means code 100 tax/non-tax receipts.
Never add overlapping revenue parents. Financing flows are not debt stocks.
Snapshot plan assignments do not establish original/amended budgets; existing
plan-component warnings remain. No fiscal-shortfall or causal verdict is made.

## Audit and reproduction
combined_source_metric_audit.ipynb is {'executed top to bottom' if execute else 'not executed'};
combined_source_metric_audit.html retains bounded tables and results.
Launch from this unpacked directory with Python and the required pandas, numpy,
nbformat, nbclient, nbconvert and ipykernel dependencies. The pure computation
companion can be run as: python rerun_audit.py PATH_TO_UNPACKED_PACKAGE.
It verifies processed input hashes and recomputes metrics from the exported CSVs.
source_cell_spotchecks.csv retains direct original-file samples for both pilot
regions. Raw workbook/CSV/archive contents are excluded; retrieval dates are
unknown where not established. These source-cell checks are samples, not an
independent re-read of every original cell. No external source access is needed.
FNS structure-file hashes describe the schema read at audit time; the original
parse-time schema hashes are not established and are not invented.

manifest.json hashes every payload file but excludes itself. The sibling
{sidecar.name} records the manifest, every file and archive SHA-256 after the zip
is closed. The sidecar is outside the zip to avoid recursive checksum references.
Build only into a fresh directory with scripts/build_updated_exchange.py.
The package does not validate dashboard layout, accessibility or browser downloads.
"""
    (output / "README.md").write_text(notes)
    for path in output.iterdir():
        privacy_check(path)
    manifest = dict(status="candidate_not_promoted", shared_externally=False, raw_files_included=False,
        generated_date="2026-10-02", eligible_regions=78, canonical_key_file="regions.csv",
        eligible_region_ids=sorted(canonical), nominal_scope="PIT and fiscal actuals nominal RUB; published production indices retain their comparison basis",
        budget_scope="Regional and municipal budgets; excludes federal budgets and territorial health-insurance funds",
        national_aggregation_authorized=False, baseline_csvs_preserved=True,
        notebook_executed=execute, notebook_visual_inspection="not performed; saved outputs checked structurally, main application visual review is separate",
        inputs={group:dict(path=folder + "/manifest.json", sha256=sha256_file(root / folder / "manifest.json"))
                for group,folder in {"baseline":BASELINE,"annual":ANNUAL,"cumulative":CUMULATIVE,"fiscal":FISCAL}.items()},
        origin_processed_files={name:dict(path=origin,sha256=sha256_file(root / origin)) for name,origin in origins.items()},
        tables={name:dict(rows=len(frame),columns=list(frame),grain=grains.get(name)) for name,frame in tables.items()},
        independent_source_cell_checks=len(tables["source_cell_spotchecks"]),
        input_validation={"baseline_recorded_payload_hashes":True,"cumulative_recorded_artifact_hashes":True,
            "fiscal_recorded_derived_hashes":True,"annual_manifest_dataset_hash":True,
            "annual_social_csvs_independently_reproduced":True},
        archive_checksum_record=sidecar.name, archive_checksum_location="external sidecar written after archive closes",
        files={path.name:sha256_file(path) for path in sorted(output.iterdir())})
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    privacy_check(output / "manifest.json")
    with ZipFile(archive, "x", ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(output.iterdir()):
            info = ZipInfo(output.name + "/" + path.name, date_time=(2026,10,2,0,0,0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    integrity = dict(status="candidate_not_promoted", shared_externally=False, archive_file=archive.name,
        archive_sha256=sha256_file(archive), manifest_file=output.name + "/manifest.json",
        manifest_sha256=sha256_file(output / "manifest.json"),
        files={path.name:sha256_file(path) for path in sorted(output.iterdir())})
    sidecar.write_text(json.dumps(integrity, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/research_exchange_2026-10-02")
    parser.add_argument("--no-execute", action="store_true", help="Only for source scaffolding; marks audit unexecuted")
    args = parser.parse_args()
    result = run(args.output, execute=not args.no_execute)
    print(json.dumps({"package":args.output.name,"regions":result["eligible_regions"],
        "tables":len(result["tables"]),"notebook_executed":result["notebook_executed"]}, indent=2))
