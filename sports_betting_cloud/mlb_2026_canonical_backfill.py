"""One-time, resumable ESPN 2026 MLB scoreboard -> canonical_completed_games import.

No Odds API requests. No historical_snapshots writes. No boxscore requests.
Run with --dry-run --max-days 3 to test; --execute --max-days 120 to import.
"""
import argparse
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

import psycopg
import requests

SPORT = 'baseball_mlb'
START = date(2026, 3, 1)
END = date(2026, 11, 15)
URL = 'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard'


def parse_final(event):
    if not isinstance(event, dict):
        return None
    season = event.get('season') or {}
    try:
        if int(season.get('type', -1)) != 2:
            return None
        if not ((event.get('status') or {}).get('type') or {}).get('completed'):
            return None
        competition = (event.get('competitions') or [])[0]
        competitors = competition.get('competitors') or []
        home = next(c for c in competitors if c.get('homeAway') == 'home')
        away = next(c for c in competitors if c.get('homeAway') == 'away')
        dt = datetime.fromisoformat(event['date'].replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return None
        ht, at = home['team'], away['team']
        eid = str(event['id'])
        hid, aid = str(ht['id']), str(at['id'])
        hn, an = str(ht['displayName']), str(at['displayName'])
        if not all((eid, hid, aid, hn, an)):
            return None
        return (SPORT, eid, dt, hid, aid, hn, an,
                float(home['score']), float(away['score']), 2, datetime.now(timezone.utc))
    except (KeyError, ValueError, TypeError, IndexError, StopIteration, AttributeError):
        return None


def fetch(session, day):
    for attempt in range(3):
        try:
            response = session.get(URL, params={'dates': day.strftime('%Y%m%d'), 'limit': 300}, timeout=30)
            response.raise_for_status()
            data = response.json()
            events = data.get('events')
            if not isinstance(events, list):
                raise ValueError('ESPN scoreboard returned no valid events list')
            return events
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
            time.sleep(3 * (2 ** attempt))


INSERT = '''INSERT INTO canonical_completed_games
 (sport,event_id,played_at,home_team_id,away_team_id,home_team,away_team,
  home_score,away_score,season_type,source_collected_at)
 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
 ON CONFLICT (sport,event_id) DO NOTHING'''

PROGRESS = '''CREATE TABLE IF NOT EXISTS mlb_2026_canonical_import_progress (
 day DATE PRIMARY KEY, completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 events_seen INTEGER NOT NULL, regular_finals INTEGER NOT NULL, games_inserted INTEGER NOT NULL
)'''


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--dry-run', action='store_true', help='Fetch ESPN but do not write anything')
    group.add_argument('--execute', action='store_true', help='Write only canonical game rows and progress')
    parser.add_argument('--max-days', type=int, default=120)
    args = parser.parse_args()
    if not 1 <= args.max_days <= 300:
        parser.error('--max-days must be between 1 and 300')
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    today = datetime.now(timezone.utc).date()
    end = min(END, today - timedelta(days=1))
    if end < START:
        logging.info('No completed dates yet')
        return
    session = requests.Session()
    session.headers.update({'User-Agent': 'sports-edge-mlb-canonical-recovery/1.0'})
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=20) as conn:
        if args.execute:
            conn.execute(PROGRESS)
            conn.commit()
        else:
            # Read-only session for the dry run, including no progress-table creation.
            conn.execute('SET TRANSACTION READ ONLY')
        if args.execute:
            done = {row[0] for row in conn.execute('SELECT day FROM mlb_2026_canonical_import_progress')}
        else:
            done = set()
        checked = inserted = finals = 0
        day = START
        while day <= end and checked < args.max_days:
            if day in done:
                day += timedelta(days=1)
                continue
            events = fetch(session, day)
            parsed = [item for event in events if (item := parse_final(event)) is not None]
            if args.execute:
                with conn.transaction():
                    count = 0
                    for item in parsed:
                        count += conn.execute(INSERT, item).rowcount
                    conn.execute('''INSERT INTO mlb_2026_canonical_import_progress
                      (day,events_seen,regular_finals,games_inserted) VALUES (%s,%s,%s,%s)
                      ON CONFLICT (day) DO NOTHING''', (day, len(events), len(parsed), count))
                conn.commit()  # Persist each day so interrupted runs resume safely.
                inserted += count
            checked += 1
            finals += len(parsed)
            logging.info('%s ESPN events=%d regular_finals=%d newly_inserted=%d',
                         day, len(events), len(parsed), count if args.execute else 0)
            day += timedelta(days=1)
            time.sleep(0.2)
        if args.dry_run:
            conn.rollback()
        logging.info('SUMMARY mode=%s days_checked=%d regular_finals=%d new_canonical_rows=%d odds_api_calls=0 snapshot_writes=0',
                     'execute' if args.execute else 'dry-run', checked, finals, inserted)
        if args.execute:
            remaining = conn.execute('''SELECT COUNT(*) FROM generate_series(%s::date,%s::date,'1 day'::interval) AS d(day)
              WHERE NOT EXISTS (SELECT 1 FROM mlb_2026_canonical_import_progress p WHERE p.day=d.day::date)''',
              (START, end)).fetchone()[0]
            logging.info('Days remaining to import=%d', remaining)


if __name__ == '__main__':
    main()
