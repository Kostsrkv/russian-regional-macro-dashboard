"""Header-selected revenue rows from consolidated regional Form 0503317.

Select parent classification rows once. Amounts are nominal RUB and cumulative
within the year. The 202 group is transfers from other budgets; the broader
200 group includes other receipts and signed repayments.
"""
from decimal import Decimal

from .ingest_treasury import _RowsParser, _row_source_code, _parse_decimal, TreasuryIngestionError
from .treasury_fiscal import PLAN, ACTUAL

REVENUE_METRICS = {
    "TOTAL_REVENUE": "Total revenue",
    "10000000000000000": "Tax and non-tax revenue",
    "10101000000000110": "Corporate income tax",
    "10102000010000110": "Retained personal income tax",
    "20000000000000000": "Non-repayable receipts (net)",
    "20200000000000000": "Transfers from other budgets",
}
CORE_CODES = set(REVENUE_METRICS) - {"20200000000000000"}


def parse_revenue(raw: bytes):
    parser = _RowsParser()
    parser.feed(raw.decode("cp1251", errors="strict"))
    header = None
    records = []
    for ordinal, row in enumerate(parser.rows):
        if row and row[0] == "Наименование показателя":
            if row.count(PLAN) != 1 or row.count(ACTUAL) != 1:
                raise TreasuryIngestionError("Revenue header/perimeter missing or ambiguous")
            header = row
            continue
        if header is None or len(row) < 8 or row[1] != "010":
            continue
        code = ("TOTAL_REVENUE" if row[2] == "***" and row[0] == "Доходы бюджета - всего"
                else _row_source_code(row) if row[2] == "000" else None)
        if code not in REVENUE_METRICS:
            continue
        if len(row) != len(header):
            raise TreasuryIngestionError("Revenue row width differs from header")
        plan_col, actual_col = header.index(PLAN), header.index(ACTUAL)
        plan = _parse_decimal(row[plan_col], field=code + " plan")
        actual = _parse_decimal(row[actual_col], field=code + " actual")
        if not plan.is_finite() or not actual.is_finite():
            raise TreasuryIngestionError("Non-finite revenue amount")
        records.append(dict(revenue_code=code, revenue_name_en=REVENUE_METRICS[code],
                            revenue_name_ru=row[0], actual_ytd_rub=actual,
                            approved_plan_rub=plan, source_row_index=ordinal,
                            plan_column_index=plan_col, actual_column_index=actual_col))
    codes = [r["revenue_code"] for r in records]
    if len(codes) != len(set(codes)):
        raise TreasuryIngestionError("Duplicate revenue parent row")
    if not CORE_CODES.issubset(codes):
        raise TreasuryIngestionError("Missing required revenue parent rows")
    totals = {r["revenue_code"]: r for r in records}
    checks = []
    for column in ("actual_ytd_rub", "approved_plan_rub"):
        gap = (totals["TOTAL_REVENUE"][column] - totals["10000000000000000"][column]
               - totals["20000000000000000"][column])
        checks.append(dict(check="revenue_components_" + column, gap_rub=str(gap),
                           passed=abs(gap) <= Decimal("0.02"), blocking=column == "actual_ytd_rub"))
    return records, checks
