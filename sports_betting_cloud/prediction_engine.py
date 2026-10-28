"""Experimental independent sports models; no market odds used to estimate probabilities.
ESPN scoreboard regular-season verification and W/L record model for NFL/NBA/NHL.
NBA/NHL spread and total models require >=4 recent final games per team.
Research only: not calibrated or backtested; missing data means no prediction.
"""
import logging, math, os, re, statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests, psycopg
from psycopg.rows import dict_row

PT=ZoneInfo('America/Los_Angeles')
SPORTS={'basketball_nba':('NBA','basketball','nba',6), 'icehockey_nhl':('NHL','hockey','nhl',6), 'americanfootball_nfl':('NFL','football','nfl',4)}
SCHEMA='''
CREATE TABLE IF NOT EXISTS model_predictions (event_id TEXT NOT NULL, market TEXT NOT NULL, selection TEXT NOT NULL, point_key TEXT NOT NULL DEFAULT '', estimated_prob DOUBLE PRECISION NOT NULL CHECK (estimated_prob>0 AND estimated_prob<1), generated_at TIMESTAMPTZ NOT NULL, model_version TEXT NOT NULL, PRIMARY KEY(event_id,market,selection,point_key));
CREATE TABLE IF NOT EXISTS verified_events (event_id TEXT PRIMARY KEY, sport TEXT NOT NULL, is_preseason BOOLEAN NOT NULL, ap_top25_team_in_match BOOLEAN, verified_at TIMESTAMPTZ NOT NULL, verification_source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prediction_context (event_id TEXT PRIMARY KEY, home_team TEXT, away_team TEXT, home_logo TEXT, away_logo TEXT, rationale TEXT, warnings TEXT, updated_at TIMESTAMPTZ NOT NULL);
'''
HEADERS={'User-Agent':'sports-edge-research/0.2'}
def norm(s): return re.sub(r'[^a-z0-9]+','',(s or '').lower())
def fetch(url,params):
    r=requests.get(url,params=params,headers=HEADERS,timeout=25);r.raise_for_status();return r.json()
def record(c):
    for r in c.get('records') or []:
        if r.get('type') in ('total','overall') or r.get('name') in ('overall','Overall'):
            m=re.match(r'^(\d+)-(\d+)(?:-\d+)?$',r.get('summary',''))
            if m:return tuple(map(int,m.groups()))
    return None
def win_prob(h,a,min_games):
    if not h or not a or sum(h)<min_games or sum(a)<min_games:return None
    hp=(h[0]+5)/(sum(h)+10);ap=(a[0]+5)/(sum(a)+10)
    z=math.log(hp/(1-hp))-math.log(ap/(1-ap))+0.12
    return max(.25,min(.75,1/(1+math.exp(-z))))
def cdf(x):return .5*(1+math.erf(x/math.sqrt(2)))
def logo(c):
    logos=(c.get('team') or {}).get('logos') or []
    u=logos[0].get('href','') if logos else ''
    return u if u.startswith('https://') and (u.split('/')[2].endswith('espn.com') or u.split('/')[2].endswith('espncdn.com')) else ''
def match_info(events):
    result={}
    for e in events:
        cs=((e.get('competitions') or [{}])[0]).get('competitors') or []
        if len(cs)!=2:continue
        h=next((x for x in cs if x.get('homeAway')=='home'),None)
        a=next((x for x in cs if x.get('homeAway')=='away'),None)
        if h and a:
            hn=(h.get('team') or {}).get('displayName');an=(a.get('team') or {}).get('displayName')
            if hn and an:result[(norm(hn),norm(an))]=(e,h,a)
    return result
def recent_scoring(sport,league,day,days=15):
    """Fetch actual completed regular-season games. Do not substitute sportsbook lines."""
    data={}
    for offset in range(1,days+1):
        date=(day-timedelta(days=offset)).strftime('%Y%m%d')
        try: board=fetch(f'https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard',{'dates':date,'limit':200})
        except (requests.RequestException,ValueError) as exc:
            logging.warning('Historical ESPN scoreboard unavailable %s: %s',date,exc);continue
        for e in board.get('events') or []:
            if (e.get('season') or {}).get('type')!=2:continue
            if not (e.get('status') or {}).get('type',{}).get('completed'):continue
            cs=((e.get('competitions') or [{}])[0]).get('competitors') or []
            if len(cs)!=2:continue
            try: scores=[float(c['score']) for c in cs]
            except (ValueError,KeyError,TypeError):continue
            for i,c in enumerate(cs):
                name=(c.get('team') or {}).get('displayName')
                if name:data.setdefault(norm(name),[]).append((scores[i],scores[1-i]))
    return {k:v[:10] for k,v in data.items()}
def save_prediction(conn,event,market,selection,point,p,now,version):
    if not 0<p<1:return False
    conn.execute('''INSERT INTO model_predictions(event_id,market,selection,point_key,estimated_prob,generated_at,model_version)
    VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id,market,selection,point_key) DO UPDATE SET
    estimated_prob=EXCLUDED.estimated_prob,generated_at=EXCLUDED.generated_at,model_version=EXCLUDED.model_version''',
    (event,market,selection,str(point) if point is not None else '',p,now,version))
    return True
def main():
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    now=datetime.now(timezone.utc);today=now.astimezone(PT).date();key=os.environ['ODDS_API_KEY']
    counts={'events':0,'matched':0,'regular':0,'moneyline_predictions':0,'spread_predictions':0,'total_predictions':0}
    with psycopg.connect(os.environ['DATABASE_URL'],row_factory=dict_row) as conn:
        conn.execute(SCHEMA)
        for sport_key,(sport,esport,league,min_games) in SPORTS.items():
            try:
                odds=fetch(f'https://api.the-odds-api.com/v4/sports/{sport_key}/odds/',{'apiKey':key,'regions':'us','markets':'h2h,spreads,totals','oddsFormat':'american'})
            except (requests.RequestException,ValueError) as exc:
                logging.warning('%s odds unavailable: %s',sport,exc);continue
            boards=[]
            for d in (-1,0,1):
                try:boards.extend(fetch(f'https://site.api.espn.com/apis/site/v2/sports/{esport}/{league}/scoreboard',{'dates':(today+timedelta(days=d)).strftime('%Y%m%d'),'limit':200}).get('events') or [])
                except (requests.RequestException,ValueError) as exc:logging.warning('ESPN schedule unavailable: %s',exc)
            by_match=match_info(boards)
            recent=recent_scoring(esport,league,today) if sport in ('NBA','NHL') else {}
            for event in odds:
                try:start=datetime.fromisoformat(event['commence_time'].replace('Z','+00:00'))
                except (KeyError,ValueError):continue
                if start<=now or start.astimezone(PT).date()!=today:continue
                counts['events']+=1
                pair=by_match.get((norm(event.get('home_team')),norm(event.get('away_team'))))
                if not pair:continue
                counts['matched']+=1
                e,h,a=pair
                if (e.get('season') or {}).get('type')!=2:continue
                counts['regular']+=1
                eid=event.get('id');hn=event['home_team'];an=event['away_team']
                if not eid:continue
                hr=record(h);ar=record(a);p=win_prob(hr,ar,min_games)
                home_games=recent.get(norm(hn),[]);away_games=recent.get(norm(an),[])
                rationale=[]
                if hr and ar:rationale.append(f'ESPN overall records: {hn} {hr[0]}-{hr[1]}; {an} {ar[0]}-{ar[1]}.')
                if p is not None:rationale.append(f'Experimental record-strength model estimates {hn} win probability at {p:.1%}; includes shrinkage and a small home adjustment.')
                if home_games and away_games:rationale.append(f'Recent completed regular-season sample: {hn} {len(home_games)} games, {an} {len(away_games)} games (up to 10).')
                warnings='Injuries, suspensions and confirmed lineups have NOT been verified. Weather/wind has NOT been verified; check game-time conditions for outdoor games. Model is experimental and uncalibrated.'
                conn.execute('''INSERT INTO verified_events(event_id,sport,is_preseason,ap_top25_team_in_match,verified_at,verification_source)
                VALUES(%s,%s,FALSE,NULL,%s,%s) ON CONFLICT(event_id) DO UPDATE SET sport=EXCLUDED.sport,is_preseason=FALSE,verified_at=EXCLUDED.verified_at,verification_source=EXCLUDED.verification_source''',
                (eid,sport,now,'ESPN scoreboard: regular season'))
                conn.execute('''INSERT INTO prediction_context(event_id,home_team,away_team,home_logo,away_logo,rationale,warnings,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id) DO UPDATE SET home_logo=EXCLUDED.home_logo,away_logo=EXCLUDED.away_logo,rationale=EXCLUDED.rationale,warnings=EXCLUDED.warnings,updated_at=EXCLUDED.updated_at''',
                (eid,hn,an,logo(h),logo(a),'\n'.join(rationale),warnings,now))
                if p is not None:
                    for team,prob in ((hn,p),(an,1-p)):
                        if save_prediction(conn,eid,'h2h',team,None,prob,now,'record-v2-experimental'):
                            counts['moneyline_predictions']+=1
                if len(home_games)<4 or len(away_games)<4:continue
                # Independent expected scores: average own points scored and opponent points allowed.
                home_mean=(statistics.mean(x[0] for x in home_games)+statistics.mean(x[1] for x in away_games))/2
                away_mean=(statistics.mean(x[0] for x in away_games)+statistics.mean(x[1] for x in home_games))/2
                # Variance estimated from observed scoring and regularized to avoid overconfidence.
                all_scores=[x[0] for x in home_games+away_games]
                sigma=max(8.0 if sport=='NBA' else 1.6,statistics.stdev(all_scores) if len(all_scores)>1 else 0)
                diff_sigma=max(11.0 if sport=='NBA' else 2.0,sigma*1.35)
                total_sigma=max(15.0 if sport=='NBA' else 2.5,sigma*1.6)
                for book in event.get('bookmakers',[]):
                    for market in book.get('markets',[]):
                        m=market.get('key')
                        if m not in ('spreads','totals'):continue
                        for o in market.get('outcomes',[]):
                            point=o.get('point');sel=o.get('name')
                            if not isinstance(point,(float,int)) or not sel:continue
                            if m=='spreads' and sel in (hn,an):
                                margin=home_mean-away_mean if sel==hn else away_mean-home_mean
                                # A discrete push possibility is not explicitly modeled; skip integer NHL lines.
                                if sport=='NHL' and float(point).is_integer():continue
                                prob=cdf((margin+point)/diff_sigma)
                            elif m=='totals' and sel in ('Over','Under'):
                                prob=1-cdf((point-(home_mean+away_mean))/total_sigma) if sel=='Over' else cdf((point-(home_mean+away_mean))/total_sigma)
                            else:continue
                            # Avoid extreme probabilities from fragile samples.
                            prob=max(.15,min(.85,prob))
                            if save_prediction(conn,eid,m,sel,point,prob,now,'recent-score-normal-v1-uncalibrated'):
                                counts['spread_predictions' if m=='spreads' else 'total_predictions']+=1
        logging.info('Prediction engine funnel: %s',counts)
        logging.warning('These are uncalibrated experimental probabilities. No props, college, verified injury or weather inputs.')
if __name__=='__main__':main()
