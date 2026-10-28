"""Experimental independent moneyline model using ESPN team win-loss records.

Scope: NBA, NHL, NFL regular-season games only. No college, props, spreads or totals.
No market odds enter probability estimation. Not backtested/calibrated: research only.
Missing or uncertain team records cause a skip, not a fabricated prediction.
"""
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
import requests

PT = ZoneInfo('America/Los_Angeles')
SPORTS = {
    'basketball_nba': ('NBA', 'basketball', 'nba', 6),
    'icehockey_nhl': ('NHL', 'hockey', 'nhl', 6),
    'americanfootball_nfl': ('NFL', 'football', 'nfl', 4),
}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS model_predictions (
 event_id TEXT NOT NULL, market TEXT NOT NULL, selection TEXT NOT NULL,
 point_key TEXT NOT NULL DEFAULT '', estimated_prob DOUBLE PRECISION NOT NULL
 CHECK (estimated_prob > 0 AND estimated_prob < 1),
 generated_at TIMESTAMPTZ NOT NULL, model_version TEXT NOT NULL,
 PRIMARY KEY(event_id,market,selection,point_key));
CREATE TABLE IF NOT EXISTS verified_events (
 event_id TEXT PRIMARY KEY, sport TEXT NOT NULL,
 is_preseason BOOLEAN NOT NULL, ap_top25_team_in_match BOOLEAN,
 verified_at TIMESTAMPTZ NOT NULL, verification_source TEXT NOT NULL);
'''
HEADERS = {'User-Agent': 'sports-edge-research/0.1'}

def norm(s):
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())

def request_json(url, params):
    r = requests.get(url, params=params, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.json()

def team_record(competitor):
    """Use explicitly reported overall W-L record, never infer from odds."""
    for item in competitor.get('records') or []:
        if item.get('type') in ('total', 'overall') or item.get('name') in ('overall', 'Overall'):
            summary = item.get('summary', '')
            match = re.match(r'^(\d+)-(\d+)(?:-\d+)?$', summary)
            if match:
                return tuple(map(int, match.groups()))
    return None

def probability(home, away, min_games):
    if home is None or away is None:
        return None
    hw, hl = home
    aw, al = away
    if hw + hl < min_games or aw + al < min_games:
        return None
    # Beta(5,5) shrinkage: deliberately conservative early-season estimates.
    h = (hw + 5) / (hw + hl + 10)
    a = (aw + 5) / (aw + al + 10)
    # logit strength difference plus modest, uncalibrated home-court/ice/field term.
    logit = lambda p: math.log(p / (1 - p))
    z = logit(h) - logit(a) + 0.12
    return max(0.25, min(0.75, 1 / (1 + math.exp(-z))))

def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    now = datetime.now(timezone.utc)
    today = now.astimezone(PT).date()
    key = os.environ['ODDS_API_KEY']
    count = {'odds_events': 0, 'matched': 0, 'regular': 0, 'sufficient_records': 0, 'predictions': 0}
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row) as conn:
        conn.execute(SCHEMA)
        for sport_key, (sport, league_sport, league, min_games) in SPORTS.items():
            try:
                odds = request_json(f'https://api.the-odds-api.com/v4/sports/{sport_key}/odds/',
                    {'apiKey': key, 'regions': 'us', 'markets': 'h2h', 'oddsFormat': 'american'})
            except (requests.RequestException, ValueError) as exc:
                logging.warning('%s odds unavailable: %s', sport, exc)
                continue
            # ESPN scoreboard dates are UTC-day based. Check adjacent dates for PT boundary.
            scoreboards = []
            for delta in (-1, 0, 1):
                day = (today + timedelta(days=delta)).strftime('%Y%m%d')
                try:
                    board = request_json(f'https://site.api.espn.com/apis/site/v2/sports/{league_sport}/{league}/scoreboard',
                        {'dates': day, 'limit': 200})
                    scoreboards.extend(board.get('events') or [])
                except (requests.RequestException, ValueError) as exc:
                    logging.warning('%s ESPN scoreboard %s unavailable: %s', sport, day, exc)
            by_match = {}
            for e in scoreboards:
                comp = (e.get('competitions') or [{}])[0]
                teams = comp.get('competitors') or []
                if len(teams) != 2:
                    continue
                home = next((t for t in teams if t.get('homeAway') == 'home'), None)
                away = next((t for t in teams if t.get('homeAway') == 'away'), None)
                if not home or not away:
                    continue
                hn = (home.get('team') or {}).get('displayName')
                an = (away.get('team') or {}).get('displayName')
                if hn and an:
                    by_match[(norm(hn), norm(an))] = (e, home, away)
            for e in odds:
                start_str = e.get('commence_time') or ''
                try:
                    start = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                except ValueError:
                    continue
                if start <= now or start.astimezone(PT).date() != today:
                    continue
                count['odds_events'] += 1
                pair = by_match.get((norm(e.get('home_team')), norm(e.get('away_team'))))
                if pair is None:
                    continue
                count['matched'] += 1
                espn_event, home, away = pair
                # ESPN season.type=2 is regular season for these leagues.
                season_type = (espn_event.get('season') or {}).get('type')
                if season_type != 2:
                    continue
                count['regular'] += 1
                p_home = probability(team_record(home), team_record(away), min_games)
                if p_home is None:
                    continue
                count['sufficient_records'] += 1
                event_id = e.get('id')
                if not event_id:
                    continue
                conn.execute('''INSERT INTO verified_events
                    (event_id,sport,is_preseason,ap_top25_team_in_match,verified_at,verification_source)
                    VALUES (%s,%s,FALSE,NULL,%s,%s)
                    ON CONFLICT(event_id) DO UPDATE SET sport=EXCLUDED.sport,
                    is_preseason=FALSE,verified_at=EXCLUDED.verified_at,
                    verification_source=EXCLUDED.verification_source''',
                    (event_id, sport, now, 'ESPN scoreboard regular-season classification'))
                for name, p in ((e.get('home_team'), p_home), (e.get('away_team'), 1-p_home)):
                    if not name:
                        continue
                    conn.execute('''INSERT INTO model_predictions
                        (event_id,market,selection,point_key,estimated_prob,generated_at,model_version)
                        VALUES (%s,'h2h',%s,'',%s,%s,%s)
                        ON CONFLICT(event_id,market,selection,point_key) DO UPDATE SET
                        estimated_prob=EXCLUDED.estimated_prob,
                        generated_at=EXCLUDED.generated_at,model_version=EXCLUDED.model_version''',
                        (event_id, name, p, now, 'experimental-shrunk-record-v1-uncalibrated'))
                    count['predictions'] += 1
        logging.info('Prediction engine funnel: %s', count)
        if not count['predictions']:
            logging.warning('No model predictions available; do not invent picks. Early season, missing ESPN records, or no upcoming games can cause this.')

if __name__ == '__main__':
    main()
