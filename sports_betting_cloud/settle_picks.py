"""Settle verified completed ESPN games against immutable saved picks.

Run: python sports_betting_cloud/settle_picks.py
Requires DATABASE_URL. Uses ESPN public scoreboards, no Odds API credits.
Only grades automated hypothetical picks; never changes odds, lines, or stakes.
"""
import logging
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import requests
from psycopg.rows import dict_row

PT = ZoneInfo('America/Los_Angeles')
ESPN = {
    'NFL': ('football', 'nfl'),
    'NBA': ('basketball', 'nba'),
    'MLB': ('baseball', 'mlb'),
    'NHL': ('hockey', 'nhl'),
    'College Football': ('football', 'college-football'),
    'College Basketball': ('basketball', 'mens-college-basketball'),
}
LOG = logging.getLogger('settlement')


def norm(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').casefold())


def scoreboard(session, sport, day):
    category, league = ESPN[sport]
    url = f'https://site.api.espn.com/apis/site/v2/sports/{category}/{league}/scoreboard'
    response = session.get(url, params={'dates': day.strftime('%Y%m%d'), 'limit': 500}, timeout=20)
    response.raise_for_status()
    events = response.json().get('events') or []
    LOG.info('ESPN %s %s: %d events', sport, day, len(events))
    return events


def teams_and_scores(event):
    competitions = event.get('competitions') or []
    if len(competitions) != 1:
        return None
    competition = competitions[0]
    competitors = competition.get('competitors') or []
    home = next((c for c in competitors if c.get('homeAway') == 'home'), None)
    away = next((c for c in competitors if c.get('homeAway') == 'away'), None)
    if not home or not away:
        return None
    def info(c):
        t = c.get('team') or {}
        names = {norm(x) for x in (t.get('displayName'), t.get('name'), t.get('shortDisplayName')) if x}
        try:
            score = int(c['score'])
        except (ValueError, TypeError, KeyError):
            score = None
        return names, score
    home_names, home_score = info(home)
    away_names, away_score = info(away)
    completed = bool(((competition.get('status') or {}).get('type') or {}).get('completed') or
                     ((event.get('status') or {}).get('type') or {}).get('completed'))
    return home_names, away_names, home_score, away_score, completed


def event_match(row, events):
    matchup = str(row.get('matchup') or '')
    if ' @ ' not in matchup:
        return None
    away, home = matchup.rsplit(' @ ', 1)
    matches = []
    for event in events:
        parsed = teams_and_scores(event)
        if not parsed:
            continue
        hn, an, *_ = parsed
        if norm(home) in hn and norm(away) in an:
            matches.append((event, parsed))
    # Never grade an ambiguous matchup.
    return matches[0] if len(matches) == 1 else None


def grade_pick(row, parsed):
    home_names, away_names, home_score, away_score, completed = parsed
    if not completed or home_score is None or away_score is None:
        return None
    market = str(row.get('market') or '').strip().casefold()
    selection = str(row.get('selection') or '').strip()
    if market in ('h2h', 'moneyline'):
        side = norm(selection)
        if side in home_names:
            margin = home_score - away_score
        elif side in away_names:
            margin = away_score - home_score
        else:
            return None
        if margin == 0:
            return None  # Unresolved tie/league-specific settlement; manual review.
        return 'Win' if margin > 0 else 'Loss'
    if market in ('spreads', 'spread'):
        m = re.fullmatch(r'(.+?)\s*\(\s*([+-]?\d+(?:\.\d+)?)\s*\)', selection)
        if not m:
            return None
        side, point = norm(m.group(1)), float(m.group(2))
        if side in home_names:
            margin = home_score - away_score
        elif side in away_names:
            margin = away_score - home_score
        else:
            return None
        adjusted = margin + point
        return 'Push' if abs(adjusted) < 1e-8 else ('Win' if adjusted > 0 else 'Loss')
    if market in ('totals', 'total'):
        m = re.fullmatch(r'(over|under)\s*\(\s*(\d+(?:\.\d+)?)\s*\)', selection, re.I)
        if not m:
            return None
        side, line = m.group(1).casefold(), float(m.group(2))
        diff = home_score + away_score - line
        if abs(diff) < 1e-8:
            return 'Push'
        return 'Win' if (diff > 0) == (side == 'over') else 'Loss'
    return None


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    database_url = os.environ['DATABASE_URL']
    now = datetime.now(PT)
    cutoff = (now.date() - timedelta(days=14)).isoformat()
    with psycopg.connect(database_url, row_factory=dict_row, connect_timeout=15) as conn:
        rows = conn.execute('''SELECT id, date, sport, matchup, market, selection FROM picks
            WHERE result='Pending' AND kind='Hypothetical' AND rank IS NOT NULL
              AND source LIKE 'Auto:%%' AND date >= %s AND date <= %s
            ORDER BY date, id''', (cutoff, now.date().isoformat())).fetchall()
        LOG.info('Pending automated picks in 14-day window: %d', len(rows))
        grouped = defaultdict(list)
        for row in rows:
            if row['sport'] not in ESPN:
                LOG.warning('Unsupported sport for pick %s: %s', row['id'], row['sport'])
                continue
            try:
                day = date.fromisoformat(str(row['date'])[:10])
            except ValueError:
                LOG.warning('Invalid date for pick %s', row['id'])
                continue
            grouped[(row['sport'], day)].append(row)
        settled = 0
        unresolved = 0
        with requests.Session() as session:
            session.headers['User-Agent'] = 'SportsEdgeAnalyticsSettlement/1.0'
            for (sport, day), picks in grouped.items():
                # ESPN scoreboards may place a late-night PT game on the next UTC day.
                events_by_id = {}
                try:
                    for offset in (-1, 0, 1):
                        for event in scoreboard(session, sport, day + timedelta(days=offset)):
                            if event.get('id'):
                                events_by_id[event['id']] = event
                except (requests.RequestException, ValueError) as exc:
                    LOG.warning('ESPN unavailable %s %s; leaving %d pending: %s', sport, day, len(picks), exc)
                    unresolved += len(picks)
                    continue
                events = list(events_by_id.values())
                for row in picks:
                    matched = event_match(row, events)
                    if not matched:
                        LOG.info('PENDING id=%s no unique ESPN matchup: %s', row['id'], row['matchup'])
                        unresolved += 1
                        continue
                    event, parsed = matched
                    result = grade_pick(row, parsed)
                    if result is None:
                        LOG.info('PENDING id=%s not final or unsupported selection: %s', row['id'], row['selection'])
                        unresolved += 1
                        continue
                    category, league = ESPN[sport]
                    evidence = f'https://www.espn.com/{"nba" if league == "nba" else "nhl" if league == "nhl" else "nfl" if league == "nfl" else "mlb" if league == "mlb" else "college-football" if league == "college-football" else "mens-college-basketball"}/game/_/gameId/{event["id"]}'
                    with conn.transaction():
                        changed = conn.execute('''UPDATE picks SET result=%s, result_source=%s
                            WHERE id=%s AND result='Pending' RETURNING id''',
                            (result, evidence, row['id'])).fetchone()
                    if changed:
                        settled += 1
                        LOG.info('SETTLED id=%s %s %s (%s)', row['id'], result, row['selection'], event['id'])
        LOG.info('Settlement complete: settled=%d unresolved=%d', settled, unresolved)


if __name__ == '__main__':
    main()
