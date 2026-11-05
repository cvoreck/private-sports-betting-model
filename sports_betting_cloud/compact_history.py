"""Backfill canonical final games from existing ESPN snapshots; no network calls.

Run: python sports_betting_cloud/compact_history.py --dry-run
Then: python sports_betting_cloud/compact_history.py
"""
import argparse
import logging
import os
from datetime import datetime, timezone

import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_completed_games (
 sport TEXT NOT NULL,
 event_id TEXT NOT NULL,
 played_at TIMESTAMPTZ NOT NULL,
 home_team_id TEXT NOT NULL,
 away_team_id TEXT NOT NULL,
 home_team TEXT NOT NULL,
 away_team TEXT NOT NULL,
 home_score DOUBLE PRECISION NOT NULL,
 away_score DOUBLE PRECISION NOT NULL,
 season_type INTEGER,
 source_collected_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY (sport, event_id)
);
CREATE INDEX IF NOT EXISTS canonical_games_sport_date
 ON canonical_completed_games (sport, played_at DESC);
"""


def parse_final(payload, sport, event_id, collected_at):
    if not isinstance(payload, dict):
        return None
    if not ((payload.get('status') or {}).get('type') or {}).get('completed'):
        return None
    comps = payload.get('competitions') or []
    if not comps:
        return None
    competitors = comps[0].get('competitors') or []
    home = next((x for x in competitors if x.get('homeAway') == 'home'), None)
    away = next((x for x in competitors if x.get('homeAway') == 'away'), None)
    if not home or not away:
        return None
    try:
        played_at = datetime.fromisoformat(payload['date'].replace('Z', '+00:00'))
        if played_at.tzinfo is None:
            return None
        hs, as_ = float(home['score']), float(away['score'])
        ht, at = home['team'], away['team']
        hid, aid = str(ht['id']), str(at['id'])
        hn, an = ht['displayName'], at['displayName']
        season_type = (payload.get('season') or {}).get('type')
        if season_type is not None:
            season_type = int(season_type)
        if not (hid and aid and hn and an):
            return None
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return None
    return (sport, event_id, played_at, hid, aid, hn, an, hs, as_, season_type, collected_at)


def migrate(conn, dry_run=False, batch_size=500):
    if not dry_run:
        conn.execute(SCHEMA)
        conn.commit()
    # DISTINCT ON is server-side: transfer only the latest FINAL snapshot per event.
    # A later non-final snapshot must not hide an earlier final observation.
    sql = """SELECT DISTINCT ON (sport, event_id)
               sport, event_id, payload, collected_at
             FROM historical_snapshots
             WHERE source='espn' AND category='game' AND event_id <> ''
               AND payload #>> '{status,type,completed}' = 'true'
             ORDER BY sport, event_id, collected_at DESC, id DESC"""
    count = skipped = 0
    with conn.cursor(name='canonical_backfill_cursor') as cursor:
        cursor.execute(sql)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for sport, eid, payload, collected_at in rows:
                item = parse_final(payload, sport, eid, collected_at)
                if not item:
                    skipped += 1
                    continue
                count += 1
                if not dry_run:
                    # Write via separate connection: named cursor needs its read transaction.
                    yield item
    logging.info('Eligible canonical finals=%s, skipped malformed=%s', count, skipped)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=20) as reader:
        if args.dry_run:
            sum(1 for _ in migrate(reader, dry_run=True))
            return
        with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=20) as writer:
            writer.execute(SCHEMA)
            writer.commit()
            count = 0
            for item in migrate(reader):
                writer.execute('''INSERT INTO canonical_completed_games
                  (sport,event_id,played_at,home_team_id,away_team_id,home_team,away_team,
                   home_score,away_score,season_type,source_collected_at)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT (sport,event_id) DO UPDATE SET
                   played_at=EXCLUDED.played_at,home_team_id=EXCLUDED.home_team_id,
                   away_team_id=EXCLUDED.away_team_id,home_team=EXCLUDED.home_team,
                   away_team=EXCLUDED.away_team,home_score=EXCLUDED.home_score,
                   away_score=EXCLUDED.away_score,season_type=EXCLUDED.season_type,
                   source_collected_at=EXCLUDED.source_collected_at
                  WHERE canonical_completed_games.source_collected_at < EXCLUDED.source_collected_at''', item)
                count += 1
                if count % 500 == 0:
                    writer.commit()
            writer.commit()
            logging.info('Processed %s canonical completed games', count)


if __name__ == '__main__':
    main()
