"""Read-only NBA moneyline outcome analysis from saved, published picks.

Does not fetch odds, grade games, modify picks, or deploy calibration.
Only saved Top-5 NBA moneyline selections are covered. Existing picks may
predate this audit; their recalculated calibration is retrospective.
"""
import csv
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

T = 0.70
EPS = 1e-12
OUT = Path('nba_shadow_outcome_report')
FIELDS = ['id','date','matchup','selection','odds','result','created_at','source',
          'original_probability','calibrated_probability','break_even_probability',
          'original_edge_pp','calibrated_edge_pp','original_units','calibrated_units',
          'original_flat_profit','calibrated_flat_profit',
          'original_variable_profit','calibrated_variable_profit',
          'original_log_loss','calibrated_log_loss','original_brier','calibrated_brier',
          'status','result_source']


def normalize_probability(raw):
    if raw is None:
        return None
    p = float(raw)
    if not math.isfinite(p):
        return None
    # Model probability stored in picks is expected to be 0..1.
    # Do not silently interpret 65 as 65%.
    if not 0 <= p <= 1:
        return None
    return max(EPS, min(1-EPS, p))


def calibrated(p):
    logit = math.log(p/(1-p))/T
    return 1/(1+math.exp(-max(-40, min(40, logit))))


def implied(odds):
    if odds is None:
        return None
    n = int(odds)
    if n >= 100:
        return 100/(n+100)
    if n <= -100:
        return -n/(-n+100)
    return None


def units(edge_pp):
    if edge_pp <= 0:
        return 0
    return 5 if edge_pp >= 15 else 4 if edge_pp >= 10 else 3 if edge_pp >= 6 else 2 if edge_pp >= 3 else 1


def profit(result, odds, stake):
    if stake == 0:
        return 0.0
    if result == 'Loss':
        return -float(stake)
    if result == 'Win':
        return float(stake) * ((odds/100) if odds > 0 else (100/abs(odds)))
    return None


def metrics(records, prefix):
    completed = [r for r in records if r['status'] == 'settled']
    eligible = [r for r in completed if r[prefix+'_units'] > 0]
    def avg(field):
        return sum(r[field] for r in completed)/len(completed) if completed else None
    return {
        'settled_evaluated': len(completed),
        'log_loss': avg(prefix+'_log_loss'),
        'brier_score': avg(prefix+'_brier'),
        'positive_edge_bets': len(eligible),
        'flat_1u_profit': sum(r[prefix+'_flat_profit'] for r in eligible),
        'flat_1u_risked': len(eligible),
        'flat_1u_roi': (sum(r[prefix+'_flat_profit'] for r in eligible)/len(eligible)) if eligible else None,
        'variable_profit': sum(r[prefix+'_variable_profit'] for r in eligible),
        'variable_risked': sum(r[prefix+'_units'] for r in eligible),
        'variable_roi': (sum(r[prefix+'_variable_profit'] for r in eligible)/sum(r[prefix+'_units'] for r in eligible)) if eligible else None,
    }


def analyze(rows):
    records=[]
    skipped=[]
    for row in rows:
        p=normalize_probability(row['estimated_prob'])
        be=implied(row['odds'])
        if p is None or be is None:
            skipped.append({'id':row['id'],'reason':'missing/invalid estimated_prob or odds'})
            continue
        q=calibrated(p)
        oe=100*(p-be)
        ce=100*(q-be)
        ou,cu=units(oe),units(ce)
        result=row['result']
        settled=result in ('Win','Loss')
        record={
            'id':row['id'],'date':row['date'],'matchup':row['matchup'],
            'selection':row['selection'],'odds':row['odds'],'result':result,
            'created_at':row['created_at'].isoformat() if row['created_at'] else None,
            'source':row['source'],'result_source':row['result_source'],
            'original_probability':p,'calibrated_probability':q,
            'break_even_probability':be,'original_edge_pp':oe,'calibrated_edge_pp':ce,
            'original_units':ou,'calibrated_units':cu,
            'status':'settled' if settled else ('pending' if result=='Pending' else 'excluded_'+str(result).lower()),
            'original_flat_profit':profit(result,row['odds'],1) if settled and ou else None,
            'calibrated_flat_profit':profit(result,row['odds'],1) if settled and cu else None,
            'original_variable_profit':profit(result,row['odds'],ou) if settled and ou else None,
            'calibrated_variable_profit':profit(result,row['odds'],cu) if settled and cu else None,
            'original_log_loss':None,'calibrated_log_loss':None,
            'original_brier':None,'calibrated_brier':None,
        }
        if settled:
            y=1 if result=='Win' else 0
            for prefix,prob in (('original',p),('calibrated',q)):
                record[prefix+'_log_loss']=-(y*math.log(prob)+(1-y)*math.log(1-prob))
                record[prefix+'_brier']=(y-prob)**2
        records.append(record)
    return records,skipped


def main():
    database_url=os.environ.get('DATABASE_URL')
    if not database_url:
        raise RuntimeError('DATABASE_URL is required')
    # Transaction-level read-only guard: cannot mutate production database.
    with psycopg.connect(database_url,row_factory=dict_row,connect_timeout=20) as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            rows=conn.execute('''SELECT id,date,matchup,market,selection,odds,
                        estimated_prob,result,created_at,source,result_source
                FROM picks WHERE sport='NBA' AND kind='Hypothetical'
                  AND rank IS NOT NULL AND source LIKE 'Auto:%%'
                  AND lower(market) IN ('h2h','moneyline')
                ORDER BY date,id''').fetchall()
    records,skipped=analyze(rows)
    report={
        'generated_at_utc':datetime.now(timezone.utc).isoformat(),
        'temperature':T,'scope':'published automated NBA Top-5 moneyline selections only',
        'database_rows':len(rows),'evaluated_rows':len(records),
        'settled_evaluated':sum(r['status']=='settled' for r in records),
        'pending':sum(r['status']=='pending' for r in records),
        'skipped':skipped,
        'original':metrics(records,'original'),
        'calibrated':metrics(records,'calibrated'),
        'limitations':[
            'No new odds or ESPN requests; reads the existing settled pick ledger only.',
            'Only published Top-5 NBA moneyline picks; not all NBA candidates.',
            'Odds and probabilities are from stored rows; timestamp simultaneity is not verified.',
            'The original pick estimated_prob is assumed to represent the selected team win probability.',
            'Applying T=0.70 to previously saved predictions is retrospective, not a pre-registered shadow bet.',
            'Both variants evaluate the same selected picks, not separately selected betting portfolios.',
            'Negative/zero estimated edges have 0u; no-bet selections still count for probability scoring.',
            'Returns are hypothetical; no transaction costs, limits, or line movement modeled.',
            'No production calibration or staking changes; no database writes.'
        ]
    }
    OUT.mkdir(exist_ok=True)
    (OUT/'nba_shadow_outcomes_summary.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    with (OUT/'nba_shadow_outcomes_rows.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)
    print('NBA shadow outcome audit:',report['settled_evaluated'],'settled,',report['pending'],'pending,',len(skipped),'skipped')
    print('Original:',report['original'])
    print('Calibrated:',report['calibrated'])
    print('Report directory:',OUT)

if __name__=='__main__':
    main()
