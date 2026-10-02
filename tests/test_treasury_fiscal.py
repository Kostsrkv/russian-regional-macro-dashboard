from decimal import Decimal
from html import escape
import pytest
from macro_rus.treasury_fiscal import parse_fiscal, PLAN, ACTUAL
from macro_rus.ingest_treasury import TreasuryIngestionError


def report(balance='20', expense='80', duplicate=False, wrong_scope=False):
    rows=[]
    def header(width, plan, actual):
        h=['']*width;h[0]='Наименование показателя'
        h[plan]=PLAN;h[actual]=ACTUAL if not wrong_scope else 'Исполнено: бюджет субъекта РФ'
        rows.append(h)
    def add(width,line,ap,code,amount,plan_col,actual_col,plan=None):
        r=['0']*width;r[:6]=[code,line,ap,code,'0000000000','000']
        r[plan_col]=str(amount if plan is None else plan);r[actual_col]=str(amount)
        rows.append(r)
    header(36,10,24)
    add(36,'010','***','850',100,10,24)
    header(34,8,22)
    add(34,'200','***','9600',expense,8,22)
    add(34,'200','000','0100',30,8,22)
    add(34,'200','000','0700',50,8,22)
    add(34,'200','000','0701',25,8,22)  # Child must not be double counted.
    if duplicate: add(34,'200','000','0700',50,8,22)
    add(34,'450','***','7900',balance,8,22)
    header(36,10,24)
    add(36,'500','***','9000',-20,10,24)
    return ('<table>'+''.join('<tr>'+''.join('<td>'+escape(c)+'</td>' for c in r)+'</tr>' for r in rows)+'</table>').encode('cp1251')


def test_headers_select_consolidated_scope_and_sections_do_not_double_count():
    rows,checks=parse_fiscal(report())
    totals={r['metric_code']:r for r in rows}
    assert totals['expenditure_total']['actual_column_index']==22
    assert totals['financing_total']['actual_column_index']==24
    assert totals['budget_balance']['actual_ytd_rub']==Decimal('20')
    assert totals['financing_total']['actual_ytd_rub']==Decimal('-20')
    assert '0701' not in totals
    assert all(c['passed'] for c in checks)


def test_wrong_perimeter_rejected():
    with pytest.raises(TreasuryIngestionError,match='perimeter'):
        parse_fiscal(report(wrong_scope=True))


def test_duplicate_function_rejected():
    with pytest.raises(TreasuryIngestionError,match='Duplicate'):
        parse_fiscal(report(duplicate=True))


def test_balance_and_function_errors_are_blocking():
    _,checks=parse_fiscal(report(balance='19',expense='81'))
    failed={c['check'] for c in checks if c['blocking'] and not c['passed']}
    assert 'balance_plus_financing_equals_zero' in failed
    assert 'functions_sum_actual_ytd_rub' in failed


def test_missing_total_rejected():
    raw=report().replace(b'<td>500</td>',b'<td>999</td>')
    with pytest.raises(TreasuryIngestionError,match='Required fiscal total'):
        parse_fiscal(raw)
