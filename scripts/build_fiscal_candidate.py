"""Build reviewed candidate exports only. No promotion, network or deployment."""
from pathlib import Path
import sys
import json
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from macro_rus.treasury_fiscal import extract_archive

def run(output):
    output=Path(output)
    if output.exists():
        raise FileExistsError('Preserve prior run; choose a fresh output directory')
    rows,checks,sources=[],[],[]
    for path in sorted([*ROOT.glob('KBSRF*.zip'),*ROOT.glob('КБСРФ*.zip')]):
        data,events,source=extract_archive(path)
        rows.extend(data);checks.extend(events);sources.append(source)
    data=pd.DataFrame(rows);quality=pd.DataFrame(checks)
    key=['region_id','period','kind','metric_code','source_sha256']
    if data.duplicated(key).any():
        raise ValueError('Duplicate candidate grain')
    failures=quality.loc[quality.blocking & ~quality.passed]
    if len(failures):
        raise ValueError(failures[['region_id','period','check','gap_rub']].to_dict('records'))
    output.mkdir(parents=True)
    data.to_csv(output/'fiscal_observations.csv',index=False)
    quality.to_csv(output/'reconciliation_checks.csv',index=False)
    summary=data.loc[data.kind.eq('summary')].pivot(index=['region_id','period'],columns='metric_code',values='actual_ytd_rub')
    summary.to_csv(output/'fiscal_summary.csv')
    manifest=dict(status='candidate_not_promoted',rows=len(data),region_periods=len(summary),
                  checks=len(quality),blocking_failures=len(failures),
                  plan_warnings=int((~quality.passed & ~quality.blocking).sum()),sources=sources,
                  limitations=['YTD, not individual quarters','Snapshot plans are not original budgets',
                               'Financing flows are not debt stocks','Cutoffs derived from archive filenames'])
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in manifest.items() if k!='sources'},indent=2))
    return manifest

if __name__=='__main__':
    run(sys.argv[1] if len(sys.argv)>1 else ROOT/'outputs/treasury_fiscal_candidate')
