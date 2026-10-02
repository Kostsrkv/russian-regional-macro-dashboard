"""Candidate fiscal extension of 0503317, separate from promoted revenue tables.

Amounts are source RUB; actuals are YTD, plans are snapshot assignments.
Positive budget balance means surplus. Financing retains source signs and is
NOT debt stock or gross borrowing. Header names determine the fiscal perimeter.
"""
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import re
from zipfile import ZipFile

import pandas as pd

from .ingest_treasury import (
    _RowsParser, _find_html_member, _parse_catalogue, _parse_decimal,
    _resolve_regions, _cutoff_from_path, _sha256_file, _decode_zip_name,
    TreasuryIngestionError, SOURCE_URL,
)

PERIMETER = 'консолидированный бюджет субъекта РФ'
PLAN = 'Утвержденные бюджетные назначения: ' + PERIMETER
ACTUAL = 'Исполнено: ' + PERIMETER
TOTALS = {'010': 'revenue_total', '200': 'expenditure_total',
          '450': 'budget_balance', '500': 'financing_total',
          '520': 'internal_financing', '620': 'external_financing',
          '700': 'cash_balance_change'}


def parse_fiscal(raw: bytes):
    """Select top-level rows only; never sum a parent with its descendants."""
    parser = _RowsParser()
    parser.feed(raw.decode('cp1251', errors='strict'))
    selected = []
    header = None
    for ordinal, row in enumerate(parser.rows):
        if row and row[0] == 'Наименование показателя':
            if row.count(PLAN) != 1 or row.count(ACTUAL) != 1:
                raise TreasuryIngestionError('Fiscal header/perimeter missing or ambiguous')
            header = row
            continue
        if header is None or len(row) < 6:
            continue
        kind = None
        code = row[1]
        if row[2] == '***' and code in TOTALS:
            kind, key = 'summary', TOTALS[code]
        elif (code == '200' and row[2] == '000'
              and re.fullmatch(r'(0[1-9]|1[0-4])00', row[3])
              and row[4:6] == ['0000000000', '000']):
            kind, key = 'expenditure_function', row[3]
        elif (code in {'520','620'} and row[2] == '000'
              and re.fullmatch(r'\d{4}', row[3])
              and row[4:8] == ['0000','00','0000','000']):
            kind, key = 'financing_component', code + ':' + row[3]
        if kind is None:
            continue
        if len(row) != len(header):
            raise TreasuryIngestionError(f'Fiscal row width changed at row {ordinal}')
        plan_col, actual_col = header.index(PLAN), header.index(ACTUAL)
        plan = _parse_decimal(row[plan_col], field=key+' plan')
        actual = _parse_decimal(row[actual_col], field=key+' actual')
        if not plan.is_finite() or not actual.is_finite():
            raise TreasuryIngestionError('Nonfinite fiscal amount')
        selected.append(dict(kind=kind, metric_code=key, name_ru=row[0],
                             source_line_code=code, source_row_index=ordinal,
                             plan_column_index=plan_col, actual_column_index=actual_col,
                             plan_at_cutoff_rub=plan, actual_ytd_rub=actual))
    keys = [(r['kind'],r['metric_code']) for r in selected]
    if len(keys) != len(set(keys)):
        raise TreasuryIngestionError('Duplicate fiscal metric')
    totals = {r['metric_code']:r for r in selected if r['kind']=='summary'}
    for key in ['revenue_total','expenditure_total','budget_balance','financing_total']:
        if key not in totals:
            raise TreasuryIngestionError('Required fiscal total absent: '+key)
    checks = []
    def check(name, gap, blocking=True):
        checks.append(dict(check=name, gap_rub=str(gap),
                           passed=abs(gap)<=Decimal('0.02'), blocking=blocking))
    value = lambda key, col='actual_ytd_rub': totals[key][col]
    check('revenue_minus_expenditure_equals_balance',
          value('revenue_total')-value('expenditure_total')-value('budget_balance'))
    check('balance_plus_financing_equals_zero',value('budget_balance')+value('financing_total'))
    functions = [r for r in selected if r['kind']=='expenditure_function']
    if not functions:
        raise TreasuryIngestionError('Expenditure functions absent')
    for col in ['actual_ytd_rub','plan_at_cutoff_rub']:
        check('functions_sum_'+col, sum((r[col] for r in functions),Decimal(0))-value('expenditure_total',col))
    # Planned expenditure assignments can exceed the revenue/financing budget
    # perimeter. Keep this source discrepancy visible, never force it to balance.
    check('plan_revenue_minus_expenditure_equals_reported_balance',
          value('revenue_total','plan_at_cutoff_rub')-value('expenditure_total','plan_at_cutoff_rub')-value('budget_balance','plan_at_cutoff_rub'),False)
    for parent, line in [('internal_financing','520'),('external_financing','620')]:
        children=[r for r in selected if r['kind']=='financing_component' and r['source_line_code']==line]
        if parent in totals and children:
            check(parent+'_components',sum((r['actual_ytd_rub'] for r in children),Decimal(0))-value(parent))
    return selected, checks


def extract_archive(path, regions=None):
    path = Path(path)
    sha = _sha256_file(path)
    cutoff = _cutoff_from_path(path)
    records, checks = [], []
    with ZipFile(path) as outer:
        bad = outer.testzip()
        member = _find_html_member(outer)
        payload = outer.read(member)
    with ZipFile(BytesIO(payload)) as archive:
        bad_html = archive.testzip()
        if bad_html:
            raise TreasuryIngestionError('HTML archive CRC failure: '+bad_html)
        catalog = _parse_catalogue(archive.read('TerrList_428m.html'))
        for region in _resolve_regions(regions):
            html_name = catalog[region.name_ru]
            rows, events = parse_fiscal(archive.read(html_name))
            common = dict(region_id=region.region_id, region_name_ru=region.name_ru,
                          period=(cutoff-pd.Timedelta(days=1)).date().isoformat(),
                          reporting_cutoff=cutoff.date().isoformat(),
                          budget_level='consolidated_regional_budget',
                          units='RUB', frequency='YTD', source_file=path.name,
                          source_sha256=sha, source_member=html_name, source_url=SOURCE_URL)
            records.extend({**common, **r} for r in rows)
            checks.extend({**common, **e} for e in events)
    return records, checks, dict(source_file=path.name, sha256=sha,
                                ancillary_crc_failure=_decode_zip_name(bad) if bad else None,
                                selected_html_crc_passed=True)
