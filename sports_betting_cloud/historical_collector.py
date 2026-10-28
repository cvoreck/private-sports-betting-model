"""Daily append-only sports archive. Uses existing DATABASE_URL and ODDS_API_KEY.

Stores available ESPN game/boxscore data and current Odds API snapshots. Historical
player props are optional and quota-limited; the free tier may reject them.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
import requests

PT = ZoneInfo('America/Los_Angeles')
SPORTS = {
    'americanfootball_nfl': ('football', 'nfl'),
    'basketball_nba': ('basketball', 'nba'),
    'baseball_mlb': ('baseball', 'mlb'),
    'americanfootball_ncaaf': ('football', 'college-football'),
    'icehockey_nhl': ('hockey', 'nhl'),
    'basketball_ncaab': ('basketball', 'mens-college-basketball'),
}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS historical_snapshots (
 id BIGSERIAL PRIMARY KEY, collected_at TIMESTAMPTZ NOT NULL,
 source TEXT NOT NULL, sport TEXT NOT NULL, event_id TEXT NOT NULL DEFAULT '',
 category TEXT NOT NULL, item_key TEXT NOT NULL DEFAULT '',
 payload JSONB NOT NULL, UNIQUE (source, sport, event_id, category, item_key, collected_at)
);
CREATE INDEX IF NOT EXISTS historical_snapshots_lookup ON historical_snapshots(sport,event_id,category,collected_at DESC);
CREATE TABLE IF NOT EXISTS historical_collection_runs (
 run_date DATE PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL,
 completed_at TIMESTAMPTZ, status TEXT NOT NULL,
 snapshot_count INTEGER NOT NULL DEFAULT 0, detail JSONB NOT NULL DEFAULT '{}'::jsonb
);
'''
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'sports-edge-history/1.0'})


def get(url, params=None):
    response = SESSION.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response


def store(conn, collected_at, source, sport, event_id, category, item_key, payload):
    conn.execute('''INSERT INTO historical_snapshots
      (collected_at,source,sport,event_id,category,item_key,payload)
      VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING''',
      (collected_at, source, sport, str(event_id or ''), category, str(item_key or ''), json.dumps(payload)))


def archive_espn(conn, now, today, stats):
    # Backfill recent results, including previous day's finals; avoids depending on
    # the morning job running after every game's final whistle.
    backfill = max(1, min(30, int(os.getenv('ESPN_BACKFILL_DAYS', '7'))))
    for sport, (domain, league) in SPORTS.items():
        for offset in range(-1, backfill + 1):
            day = today - timedelta(days=offset)
            date_key = day.strftime('%Y%m%d')
            try:
                board = get(f'https://site.api.espn.com/apis/site/v2/sports/{domain}/{league}/scoreboard',
                            {'dates': date_key, 'limit': 300}).json()
            except (requests.RequestException, ValueError) as exc:
                logging.warning('ESPN %s %s: %s', sport, date_key, exc)
                stats['errors'].append(f'ESPN {sport} {date_key}: {exc}')
                continue
            for event in board.get('events') or []:
                eid = str(event.get('id') or '')
                if not eid:
                    continue
                # One daily observation per event; upsert is not needed because
                # timestamp is per-run and each run keeps its own snapshot.
                store(conn, now, 'espn', sport, eid, 'game', date_key, event)
                stats['games'] += 1
                completed = (event.get('status') or {}).get('type', {}).get('completed', False)
                if not completed or offset > 2 or stats['boxscores'] >= 60:
                    continue
                # ESPN summary includes boxscore player/team statistics when offered.
                try:
                    summary = get(f'https://site.api.espn.com/apis/site/v2/sports/{domain}/{league}/summary',
                                  {'event': eid}).json()
                    box = summary.get('boxscore') or {}
                    if box:
                        store(conn, now, 'espn', sport, eid, 'boxscore', date_key, box)
                        stats['boxscores'] += 1
                    # Preserve availability/injury context if present; these
                    # reports are not guaranteed to exist for every league.
                    for category in ('injuries', 'rosters', 'leaders', 'predictor'):
                        if summary.get(category):
                            store(conn, now, 'espn', sport, eid, category, date_key, summary[category])
                except (requests.RequestException, ValueError) as exc:
                    logging.warning('ESPN boxscore %s %s: %s', sport, eid, exc)
                    stats['errors'].append(f'boxscore {sport} {eid}: {exc}')
                time.sleep(0.04)
        conn.commit()


def archive_odds(conn, now, stats):
    key = os.getenv('ODDS_API_KEY')
    if not key:
        logging.warning('ODDS_API_KEY absent: archiving ESPN only')
        return
    # Free quotas are finite. We only request main markets by default.
    # Optional player props use event-specific calls and can consume much more quota.
    prop_budget = max(0, min(4, int(os.getenv('MAX_PROP_EVENT_REQUESTS', '2'))))
    min_remaining = max(30, int(os.getenv('MIN_ODDS_CREDITS_REMAINING', '100')))
    prop_markets_by_sport = {
        'basketball_nba': 'player_points,player_rebounds,player_assists',
        'americanfootball_nfl': 'player_pass_yds,player_rush_yds,player_reception_yds',
        'baseball_mlb': 'batter_hits,pitcher_strikeouts',
        'icehockey_nhl': 'player_shots_on_goal',
    }
    # Only attempt supported event-specific markets; responses can still be unavailable.
    attempted_props = set()
    for sport in SPORTS:
        try:
            response = get(f'https://api.the-odds-api.com/v4/sports/{sport}/odds/', {
                'apiKey': key, 'regions': 'us', 'markets': 'h2h,spreads,totals',
                'oddsFormat': 'american', 'dateFormat': 'iso'})
            events = response.json()
            if not isinstance(events, list):
                raise ValueError('Unexpected odds response')
            for event in events:
                eid = str(event.get('id') or '')
                if eid:
                    store(conn, now, 'the-odds-api', sport, eid, 'pregame_odds', 'h2h_spreads_totals', event)
                    stats['odds_events'] += 1
            remaining = response.headers.get('x-requests-remaining')
            logging.info('Odds %s: %d events; quota remaining %s', sport, len(events), remaining or 'unknown')
            if remaining is not None and int(remaining) <= 10:
                logging.warning('Stopping odds collection to protect remaining API quota')
                stats['errors'].append('Odds API remaining quota <=10')
                break
            if prop_budget > 0 and sport in prop_markets_by_sport and (remaining is None or int(remaining) > min_remaining):
                prop_markets = prop_markets_by_sport[sport]
                # Prioritize games starting today, and do not re-request stored props.
                local_today = now.astimezone(PT).date()
                eligible_events = sorted((e for e in events if e.get('commence_time') and
                    datetime.fromisoformat(e['commence_time'].replace('Z','+00:00')).astimezone(PT).date() == local_today and
                    datetime.fromisoformat(e['commence_time'].replace('Z','+00:00')) > now),
                    key=lambda e: e['commence_time'])
                for event in eligible_events:
                    if prop_budget <= 0:
                        break
                    eid = event.get('id')
                    if not eid:
                        continue
                    # Avoid burning credits on manual reruns on the same Pacific date.
                    existing = conn.execute("""SELECT 1 FROM historical_snapshots WHERE source='the-odds-api'
                      AND sport=%s AND event_id=%s AND category='player_prop_odds'
                      AND collected_at >= %s LIMIT 1""",
                      (sport, str(eid), datetime.combine(local_today, datetime.min.time(), tzinfo=PT))).fetchone()
                    if existing or (sport, str(eid)) in attempted_props:
                        continue
                    attempted_props.add((sport,str(eid)))
                    try:
                        r = get(f'https://api.the-odds-api.com/v4/sports/{sport}/events/{eid}/odds', {
                            'apiKey': key, 'regions': 'us', 'markets': prop_markets,
                            'oddsFormat': 'american', 'dateFormat': 'iso'})
                        prop_payload = r.json()
                        if prop_payload.get('bookmakers'):
                            store(conn, now, 'the-odds-api', sport, eid, 'player_prop_odds', prop_markets, prop_payload)
                            stats['prop_events'] += 1
                        else:
                            logging.info('No player props returned for %s %s', sport, eid)
                        prop_budget -= 1
                        remaining = r.headers.get('x-requests-remaining')
                        if remaining is not None and int(remaining) <= min_remaining:
                            prop_budget = 0
                            break
                    except (requests.RequestException, ValueError) as exc:
                        # A 401/403/422 can indicate an unavailable market or plan.
                        logging.warning('Props unavailable %s %s: %s', sport, eid, exc)
                        stats['errors'].append(f'props {sport} {eid}: {exc}')
                        prop_budget -= 1
            conn.commit()
        except (requests.RequestException, ValueError) as exc:
            logging.warning('Odds %s unavailable: %s', sport, exc)
            stats['errors'].append(f'odds {sport}: {exc}')


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    now = datetime.now(timezone.utc)
    today = now.astimezone(PT).date()
    stats = {'games': 0, 'boxscores': 0, 'odds_events': 0, 'prop_events': 0, 'errors': []}
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=20) as conn:
        conn.execute(SCHEMA)
        conn.execute('''INSERT INTO historical_collection_runs(run_date,started_at,status)
          VALUES(%s,%s,'running') ON CONFLICT(run_date) DO UPDATE SET
          started_at=EXCLUDED.started_at,status='running' ''', (today, now))
        conn.commit()
        try:
            archive_espn(conn, now, today, stats)
            archive_odds(conn, now, stats)
            conn.execute('''UPDATE historical_collection_runs SET completed_at=NOW(),status=%s,
              snapshot_count=(SELECT count(*) FROM historical_snapshots WHERE collected_at=%s),
              detail=%s::jsonb WHERE run_date=%s''',
              ('partial' if stats['errors'] else 'completed', now, json.dumps(stats), today))
            conn.commit()
        except Exception:
            logging.exception('Historical archive failed')
            conn.rollback()
            conn.execute('''UPDATE historical_collection_runs SET completed_at=NOW(),status='failed',detail=%s::jsonb WHERE run_date=%s''',
                         (json.dumps(stats), today))
            conn.commit()
            raise
    logging.info('Historical archive: %s', json.dumps(stats))


if __name__ == '__main__':
    main()
