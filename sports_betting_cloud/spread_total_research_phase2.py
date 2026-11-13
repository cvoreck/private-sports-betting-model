"""Read-only Phase 2: opponent-adjusted pregame margin/total research.

Uses only canonical final scores. No bookmaker probabilities or profitability claims.
"""
import json
import logging
import math
import os
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SPORTS = ('basketball_nba', 'americanfootball_nfl', 'icehockey_nhl')
WINDOW = 16
MIN_GAMES = 5
HALF_LIFE = 8


def avg(xs):
    return sum(xs) / len(xs) if xs else 0.0


def recent(xs):
    xs = list(xs)
    if not xs:
        return 0.0
    ws = [2 ** (-(len(xs)-1-i)/HALF_LIFE) for i in range(len(xs))]
    return sum(a*b for a,b in zip(xs,ws))/sum(ws)


def solve(a,b):
    n=len(b)
    a=[list(row)+[b[i]] for i,row in enumerate(a)]
    for i in range(n):
        pivot=max(range(i,n),key=lambda j:abs(a[j][i]))
        if abs(a[pivot][i])<1e-10:
            raise ValueError('Singular regression matrix')
        a[i],a[pivot]=a[pivot],a[i]
        div=a[i][i]
        for j in range(i,n+1): a[i][j]/=div
        for k in range(n):
            if k==i: continue
            factor=a[k][i]
            for j in range(i,n+1): a[k][j]-=factor*a[i][j]
    return [a[i][n] for i in range(n)]


def train_model(rows, target, lam):
    # Standardization uses training rows only; intercept is unpenalized.
    p=len(rows[0]['features'])
    means=[avg([r['features'][j] for r in rows]) for j in range(p)]
    stds=[max(1e-6, math.sqrt(avg([(r['features'][j]-means[j])**2 for r in rows]))) for j in range(p)]
    xtx=[[0.0]*(p+1) for _ in range(p+1)]
    xty=[0.0]*(p+1)
    for r in rows:
        x=[1.0]+[(v-means[j])/stds[j] for j,v in enumerate(r['features'])]
        y=r[target]
        for i in range(p+1):
            xty[i]+=x[i]*y
            for j in range(p+1): xtx[i][j]+=x[i]*x[j]
    for i in range(1,p+1): xtx[i][i]+=lam*len(rows)
    return {'coefficients':solve(xtx,xty),'means':means,'stds':stds,'lambda':lam}


def prediction(model, row):
    x=[1.0]+[(v-model['means'][j])/model['stds'][j] for j,v in enumerate(row['features'])]
    return sum(a*b for a,b in zip(model['coefficients'],x))


def metrics(rows, values, target):
    errors=[p-r[target] for r,p in zip(rows,values)]
    return {'n':len(errors),'mae':round(avg([abs(e) for e in errors]),4),
            'rmse':round(math.sqrt(avg([e*e for e in errors])),4),
            'bias':round(avg(errors),4)}


def build_examples(games, sport):
    # Rolling pregame opponent-adjusted offensive/defensive residuals.
    # Opponent strength is estimated strictly BEFORE each game; no future results.
    history=defaultdict(lambda:deque(maxlen=WINDOW))
    examples=[]
    seen=set()
    for g in games:
        eid=str(g['event_id'])
        if eid in seen: continue
        seen.add(eid)
        h,a=str(g['home_team_id']),str(g['away_team_id'])
        if not h or not a or h==a: continue
        hs,aws=float(g['home_score']),float(g['away_score'])
        if not all(math.isfinite(v) and v>=0 for v in (hs,aws)): continue
        hh,ah=history[h],history[a]
        if len(hh)>=MIN_GAMES and len(ah)>=MIN_GAMES:
            hf=recent([x[0] for x in hh]); ha=recent([x[1] for x in hh])
            af=recent([x[0] for x in ah]); aa=recent([x[1] for x in ah])
            base_home=(hf+aa)/2
            base_away=(af+ha)/2
            # Prior opponent-adjusted residuals: scoring versus opponent's pregame
            # allowed average, and allowed versus opponent's pregame scoring average.
            h_off=recent([x[2] for x in hh]); h_def=recent([x[3] for x in hh])
            a_off=recent([x[2] for x in ah]); a_def=recent([x[3] for x in ah])
            # Sport-specific features: hockey prioritizes goal differential;
            # football/basketball include scoring volatility and home splits.
            h_margin=recent([x[0]-x[1] for x in hh]); a_margin=recent([x[0]-x[1] for x in ah])
            h_home=[x for x in hh if x[4]]
            a_away=[x for x in ah if not x[4]]
            venue=(recent([x[0]-x[1] for x in h_home])-recent([x[0]-x[1] for x in a_away])) if h_home and a_away else h_margin-a_margin
            if sport=='icehockey_nhl':
                features=[base_home-base_away,base_home+base_away,h_off-a_off,a_def-h_def,h_margin-a_margin,venue]
            else:
                h_vol=math.sqrt(avg([(x[0]+x[1]-recent([z[0]+z[1] for z in hh]))**2 for x in hh]))
                a_vol=math.sqrt(avg([(x[0]+x[1]-recent([z[0]+z[1] for z in ah]))**2 for x in ah]))
                features=[base_home-base_away,base_home+base_away,h_off-a_off,a_def-h_def,h_margin-a_margin,venue,h_vol+a_vol]
            examples.append({'event_id':eid,'date':g['played_at'].isoformat(),
                             'margin':hs-aws,'total':hs+aws,
                             'baseline_margin':base_home-base_away,
                             'baseline_total':base_home+base_away,'features':features})
        # Opponent reference values captured before this result is added.
        h_opp_allow=recent([x[1] for x in ah]) if ah else None
        a_opp_allow=recent([x[1] for x in hh]) if hh else None
        h_opp_score=recent([x[0] for x in ah]) if ah else None
        a_opp_score=recent([x[0] for x in hh]) if hh else None
        # If opponent has no prior history, residual is neutral rather than
        # accidentally using the game's own score as its reference.
        hh.append((hs,aws,hs-h_opp_allow if h_opp_allow is not None else 0.0,
                   aws-h_opp_score if h_opp_score is not None else 0.0,True))
        ah.append((aws,hs,aws-a_opp_allow if a_opp_allow is not None else 0.0,
                   hs-a_opp_score if a_opp_score is not None else 0.0,False))
    return examples


def evaluate(examples):
    n=len(examples)
    if n<300: return {'status':'INSUFFICIENT_DATA','eligible_games':n}
    c1,c2=int(n*.6),int(n*.8)
    train,valid,test=examples[:c1],examples[c1:c2],examples[c2:]
    result={'status':'RESEARCH_ONLY','eligible_games':n,
            'splits':{'train':len(train),'validation':len(valid),'test':len(test),
                      'test_start':test[0]['date'],'test_end':test[-1]['date']},'markets':{}}
    for target,market in [('margin','spread'),('total','total')]:
        baseline_key='baseline_'+target
        candidates=[]
        for lam in (0.001,0.01,0.1,1.0,10.0):
            model=train_model(train,target,lam)
            vm=metrics(valid,[prediction(model,r) for r in valid],target)
            candidates.append((vm['mae'],lam,model,vm))
        _,lam,model,validation=sorted(candidates,key=lambda x:(x[0],x[1]))[0]
        result['markets'][market]={
            'selected_lambda_from_validation':lam,
            'validation':{'baseline':metrics(valid,[r[baseline_key] for r in valid],target),
                          'trained':validation},
            'test':{'baseline':metrics(test,[r[baseline_key] for r in test],target),
                    'trained':metrics(test,[prediction(model,r) for r in test],target)},
            'feature_count':len(train[0]['features']),
            'note':'Test was held out from hyperparameter selection. No sportsbook lines or ROI.'}
    return result


def main():
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    url=os.environ.get('DATABASE_URL')
    if not url: raise RuntimeError('DATABASE_URL required')
    report={'generated_at':datetime.now(timezone.utc).isoformat(),
            'read_only':True,'database_writes':0,'external_api_calls':0,'model_promotions':0,
            'method':'Pregame opponent residuals, rolling 16 games, validation-selected ridge penalty, chronological 60/20/20.',
            'limitations':['No historical bookmaker lines/odds; no calibrated cover probability, push rate, EV, or ROI.',
                           'No injuries, lineups, pace, goalie or play-by-play data.',
                           'Baseline is a scoring-average proxy, not identical to production.',
                           'Single chronological holdout; further rolling-origin evaluation recommended.'],
            'sports':{}}
    with psycopg.connect(url,row_factory=dict_row,connect_timeout=20) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        for sport in SPORTS:
            logging.info('Evaluating %s',sport)
            games=conn.execute('''SELECT event_id,played_at,home_team_id,away_team_id,home_score,away_score
               FROM canonical_completed_games WHERE sport=%s AND season_type=2
               ORDER BY played_at,event_id''',(sport,)).fetchall()
            report['sports'][sport]=evaluate(build_examples(games,sport))
        conn.rollback()
    Path('spread_total_phase2_report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    logging.info('Report saved; database unchanged')

if __name__=='__main__': main()
