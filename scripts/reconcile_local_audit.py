"""Rebuild in memory and compare to promoted values; never promote or deploy."""
from pathlib import Path
import json
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from macro_rus.pipeline import build_tables

OUT = ROOT / 'outputs/data_inventory_after_download_verified'
SPECS = {
    'pit_receipts': (['region_id','period','okved_raw'], ['pit_ytd_rub','pit_flow_rub']),
    'industrial_production': (['region_id','period','okved_section','index_measure'], ['index_value']),
    'budget_execution': (['region_id','period','budget_level','revenue_code'], ['actual_ytd_rub','approved_plan_rub']),
}

def run():
    tables, validation, diagnostics = build_tables(ROOT)
    summary = {'tables': {}, 'validation_blocking': len(validation.blocking),
               'validation_status_counts': validation.events.status.value_counts().to_dict(),
               'scope': 'Reproduces existing pipeline; not independent certification of source interpretation.'}
    for name, (keys, values) in SPECS.items():
        fresh = tables[name].copy()
        old = pd.read_parquet(ROOT / 'data/promoted/current' / f'{name}.parquet')
        for frame in (fresh, old):
            frame['period'] = pd.to_datetime(frame['period'])
        joined = fresh[keys+values].merge(old[keys+values], on=keys, how='outer',
                                         suffixes=('_fresh','_promoted'), indicator=True, validate='one_to_one')
        mismatch = joined['_merge'].ne('both')
        for value in values:
            a, b = joined[f'{value}_fresh'], joined[f'{value}_promoted']
            equal = (a.isna() & b.isna()) | a.eq(b).fillna(False)
            mismatch |= ~equal
        differences = joined.loc[mismatch]
        differences.to_csv(OUT / f'{name}_differences.csv', index=False)
        summary['tables'][name] = {
            'fresh_rows': len(fresh), 'promoted_rows': len(old),
            'different_or_unmatched_rows': len(differences),
            'periods': sorted(fresh.period.dt.strftime('%Y-%m-%d').unique().tolist()),
            'null_measures': {c:int(fresh[c].isna().sum()) for c in values},
        }
    validation.events.to_csv(OUT / 'rebuild_quality_events.csv', index=False)
    (OUT / 'rebuild_diagnostics.json').write_text(json.dumps(diagnostics,ensure_ascii=False,indent=2))
    (OUT / 'reconciliation.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps({**summary, 'tables':{k:{a:b for a,b in v.items() if a!='periods'} for k,v in summary['tables'].items()}},indent=2))
    return summary

if __name__ == '__main__':
    run()
