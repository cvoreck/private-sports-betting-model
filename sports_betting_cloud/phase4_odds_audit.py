#!/usr/bin/env python3
"""Read-only inventory of existing Odds API snapshots. Never fetches vendor APIs."""
import collections
import datetime as dt
import json
import os
from pathlib import Path
import psycopg

SPORTS = ('americanfootball_nfl','basketball_nba','icehockey_nhl','baseball_mlb','americanfootball_ncaaf','basketball_ncaab')
CATEGORIES = ('pregame_odds','daily_odds_cache')
UTC = dt.timezone.utc

def parse_time(s):
    if not s or not isinstance(s,str): return None
    try: return dt.datetime.fromisoformat(s.replace('Z','+00:00')).astimezone(UTC)
    except (ValueError,TypeError): return None

def summarize():
    report = {'generated_at':dt.datetime.now(UTC).isoformat(), 'read_only':True,'database_writes':0,'external_api_calls':0,'paid_api_calls':0,'model_promotions':0,
      'purpose':'Existing historical sportsbook spread/total coverage inventory only',
      'notes':['Snapshot event IDs are Odds API IDs, not necessarily ESPN/canonical IDs; direct ID matching must not be assumed.',
               'An odds snapshot is usable for a historical backtest only if it predates event commencement and is linked reliably to a verified final.',
               'Counts are coverage metrics, not model performance, EV or ROI. No snapshots are modified.'], 'sports':{}}
    with psycopg.connect(os.environ['DATABASE_URL']) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        for sport in SPORTS:
            counts=collections.Counter(); books=collections.Counter(); markets=collections.Counter(); event_markets=collections.defaultdict(set)
            unique_events=set(); event_dates=[]; snapshot_dates=[]; per_category=collections.Counter()
            pregame=collections.Counter(); valid_price=collections.Counter(); bookmaker_events=collections.defaultdict(set)
            sample=[]; rows_seen=0; bad_payloads=0
            # Use named cursor to bound memory. The server cursor remains inside the read-only transaction.
            with conn.cursor(name='phase4_audit_'+sport.replace('-','_')) as cur:
                cur.execute('''SELECT category, event_id, collected_at, payload FROM historical_snapshots
                    WHERE source = 'the-odds-api' AND sport = %s AND category = ANY(%s)
                    ORDER BY collected_at''',(sport,list(CATEGORIES)))
                for category,event_id,collected_at,payload in cur:
                    rows_seen+=1; per_category[category]+=1
                    if collected_at: snapshot_dates.append(collected_at)
                    if not isinstance(payload,dict): bad_payloads+=1; continue
                    events = payload.get('events',[]) if category=='daily_odds_cache' else [payload]
                    if not isinstance(events,list): bad_payloads+=1; continue
                    for event in events:
                        if not isinstance(event,dict): continue
                        eid=str(event.get('id') or event_id or '')
                        if not eid: continue
                        unique_events.add(eid)
                        commence=parse_time(event.get('commence_time'))
                        if commence: event_dates.append(commence)
                        if commence and collected_at:
                            when='pregame' if collected_at < commence else 'at_or_after_start'
                        else: when='unknown_time'
                        pregame[when]+=1
                        for bookmaker in event.get('bookmakers') or []:
                            if not isinstance(bookmaker,dict): continue
                            book=str(bookmaker.get('key') or 'unknown')
                            books[book]+=1
                            for market in bookmaker.get('markets') or []:
                                if not isinstance(market,dict): continue
                                mkey=market.get('key')
                                if mkey not in ('h2h','spreads','totals'): continue
                                markets[mkey]+=1
                                event_markets[eid].add(mkey)
                                bookmaker_events[book].add(eid)
                                outcomes=market.get('outcomes') or []
                                if isinstance(outcomes,list) and len(outcomes)>=2 and all(isinstance(o,dict) and isinstance(o.get('price'),(int,float)) and (mkey=='h2h' or isinstance(o.get('point'),(int,float))) for o in outcomes[:2]):
                                    valid_price[mkey]+=1
                        if len(sample)<3 and category=='pregame_odds':
                            sample.append({'event_id':eid,'commence_time':event.get('commence_time'),'collected_at':collected_at.isoformat() if collected_at else None,
                                           'home_team':event.get('home_team'),'away_team':event.get('away_team'), 'markets':sorted(event_markets[eid])})
            report['sports'][sport]={
                'snapshot_rows':rows_seen,'rows_by_category':dict(per_category),'invalid_payload_rows':bad_payloads,
                'unique_odds_api_event_ids':len(unique_events),
                'unique_events_with_market':{k:sum(k in v for v in event_markets.values()) for k in ('h2h','spreads','totals')},
                'bookmaker_market_observations':dict(markets),'observations_with_two_priced_outcomes':dict(valid_price),
                'event_observations_by_capture_time':dict(pregame),
                'first_snapshot':min(snapshot_dates).isoformat() if snapshot_dates else None,
                'last_snapshot':max(snapshot_dates).isoformat() if snapshot_dates else None,
                'earliest_event':min(event_dates).isoformat() if event_dates else None,
                'latest_event':max(event_dates).isoformat() if event_dates else None,
                'top_bookmakers_by_observations':books.most_common(12),
                'sample_event_metadata':sample,
                'next_requirement':'Crosswalk Odds API event/team identifiers to verified final scores using dates and team-name normalization; do not join by event ID alone.'}
            print(f"{sport}: rows={rows_seen}, events={len(unique_events)}, spreads={report['sports'][sport]['unique_events_with_market']['spreads']}, totals={report['sports'][sport]['unique_events_with_market']['totals']}",flush=True)
    Path('phase4_odds_audit_report.json').write_text(json.dumps(report,indent=2,default=str)+'\n')
    print('Wrote phase4_odds_audit_report.json (read-only)')
if __name__=='__main__': summarize()
