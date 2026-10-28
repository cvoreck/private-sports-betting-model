"""Experimental sports model using archived results and market-aware shrinkage.
ESPN scoreboard regular-season verification and W/L record model for NFL/NBA/NHL.
NBA/NHL spread and total models require >=4 recent final games per team.
Research only: not calibrated or backtested; missing data means no prediction.
"""
import logging, math, os, re, statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests, psycopg
from matchup_research import completed_games, research
from weekly_trainer import MARGIN_SCALE, features as trained_features, sigmoid as trained_sigmoid, parse_game
from psycopg.rows import dict_row
from ap_rankings_verifier import verify_college_events

PT=ZoneInfo('America/Los_Angeles')
SPORTS={'basketball_nba':('NBA','basketball','nba',6), 'icehockey_nhl':('NHL','hockey','nhl',6), 'americanfootball_nfl':('NFL','football','nfl',4), 'baseball_mlb':('MLB','baseball','mlb',4), 'americanfootball_ncaaf':('College Football','football','college-football',4), 'basketball_ncaab':('College Basketball','basketball','mens-college-basketball',4)}
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

def archived_completed_games(conn, sport_key, before, max_games=30):
    """Read deduplicated, completed regular-season games saved before prediction day.

    ESPN scoreboard game payloads have scores and final status. Exclude today's
    games to avoid lookahead. This uses game results, not player-prop prices.
    """
    data, seen = {}, set()
    if not conn.execute("SELECT to_regclass('public.historical_snapshots') AS tbl").fetchone()['tbl']:
        return {}
    try:
        rows = conn.execute("""SELECT event_id,payload FROM historical_snapshots
          WHERE source='espn' AND sport=%s AND category='game'
          ORDER BY collected_at DESC LIMIT 30000""",
          (sport_key,)).fetchall()
    except psycopg.errors.UndefinedTable:
        conn.rollback()
        return {}
    for row in rows:
        eid = row['event_id']
        if eid in seen: continue
        seen.add(eid)
        e = row['payload']
        if (e.get('season') or {}).get('type') != 2: continue
        if not ((e.get('status') or {}).get('type') or {}).get('completed'): continue
        try:
            played = datetime.fromisoformat(e['date'].replace('Z','+00:00')).astimezone(PT).date()
            if played >= before: continue
            cs = (e.get('competitions') or [{}])[0].get('competitors') or []
            if len(cs) != 2: continue
            scores = [float(c['score']) for c in cs]
        except (ValueError, TypeError, KeyError, IndexError): continue
        for i, c in enumerate(cs):
            name = (c.get('team') or {}).get('displayName')
            if name:
                # Match matchup_research.completed_games() record schema exactly.
                # Keep timezone-aware datetime for rest calculations, and derive
                # actual win/home values from the ESPN box score and competitors.
                data.setdefault(norm(name), []).append({
                    'scored': scores[i], 'allowed': scores[1-i],
                    'win': scores[i] > scores[1-i],
                    'home': c.get('homeAway') == 'home',
                    'game_id': eid,
                    'date': datetime.fromisoformat(e['date'].replace('Z', '+00:00')),
                })
    for key in data:
        data[key].sort(key=lambda x:x['date'], reverse=True)
        data[key] = data[key][:max_games]
    return data

def merge_games(live, archived, limit=30):
    """Prefer archive, then fill missing history with ESPN live results."""
    merged = {k:list(v) for k,v in archived.items()}
    for team, games in live.items():
        if len(merged.get(team, [])) >= 4:
            continue  # Avoid double counting games also returned by live ESPN.
        existing = merged.setdefault(team, [])
        ids = {g.get('game_id') for g in existing if g.get('game_id')}
        for game in games:
            gid = game.get('game_id') or game.get('id')
            if gid and gid in ids: continue
            if gid: ids.add(gid)
            existing.append(game)
        merged[team] = existing[:limit]
    return merged

def implied(odds):
    if not isinstance(odds,(float,int)) or odds == 0: return None
    return 100/(100+odds) if odds > 0 else -odds/(100-odds)

def fair_market_probability(market, outcome):
    """Remove proportional vig for two-sided markets at the same sportsbook."""
    outcomes = market.get('outcomes') or []
    if len(outcomes) != 2: return None
    raw = [implied(x.get('price')) for x in outcomes]
    if any(x is None for x in raw): return None
    total = sum(raw)
    if total <= 0: return None
    for x, p in zip(outcomes, raw):
        if x.get('name') == outcome.get('name') and x.get('point') == outcome.get('point'):
            return p/total
    return None

def conservative_probability(raw, market, outcome, sample_size):
    baseline = fair_market_probability(market, outcome)
    if baseline is None: return None
    # Explicit heuristic, NOT trained or calibrated: 4 games/team -> 10% weight;
    # 20 games/team -> 30% weight. Protect against tiny-sample false certainty.
    weight = min(0.30, max(0.10, 0.10 + 0.0125 * max(0, sample_size-4)))
    return max(0.02, min(0.98, baseline + weight*(raw-baseline)))

def save_prediction(conn,event,market,selection,point,p,now,version):
    if not 0<p<1:return False
    conn.execute('''INSERT INTO model_predictions(event_id,market,selection,point_key,estimated_prob,generated_at,model_version)
    VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id,market,selection,point_key) DO UPDATE SET
    estimated_prob=EXCLUDED.estimated_prob,generated_at=EXCLUDED.generated_at,model_version=EXCLUDED.model_version''',
    (event,market,selection,str(point) if point is not None else '',p,now,version))
    return True

def cached_odds_events(conn, sport, today):
    # Read cached daily odds; never call paid API.
    row=conn.execute('''SELECT payload FROM historical_snapshots
       WHERE source='the-odds-api' AND sport=%s AND category='daily_odds_cache'
       AND item_key=%s ORDER BY collected_at DESC LIMIT 1''',
       (sport,today.isoformat())).fetchone()
    if not row: return []
    payload=row['payload'] if isinstance(row,dict) else row[0]
    return payload.get('events',[]) if isinstance(payload,dict) else []


def load_promoted_models(conn):
    """Only use models promoted by the weekly chronological evaluation."""
    rows = conn.execute("SELECT sport, version, coefficients FROM sports_edge_trained_models").fetchall()
    models = {}
    for row in rows:
        c = row['coefficients'] or {}
        w = c.get('weights')
        if (row['sport'] in MARGIN_SCALE and isinstance(w, list) and len(w) == 3
                and all(isinstance(v, (int, float)) and math.isfinite(v) for v in w)
                and c.get('margin_scale') == MARGIN_SCALE[row['sport']]
                and c.get('features') == ['intercept', 'win_rate_diff_last10', 'margin_diff_last10_scaled']):
            models[row['sport']] = (row['version'], w)
        else:
            logging.warning('Skipping incompatible trained model: %s (version=%s, margin_scale=%r, features=%r)',
                            row['sport'], row['version'], c.get('margin_scale'), c.get('features'))
    return models


def archived_training_history(conn, sport, cutoff):
    """Rebuild exact trainer feature history using games before the matchup."""
    rows = conn.execute("""SELECT event_id,payload FROM historical_snapshots
       WHERE source='espn' AND sport=%s AND category='game'
       ORDER BY collected_at DESC LIMIT 30000""", (sport,)).fetchall()
    games = {}
    for row in rows:
        if not isinstance(row['payload'], dict):
            continue
        parsed = parse_game(row['payload'], sport)
        if parsed and parsed[0] < cutoff and parsed[1] and parsed[1] not in games:
            games[parsed[1]] = parsed
    history = {}
    for dt, eid, h, a, hs, aa in sorted(games.values(), key=lambda x: (x[0], x[1])):
        history.setdefault(h, []).append((int(hs > aa), hs-aa))
        history.setdefault(a, []).append((int(aa > hs), aa-hs))
    return history


def trained_home_probability(event, history, sport, weights):
    cs = (event.get('competitions') or [{}])[0].get('competitors') or []
    home = next((x for x in cs if x.get('homeAway') == 'home'), None)
    away = next((x for x in cs if x.get('homeAway') == 'away'), None)
    if not home or not away:
        return None
    ht, at = home.get('team') or {}, away.get('team') or {}
    hid = str(ht.get('id') or norm(ht.get('displayName')))
    aid = str(at.get('id') or norm(at.get('displayName')))
    x = trained_features(hid, aid, history, sport)
    return trained_sigmoid(sum(a*b for a,b in zip(weights, x))) if x else None

def main():
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    now=datetime.now(timezone.utc);today=now.astimezone(PT).date()
    counts={'events':0,'matched':0,'regular':0,'moneyline_predictions':0,'spread_predictions':0,'total_predictions':0,'trained_moneyline_events':0,'no_trained_model':0,'insufficient_team_history':0,'college_events_awaiting_ap_verification':0,'trained_noncollege_events':0}
    with psycopg.connect(os.environ['DATABASE_URL'],row_factory=dict_row) as conn:
        conn.execute(SCHEMA)
        models=load_promoted_models(conn)
        logging.info('Promoted models available: %s', sorted(models))
        college_odds = {key: cached_odds_events(conn, key, today) for key in ('americanfootball_ncaaf', 'basketball_ncaab')}
        verify_college_events(conn, now, today, college_odds, fetch_json=fetch)
        for sport_key,(sport,esport,league,min_games) in SPORTS.items():
            try:
                odds=college_odds[sport_key] if sport_key in college_odds else cached_odds_events(conn,sport_key,today)
            except (requests.RequestException,ValueError) as exc:
                logging.warning('%s cached odds unavailable: %s',sport,exc);continue
            boards=[]
            for d in (-1,0,1):
                try:boards.extend(fetch(f'https://site.api.espn.com/apis/site/v2/sports/{esport}/{league}/scoreboard',{'dates':(today+timedelta(days=d)).strftime('%Y%m%d'),'limit':200}).get('events') or [])
                except (requests.RequestException,ValueError) as exc:logging.warning('ESPN schedule unavailable: %s',exc)
            by_match=match_info(boards)
            live_recent=completed_games(esport,league,today,lookback=24 if sport in ('NBA','NHL','MLB','College Basketball') else 80)
            archived=archived_completed_games(conn,sport_key,today) 
            trained_model=models.get(sport_key)
            trained_history=archived_training_history(conn,sport_key,now) if trained_model else {}
            recent=merge_games(live_recent,archived)
            logging.info('%s archive team histories=%d; live team histories=%d',sport,len(archived),len(live_recent))
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
                hr=record(h);ar=record(a)
                # Use each event's scheduled start as the feature cutoff, never future results.
                if trained_model:
                    p=trained_home_probability(e,trained_history,sport_key,trained_model[1])
                else:
                    p=None
                if not trained_model: counts['no_trained_model']+=1
                elif p is None: counts['insufficient_team_history']+=1
                else:
                    counts['trained_moneyline_events']+=1
                    if not sport.startswith('College'):
                        counts['trained_noncollege_events']+=1
                home_games=recent.get(norm(hn),[]);away_games=recent.get(norm(an),[])
                rationale, warnings_list = research(e, sport, hn, an, recent, start, p) if sport in ('NBA','NHL','NFL') else ([], ['College AP Top 25 eligibility requires independent verification.'] if sport.startswith('College') else [])
                if hr and ar:
                    rationale.insert(0, f'ESPN overall records: {hn} {hr[0]}-{hr[1]}; {an} {ar[0]}-{ar[1]}.')
                warnings = '\n'.join(warnings_list)
                if sport.startswith('College'):
                    # Require pre-existing independent AP Top 25 verification.
                    verified = conn.execute("SELECT ap_top25_team_in_match, is_preseason FROM verified_events WHERE event_id=%s AND sport=%s AND verified_at >= %s", (eid, sport, now-timedelta(days=7))).fetchone()
                    if not verified or verified['ap_top25_team_in_match'] is not True or verified['is_preseason']:
                        counts['college_events_awaiting_ap_verification']+=1
                        continue
                else:
                    conn.execute('''INSERT INTO verified_events(event_id,sport,is_preseason,ap_top25_team_in_match,verified_at,verification_source)
                VALUES(%s,%s,FALSE,NULL,%s,%s) ON CONFLICT(event_id) DO UPDATE SET sport=EXCLUDED.sport,is_preseason=FALSE,verified_at=EXCLUDED.verified_at,verification_source=EXCLUDED.verification_source''',
                (eid,sport,now,'ESPN scoreboard: regular season'))
                conn.execute('''INSERT INTO prediction_context(event_id,home_team,away_team,home_logo,away_logo,rationale,warnings,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id) DO UPDATE SET home_logo=EXCLUDED.home_logo,away_logo=EXCLUDED.away_logo,rationale=EXCLUDED.rationale,warnings=EXCLUDED.warnings,updated_at=EXCLUDED.updated_at''',
                (eid,hn,an,logo(h),logo(a),'\n'.join(rationale),warnings,now))
                if p is not None:
                    for team,prob in ((hn,p),(an,1-p)):
                        if save_prediction(conn,eid,'h2h',team,None,prob,now,trained_model[0]):
                            counts['moneyline_predictions']+=1
                if sport not in ('NBA','NHL','NFL') or len(home_games)<4 or len(away_games)<4:continue
                # Independent expected scores: average own points scored and opponent points allowed.
                home_mean=(statistics.mean(x['scored'] for x in home_games)+statistics.mean(x['allowed'] for x in away_games))/2
                away_mean=(statistics.mean(x['scored'] for x in away_games)+statistics.mean(x['allowed'] for x in home_games))/2
                # Variance estimated from observed scoring and regularized to avoid overconfidence.
                all_scores=[x['scored'] for x in home_games+away_games]
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
                            prob=conservative_probability(prob,market,o,min(len(home_games),len(away_games)))
                            if prob is None:continue
                            if save_prediction(conn,eid,m,sel,point,prob,now,'archive-market-shrink-v1-experimental'):
                                counts['spread_predictions' if m=='spreads' else 'total_predictions']+=1
        logging.info('Prediction engine funnel: %s',counts)
        logging.warning('Moneylines use promoted trained models only. Spreads/totals remain experimental heuristics. College predictions require pre-existing independently verified AP Top 25 eligibility; no props or injury model.')
if __name__=='__main__':main()
