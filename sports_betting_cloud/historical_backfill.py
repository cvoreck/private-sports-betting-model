"""Resumable ESPN-only historical backfill for Sports Edge.

Five starting seasons (2021-2025) for all six Sports Edge leagues by default.
Uses DATABASE_URL only. No Odds API calls, no paid odds credits.
"""
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

import psycopg
import requests
from psycopg.rows import dict_row

SPORTS = {
    'basketball_nba': ('basketball', 'nba'),
    'icehockey_nhl': ('hockey', 'nhl'),
    'americanfootball_nfl': ('football', 'nfl'),
    'baseball_mlb': ('baseball', 'mlb'),
    'americanfootball_ncaaf': ('football', 'college-football'),
    'basketball_ncaab': ('basketball', 'mens-college-basketball'),
}
START_SEASONS = list(range(2021, 2026))
SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_snapshots (
 id BIGSERIAL PRIMARY KEY, collected_at TIMESTAMPTZ NOT NULL,
 source TEXT NOT NULL, sport TEXT NOT NULL, event_id TEXT NOT NULL DEFAULT '',
 category TEXT NOT NULL, item_key TEXT NOT NULL DEFAULT '', payload JSONB NOT NULL,
 UNIQUE (source,sport,event_id,category,item_key,collected_at));
CREATE INDEX IF NOT EXISTS historical_snapshots_lookup
 ON historical_snapshots(sport,event_id,category,collected_at DESC);
CREATE TABLE IF NOT EXISTS sports_edge_backfill_progress (
 sport TEXT NOT NULL, season_start INTEGER NOT NULL,
 next_date DATE NOT NULL, finished BOOLEAN NOT NULL DEFAULT FALSE,
 days_checked INTEGER NOT NULL DEFAULT 0, games_saved INTEGER NOT NULL DEFAULT 0,
 last_error TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY (sport,season_start));
CREATE TABLE IF NOT EXISTS sports_edge_backfill_boxscore_failures (
 sport TEXT NOT NULL, event_id TEXT NOT NULL, reason TEXT,
 attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY (sport,event_id));
"""
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'sports-edge-backfill/1.0 (research archival)'})


def season_window(sport, year):
    if sport == 'americanfootball_nfl':
        return date(year, 8, 1), date(year + 1, 2, 20)
    if sport == 'americanfootball_ncaaf':
        return date(year, 8, 1), date(year + 1, 2, 1)
    if sport == 'baseball_mlb':
        return date(year, 3, 1), date(year, 11, 15)
    if sport == 'basketball_ncaab':
        return date(year, 10, 15), date(year + 1, 5, 1)
    return date(year, 10, 1), date(year + 1, 7, 1)


def fetch(domain, league, endpoint, params):
    url = f'https://site.api.espn.com/apis/site/v2/sports/{domain}/{league}/{endpoint}'
    for attempt in range(3):
        try:
            r = SESSION.get(url, params=params, timeout=35)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f'HTTP {r.status_code}', response=r)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt * 3)


def is_final_regular(event):
    return (event.get('season') or {}).get('type') == 2 and bool(
        ((event.get('status') or {}).get('type') or {}).get('completed'))


def save_snapshot(conn, sport, event_id, category, key, payload):
    # Fixed timestamp makes a repeated historical import idempotent.
    # The daily collector uses its own timestamps, which are left untouched.
    stamp = datetime(2000, 1, 1, tzinfo=timezone.utc)
    cur = conn.execute('''INSERT INTO historical_snapshots
      (collected_at,source,sport,event_id,category,item_key,payload)
      VALUES (%s,'espn',%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING''',
      (stamp, sport, str(event_id), category, str(key), json.dumps(payload)))
    return cur.rowcount


def init_progress(conn):
    for sport in SPORTS:
        for year in START_SEASONS:
            first, _ = season_window(sport, year)
            conn.execute('''INSERT INTO sports_edge_backfill_progress
              (sport,season_start,next_date) VALUES (%s,%s,%s)
              ON CONFLICT DO NOTHING''', (sport, year, first))
    conn.commit()


def backfill_games(conn, max_days):
    checked = saved = 0
    today = datetime.now(timezone.utc).date()
    while checked < max_days:
        rows = conn.execute('''SELECT * FROM sports_edge_backfill_progress
          WHERE finished=FALSE ORDER BY updated_at ASC, sport, season_start''').fetchall()
        if not rows:
            logging.info('All historical scoreboard date ranges completed')
            break
        progress = rows[0]
        sport, year, day = progress['sport'], progress['season_start'], progress['next_date']
        _, end = season_window(sport, year)
        end = min(end, today - timedelta(days=1))
        if day > end:
            conn.execute('''UPDATE sports_edge_backfill_progress SET finished=TRUE,
              updated_at=NOW() WHERE sport=%s AND season_start=%s''', (sport, year))
            conn.commit()
            continue
        domain, league = SPORTS[sport]
        try:
            board = fetch(domain, league, 'scoreboard',
                          {'dates': day.strftime('%Y%m%d'), 'limit': 300})
            events = board.get('events') or []
            # Detect unexpected API shapes instead of silently marking the day complete.
            if not isinstance(events, list):
                raise ValueError('ESPN scoreboard events was not a list')
            day_saved = 0
            for event in events:
                if not isinstance(event, dict) or not is_final_regular(event):
                    continue
                eid = event.get('id')
                if eid:
                    day_saved += save_snapshot(conn, sport, eid, 'game',
                                               day.strftime('%Y%m%d'), event)
            conn.execute('''UPDATE sports_edge_backfill_progress
              SET next_date=%s,days_checked=days_checked+1,
              games_saved=games_saved+%s,last_error=NULL,updated_at=NOW()
              WHERE sport=%s AND season_start=%s''',
              (day + timedelta(days=1), day_saved, sport, year))
            conn.commit()
            checked += 1
            saved += day_saved
            if checked % 25 == 0:
                logging.info('Scoreboards: days_checked=%d new_game_rows=%d latest=%s %s',
                             checked, saved, sport, day)
            time.sleep(float(os.getenv('ESPN_REQUEST_PAUSE', '0.15')))
        except (requests.RequestException, ValueError) as exc:
            conn.rollback()
            conn.execute('''UPDATE sports_edge_backfill_progress SET last_error=%s,
              updated_at=NOW() WHERE sport=%s AND season_start=%s''',
              (str(exc)[:1000], sport, year))
            conn.commit()
            logging.error('Stopped at %s %s: %s (cursor preserved)', sport, day, exc)
            raise
    return checked, saved


def backfill_boxscores(conn, max_games):
    # DISTINCT ON chooses one snapshot per event; only regular-season finals.
    # Skip events already archived as boxscores and failed lookups to avoid endless retries.
    rows = conn.execute('''SELECT DISTINCT ON (g.sport,g.event_id)
          g.sport,g.event_id,g.item_key,g.payload
        FROM historical_snapshots g
        WHERE g.source='espn' AND g.category='game'
          AND g.sport = ANY(%s)
          AND g.collected_at = %s
          AND NOT EXISTS (SELECT 1 FROM historical_snapshots b
            WHERE b.source='espn' AND b.sport=g.sport
              AND b.event_id=g.event_id AND b.category='boxscore')
          AND NOT EXISTS (SELECT 1 FROM sports_edge_backfill_boxscore_failures f
            WHERE f.sport=g.sport AND f.event_id=g.event_id)
        ORDER BY g.sport,g.event_id,g.collected_at DESC
        LIMIT %s''',
        (list(SPORTS), datetime(2000, 1, 1, tzinfo=timezone.utc), max_games)).fetchall()
    saved = 0
    failed = 0
    for row in rows:
        if not is_final_regular(row['payload']):
            continue
        sport, eid = row['sport'], row['event_id']
        domain, league = SPORTS[sport]
        try:
            summary = fetch(domain, league, 'summary', {'event': eid})
            box = summary.get('boxscore') or {}
            if box:
                saved += save_snapshot(conn, sport, eid, 'boxscore', row['item_key'], box)
            else:
                conn.execute('''INSERT INTO sports_edge_backfill_boxscore_failures
                  (sport,event_id,reason) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING''',
                  (sport, eid, 'No boxscore in ESPN response'))
                failed += 1
            conn.commit()
            time.sleep(float(os.getenv('ESPN_REQUEST_PAUSE', '0.15')))
        except (requests.RequestException, ValueError) as exc:
            conn.rollback()
            conn.execute('''INSERT INTO sports_edge_backfill_boxscore_failures
              (sport,event_id,reason) VALUES (%s,%s,%s)
              ON CONFLICT(sport,event_id) DO UPDATE SET
              reason=EXCLUDED.reason,attempted_at=NOW()''', (sport, eid, str(exc)[:1000]))
            conn.commit()
            failed += 1
            logging.warning('Boxscore %s %s unavailable: %s', sport, eid, exc)
    return len(rows), saved, failed


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    days = max(0, min(600, int(os.getenv('BACKFILL_MAX_DAYS', '250'))))
    boxes = max(0, min(300, int(os.getenv('BACKFILL_MAX_BOXSCORES', '100'))))
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row,
                         connect_timeout=20) as conn:
        conn.execute(SCHEMA)
        conn.commit()
        init_progress(conn)
        checked, games = backfill_games(conn, days)
        queried, saved, failed = backfill_boxscores(conn, boxes)
        progress = conn.execute('''SELECT COUNT(*) FILTER (WHERE finished) AS done,
          COUNT(*) AS total, SUM(days_checked) AS days,
          SUM(games_saved) AS games FROM sports_edge_backfill_progress''').fetchone()
        logging.info('BACKFILL SUMMARY days_this_run=%s game_rows_this_run=%s '
                     'boxscores_attempted=%s boxscores_saved=%s boxscores_unavailable=%s '
                     'season_ranges_complete=%s/%s total_days=%s total_game_rows=%s '
                     'odds_api_requests=0', checked, games, queried, saved, failed,
                     progress['done'], progress['total'], progress['days'], progress['games'])

if __name__ == '__main__':
    main()
