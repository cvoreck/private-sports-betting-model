"""Chronological, leakage-aware home-win training from archived ESPN regular-season games.
No Odds API calls. Trains candidates per sport and promotes only when validated.
"""
import json, logging, math, os, re
from collections import defaultdict
from datetime import datetime, timezone
import psycopg
from psycopg.rows import dict_row

SPORTS = ('basketball_nba', 'icehockey_nhl', 'americanfootball_nfl')
MIN_TRAIN, MIN_TEST, MIN_PRIOR = 80, 25, 5
SCHEMA = '''
CREATE TABLE IF NOT EXISTS sports_edge_training_runs (
 id BIGSERIAL PRIMARY KEY, trained_at TIMESTAMPTZ NOT NULL DEFAULT now(), sport TEXT NOT NULL,
 status TEXT NOT NULL, train_games INT NOT NULL, test_games INT NOT NULL,
 candidate_logloss DOUBLE PRECISION, baseline_logloss DOUBLE PRECISION,
 incumbent_logloss DOUBLE PRECISION, details JSONB NOT NULL DEFAULT '{}'::jsonb);
CREATE TABLE IF NOT EXISTS sports_edge_trained_models (
 sport TEXT PRIMARY KEY, version TEXT NOT NULL, trained_at TIMESTAMPTZ NOT NULL,
 coefficients JSONB NOT NULL, test_logloss DOUBLE PRECISION NOT NULL,
 train_games INT NOT NULL, test_games INT NOT NULL);
'''
def norm(s): return re.sub('[^a-z0-9]+', '', (s or '').lower())
def sigmoid(x): return 1/(1+math.exp(-max(-30,min(30,x))))
def clip(p): return min(.999,max(.001,p))
def logloss(ys,ps): return sum(-y*math.log(clip(p))-(1-y)*math.log(clip(1-p)) for y,p in zip(ys,ps))/len(ys)
def features(home,away,hist):
    h=hist[home][-10:]; a=hist[away][-10:]
    if len(h)<MIN_PRIOR or len(a)<MIN_PRIOR: return None
    wr=lambda rows: sum(x[0] for x in rows)/len(rows)
    margin=lambda rows: sum(x[1] for x in rows)/len(rows)
    return [1.0, wr(h)-wr(a), max(-3,min(3,(margin(h)-margin(a))/15))]
def parse_game(e, sport):
    try:
        if (e.get('season') or {}).get('type')!=2: return None
        if not ((e.get('status') or {}).get('type') or {}).get('completed'):return None
        cs=(e.get('competitions') or [{}])[0].get('competitors') or []
        h=next(x for x in cs if x.get('homeAway')=='home')
        a=next(x for x in cs if x.get('homeAway')=='away')
        hs=float(h['score']); aa=float(a['score'])
        if hs==aa:return None
        hn=norm((h.get('team') or {}).get('displayName'))
        an=norm((a.get('team') or {}).get('displayName'))
        dt=datetime.fromisoformat(e['date'].replace('Z','+00:00'))
        if not hn or not an or dt>datetime.now(timezone.utc):return None
        return (dt, str(e.get('id') or ''),hn,an,hs,aa)
    except (ValueError,TypeError,KeyError,StopIteration,IndexError):return None
def dataset(rows,sport):
    games={}
    for r in rows:
        e=r['payload']
        if not isinstance(e,dict):continue
        g=parse_game(e,sport)
        if g and g[1]:games[g[1]]=g
    hist=defaultdict(list); examples=[]
    for dt,eid,h,a,hs,aa in sorted(games.values(),key=lambda g:(g[0],g[1])):
        x=features(h,a,hist)
        if x is not None:examples.append((dt,eid,x,int(hs>aa)))
        hist[h].append((int(hs>aa),hs-aa))
        hist[a].append((int(aa>hs),aa-hs))
    return examples,len(games)
def fit(data):
    w=[0.0,0.0,0.0]
    for step in range(900):
        grad=[0.0,0.0,0.0]
        for _,_,x,y in data:
            err=sigmoid(sum(v*c for v,c in zip(w,x)))-y
            for j in range(3):grad[j]+=err*x[j]
        rate=.35
        for j in range(3):w[j]-=rate*(grad[j]/len(data)+(0 if j==0 else .025*w[j]))
    return w
def predict(w, data):return [sigmoid(sum(v*c for v,c in zip(w,x))) for _,_,x,_ in data]
def evaluate(w,data):return logloss([z[3] for z in data],predict(w,data))
def run(conn,sport):
    rows=conn.execute('''SELECT event_id,payload FROM historical_snapshots
      WHERE source='espn' AND sport=%s AND category='game'
      ORDER BY collected_at DESC LIMIT 30000''',(sport,)).fetchall()
    data,game_count=dataset(rows,sport)
    n=len(data); cut=int(n*.8); train,test=data[:cut],data[cut:]
    details={'archived_unique_final_games':game_count,'eligible_pregame_examples':n,
             'first_test_game':test[0][0].isoformat() if test else None,
             'last_test_game':test[-1][0].isoformat() if test else None}
    status='insufficient_data'; cand=base=inc=None
    if len(train)>=MIN_TRAIN and len(test)>=MIN_TEST:
        weights=fit(train); cand=evaluate(weights,test)
        ys=[z[3] for z in test]
        # Baseline is 50/50; cannot exploit later-season outcomes.
        base=logloss(ys,[.5]*len(ys))
        incumbent=conn.execute('SELECT coefficients FROM sports_edge_trained_models WHERE sport=%s',(sport,)).fetchone()
        if incumbent:
            old=incumbent['coefficients']['weights']
            inc=evaluate(old,test)
        # Require a meaningful improvement, not merely a rounding difference.
        if cand < base-.005 and (inc is None or cand < inc-.005):
            version='rolling-homewin-v1-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            conn.execute('''INSERT INTO sports_edge_trained_models
             (sport,version,trained_at,coefficients,test_logloss,train_games,test_games)
             VALUES (%s,%s,now(),%s,%s,%s,%s)
             ON CONFLICT(sport) DO UPDATE SET version=EXCLUDED.version,
             trained_at=EXCLUDED.trained_at,coefficients=EXCLUDED.coefficients,
             test_logloss=EXCLUDED.test_logloss,train_games=EXCLUDED.train_games,
             test_games=EXCLUDED.test_games''',
             (sport,version,json.dumps({'weights':weights,'features':['intercept','win_rate_diff_last10','margin_diff_last10_scaled']}),cand,len(train),len(test)))
            status='promoted'
        else:status='kept_existing'
        details['candidate_weights']=weights
    conn.execute('''INSERT INTO sports_edge_training_runs
      (sport,status,train_games,test_games,candidate_logloss,baseline_logloss,incumbent_logloss,details)
      VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
      (sport,status,len(train),len(test),cand,base,inc,json.dumps(details)))
    logging.info('%s: %s | final games=%d examples=%d train=%d test=%d candidate=%s baseline=%s incumbent=%s',
                 sport,status,game_count,n,len(train),len(test),cand,base,inc)
def main():
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    with psycopg.connect(os.environ['DATABASE_URL'],row_factory=dict_row) as conn:
        conn.execute(SCHEMA)
        for sport in SPORTS:run(conn,sport)
if __name__=='__main__':main()
