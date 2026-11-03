"""Read-only pregame market archive coverage audit. No external API requests.

Usage: DATABASE_URL=... python sports_betting_cloud/market_data_readiness.py
Creates sports_edge_market_readiness.json in current working directory.
"""
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

MARKETS = {'h2h': 'moneyline', 'spreads': 'spread', 'totals': 'total'}


def audit(conn):
    rows = conn.execute("""SELECT sport,event_id,collected_at,payload
      FROM historical_snapshots WHERE source='the-odds-api'
      AND category='pregame_odds' ORDER BY sport,event_id,collected_at""").fetchall()
    groups = defaultdict(list)
    for row in rows:
        payload = row['payload']
        if isinstance(payload, str):
            payload = json.loads(payload)
        groups[(row['sport'],row['event_id'])].append((row['collected_at'],payload))
    stats = defaultdict(lambda: {'events_archived':0,'events_with_pregame_snapshot':0,
        'events_without_confirmed_pregame_snapshot':0,'market_event_counts':Counter(),
        'market_quote_counts':Counter(),'pregame_snapshots':0,
        'latest_pregame_snapshot_at':None})
    for (sport,event_id), snapshots in groups.items():
        s = stats[sport]
        s['events_archived'] += 1
        seen = set()
        pregame = False
        for collected, payload in snapshots:
            start_text = payload.get('commence_time')
            if not start_text:
                continue
            try:
                start = datetime.fromisoformat(start_text.replace('Z','+00:00'))
            except (ValueError,TypeError):
                continue
            if start.tzinfo is None or collected.tzinfo is None:
                continue
            # Strict: only archive quotes captured before scheduled game time.
            if collected >= start:
                continue
            pregame = True
            s['pregame_snapshots'] += 1
            when = collected.astimezone(timezone.utc).isoformat()
            if not s['latest_pregame_snapshot_at'] or when > s['latest_pregame_snapshot_at']:
                s['latest_pregame_snapshot_at'] = when
            for bookmaker in payload.get('bookmakers',[]):
                for market in bookmaker.get('markets',[]):
                    key = market.get('key')
                    if key not in MARKETS:
                        continue
                    label = MARKETS[key]
                    outcomes = [o for o in market.get('outcomes',[]) if isinstance(o.get('price'),(int,float))]
                    if outcomes:
                        seen.add(label)
                        s['market_quote_counts'][label] += len(outcomes)
        if pregame:
            s['events_with_pregame_snapshot'] += 1
        else:
            s['events_without_confirmed_pregame_snapshot'] += 1
        for market in seen:
            s['market_event_counts'][market] += 1
    return {'generated_at':datetime.now(timezone.utc).isoformat(),
      'audit':'archived pregame odds coverage; not a betting ROI backtest',
      'rules':['Pregame requires collected_at < commence_time',
               'One event counted once per market even with multiple snapshots',
               'No event outcome joins, no CLV, no ROI, no model evaluation',
               'Archived odds are not necessarily the exact prices available at selection time'],
      'sports':{sport:dict(s) for sport,s in sorted(stats.items())},
      'total_unique_events':len(groups),
      'limitations':['No match to predictions or final scores yet',
                     'No sportsbook baseline or calibration promotion decision',
                     'A missing archive does not imply no sportsbook market existed']}


def main():
    url = os.getenv('DATABASE_URL')
    if not url:
        raise SystemExit('DATABASE_URL is required; no database writes are performed')
    with psycopg.connect(url, row_factory=dict_row) as conn:
        report = audit(conn)
    target = Path('sports_edge_market_readiness.json')
    target.write_text(json.dumps(report,indent=2,default=str)+'\n',encoding='utf-8')
    print(f'Wrote {target}; {report["total_unique_events"]} unique archived odds events')
    for sport,s in report['sports'].items():
        print(sport, 'pregame',s['events_with_pregame_snapshot'], '/',s['events_archived'],
              'markets',dict(s['market_event_counts']))


if __name__=='__main__':
    main()
