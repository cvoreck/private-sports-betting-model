"""Incrementally upsert ESPN finals collected in the last 10 days.

Requires the existing compact_history.py from the initial migration.
No network calls; source archive and pregame odds are not modified.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import psycopg
from compact_history import parse_final, SCHEMA

UPSERT = """INSERT INTO canonical_completed_games
 (sport,event_id,played_at,home_team_id,away_team_id,home_team,away_team,
  home_score,away_score,season_type,source_collected_at)
 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
 ON CONFLICT (sport,event_id) DO UPDATE SET
  played_at=EXCLUDED.played_at,
  home_team_id=EXCLUDED.home_team_id,
  away_team_id=EXCLUDED.away_team_id,
  home_team=EXCLUDED.home_team,
  away_team=EXCLUDED.away_team,
  home_score=EXCLUDED.home_score,
  away_score=EXCLUDED.away_score,
  season_type=EXCLUDED.season_type,
  source_collected_at=EXCLUDED.source_collected_at
 WHERE canonical_completed_games.source_collected_at < EXCLUDED.source_collected_at
"""


def update(conn, since):
    # Latest completed observation per game; a later incomplete snapshot
    # cannot suppress a previously observed final.
    rows = conn.execute("""SELECT DISTINCT ON (sport, event_id)
        sport, event_id, payload, collected_at
        FROM historical_snapshots
        WHERE source='espn' AND category='game' AND event_id <> ''
          AND collected_at >= %s
          AND payload #>> '{status,type,completed}' = 'true'
        ORDER BY sport, event_id, collected_at DESC, id DESC
    """, (since,)).fetchall()
    valid = skipped = 0
    for sport, event_id, payload, collected_at in rows:
        item = parse_final(payload, sport, event_id, collected_at)
        if item is None:
            skipped += 1
            continue
        conn.execute(UPSERT, item)
        valid += 1
    logging.info('Compact incremental update: eligible=%d malformed=%d window_start=%s',
                 valid, skipped, since.isoformat())
    return valid, skipped


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    days = int(os.getenv('COMPACT_LOOKBACK_DAYS', '10'))
    if not 1 <= days <= 30:
        raise ValueError('COMPACT_LOOKBACK_DAYS must be between 1 and 30')
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=20) as conn:
        conn.execute(SCHEMA)
        update(conn, since)
        conn.commit()


if __name__ == '__main__':
    main()
