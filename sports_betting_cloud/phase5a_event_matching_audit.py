#!/usr/bin/env python3
"""Read-only Odds API -> canonical ESPN final matching audit; no vendor requests."""
import collections
import datetime as dt
import json
import os
import re
import unicodedata
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

UTC = dt.timezone.utc
SPORTS = ('americanfootball_nfl','basketball_nba','icehockey_nhl','baseball_mlb','americanfootball_ncaaf','basketball_ncaab')
CATEGORIES = ('pregame_odds','daily_odds_cache')
# These are only conservative spelling aliases, not fuzzy similarity matching.
ALIASES = {
    'la clippers':'los angeles clippers',
    'ny knicks':'new york knicks',
    'brooklyn nets':'brooklyn nets',
    'st louis blues':'st louis blues',
    'nc state wolfpack':'north carolina state wolfpack',
    'ole miss rebels':'mississippi rebels',
    'uconn huskies':'connecticut huskies',
}

def norm(value):
    value = unicodedata.normalize('NFKD', str(value or '')).encode('ascii','ignore').decode().lower()
    value = value.replace('&',' and ')
    value = re.sub(r'[^a-z0-9]+',' ',value).strip()
    value = re.sub(r'\s+',' ',value)
    return ALIASES.get(value,value)

def parse_time(value):
    if isinstance(value,dt.datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value,str): return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError: return None

def valid_market(market, home, away):
    if not isinstance(market,dict): return False
    kind=market.get('key')
    if kind not in ('spreads','totals'): return False
    outcomes=market.get('outcomes')
    if not isinstance(outcomes,list) or len(outcomes)!=2: return False
    if not all(isinstance(o,dict) and isinstance(o.get('price'),(int,float)) and not isinstance(o.get('price'),bool) and isinstance(o.get('point'),(int,float)) and not isinstance(o.get('point'),bool) for o in outcomes): return False
    if kind=='spreads':
        by_name={norm(o.get('name')):o for o in outcomes}
        if not home or not away or home==away or set(by_name)!={home,away}: return False
        return abs(float(by_name[home]['point'])+float(by_name[away]['point']))<0.001
    return {norm(o.get('name')) for o in outcomes}=={'over','under'} and abs(float(outcomes[0]['point'])-float(outcomes[1]['point']))<0.001

def run():
    now=dt.datetime.now(UTC)
    report={'generated_at':now.isoformat(),'read_only':True,'database_writes':0,'external_api_calls':0,'paid_api_calls':0,'model_promotions':0,
            'matching_rule':'Same sport, exact normalized home and away team names, unique canonical final within 18 hours of listed start; no fuzzy matching or ID joining.',
            'pregame_rule':'Snapshot collected_at and bookmaker market last_update (when available) must both be earlier than event start; 10-minute minimum lead time for snapshot.',
            'notes':['Historical final matching is an audit only; no profitability or model validation is calculated.',
                     'An Odds API event ID is never treated as an ESPN event ID.',
                     'Multiple sportsbook observations of one game are not independent games.',
                     'A short collection history limits statistical power.'], 'sports':{}}
    with psycopg.connect(os.environ['DATABASE_URL'],row_factory=dict_row,connect_timeout=20) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        for sport in SPORTS:
            finals=conn.execute('''SELECT event_id,played_at,home_team,away_team,home_score,away_score
               FROM canonical_completed_games WHERE sport=%s AND season_type=2 AND played_at <= %s''',(sport,now)).fetchall()
            by_pair=collections.defaultdict(list)
            for g in finals:
                h,a=norm(g['home_team']),norm(g['away_team'])
                if h and a: by_pair[(h,a)].append(g)
            events={}
            counts=collections.Counter()
            with conn.cursor(name='phase5a_'+sport) as cur:
                cur.execute('''SELECT category,event_id,collected_at,payload FROM historical_snapshots
                  WHERE source='the-odds-api' AND sport=%s AND category=ANY(%s)
                  ORDER BY collected_at''',(sport,list(CATEGORIES)))
                for row in cur:
                    counts['snapshot_rows']+=1
                    payload=row['payload']
                    if not isinstance(payload,dict): counts['bad_payload_rows']+=1; continue
                    candidates=payload.get('events',[]) if row['category']=='daily_odds_cache' else [payload]
                    if not isinstance(candidates,list): counts['bad_payload_rows']+=1; continue
                    for ev in candidates:
                        if not isinstance(ev,dict): continue
                        eid=str(ev.get('id') or row['event_id'] or '').strip()
                        if not eid: counts['missing_event_id_observations']+=1; continue
                        record=events.setdefault(eid,{'meta':set(),'observations':[], 'market_valid':collections.Counter(), 'market_any':set()})
                        start=parse_time(ev.get('commence_time'))
                        home,away=norm(ev.get('home_team')),norm(ev.get('away_team'))
                        record['meta'].add((home,away,start))
                        collected=parse_time(row['collected_at'])
                        for bookmaker in ev.get('bookmakers') or []:
                            if not isinstance(bookmaker,dict): continue
                            for market in bookmaker.get('markets') or []:
                                if not isinstance(market,dict) or market.get('key') not in ('spreads','totals'): continue
                                kind=market['key']; record['market_any'].add(kind)
                                if not valid_market(market,home,away):
                                    counts['invalid_'+kind+'_bookmaker_observations']+=1; continue
                                record['market_valid'][kind]+=1
                                updated=parse_time(market.get('last_update') or bookmaker.get('last_update'))
                                valid_time=bool(start and collected and collected <= start-dt.timedelta(minutes=10) and (updated is None or updated < start))
                                record['observations'].append((kind,valid_time, bool(start and collected and collected>=start)))
            examples=[]
            for eid,record in events.items():
                counts['unique_odds_events']+=1
                if len(record['meta'])!=1:
                    reason='conflicting_event_metadata'
                else:
                    home,away,start=next(iter(record['meta']))
                    if not (home and away and start): reason='missing_team_or_start'
                    elif start>now: reason='future_event'
                    else:
                        pair_matches=by_pair.get((home,away),[])
                        matches=[g for g in pair_matches if abs((parse_time(g['played_at'])-start).total_seconds()) <= 18*3600]
                        if len(matches)==1: reason='matched_unique_final'
                        elif len(matches)>1: reason='ambiguous_multiple_finals'
                        elif pair_matches: reason='team_pair_found_but_date_mismatch'
                        else: reason='no_exact_team_pair'
                counts[reason]+=1
                if reason=='matched_unique_final':
                    for kind in ('spreads','totals'):
                        if kind in record['market_any']: counts['matched_events_with_'+kind+'_market']+=1
                        valid=[obs for obs in record['observations'] if obs[0]==kind and obs[1]]
                        if valid:
                            counts['eligible_'+kind+'_events']+=1
                            counts['eligible_'+kind+'_bookmaker_observations']+=len(valid)
                        elif kind in record['market_any']: counts['matched_'+kind+'_without_valid_pregame_price']+=1
                if reason not in ('matched_unique_final','future_event') and len(examples)<8:
                    examples.append({'odds_event_id':eid,'reason':reason,'home_team':home if len(record['meta'])==1 else None,'away_team':away if len(record['meta'])==1 else None,'commence_time':start.isoformat() if len(record['meta'])==1 and start else None})
            report['sports'][sport]={'canonical_regular_season_finals':len(finals),**dict(counts),
                'unmatched_examples':examples,
                'next_step':'Review exact-name mismatch examples; run Phase 5B only on uniquely matched games with valid pregame prices.'}
            print(sport, 'odds_events=',len(events),'matched=',counts['matched_unique_final'], 'eligible_spreads=',counts['eligible_spreads_events'],'eligible_totals=',counts['eligible_totals_events'],flush=True)
        conn.rollback()
    Path('phase5a_event_matching_report.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    print('Wrote phase5a_event_matching_report.json; no database changes')

if __name__=='__main__': run()
