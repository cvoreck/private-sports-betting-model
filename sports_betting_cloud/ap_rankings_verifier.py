"""Fail-closed AP Top 25 eligibility verification for today's college odds events.

Uses ESPN's explicitly labelled AP poll, not ESPN's generic game ranking.
No Odds API calls. Invoke from prediction_engine before predicting college games.
"""
import logging
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

PT = ZoneInfo('America/Los_Angeles')
LEAGUES = {
    'americanfootball_ncaaf': ('College Football', 'football', 'college-football'),
    'basketball_ncaab': ('College Basketball', 'basketball', 'mens-college-basketball'),
}


def _parse_date(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date()
    except ValueError:
        return None


def _ap_poll(payload, today):
    """Return (poll date, team ids, poll label), or None if unverifiable.

    A poll's own lastUpdated/date is preferred; latestWeek.startDate is a
    fallback. Never use the retrieval timestamp as a fabricated poll date.
    """
    rankings = payload.get('rankings') or []
    ap = [p for p in rankings if (str(p.get('type', '')).lower() == 'ap'
          or (not p.get('type') and str(p.get('name', '')).strip().lower() in
              ('ap top 25', 'ap poll', 'associated press top 25')))]
    if len(ap) != 1:
        return None
    poll = ap[0]
    ranks = poll.get('ranks') or []
    ids = set()
    for row in ranks:
        rank = row.get('current')
        team_id = (row.get('team') or {}).get('id')
        try:
            number = int(rank)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= 25 and team_id is not None:
            ids.add(str(team_id))
    # Exactly 25 distinct AP teams required; partial API payloads fail closed.
    if len(ids) != 25:
        return None
    poll_date = (_parse_date(poll.get('lastUpdated')) or _parse_date(poll.get('date'))
                 or _parse_date((payload.get('latestWeek') or {}).get('startDate')))
    if poll_date is None or poll_date > today or today - poll_date > timedelta(days=9):
        return None
    return poll_date, ids, str(poll.get('name') or 'AP Top 25')


def verify_college_events(conn, now, today, odds_by_sport, fetch_json=None):
    """Write verified_events rows for every cached college odds event.

    An unavailable/stale poll invalidates eligibility for *today's* games.
    Only exact ESPN home/away team-ID matches are accepted.
    """
    if fetch_json is None:
        def fetch_json(url, params):
            response = requests.get(url, params=params, timeout=25,
                                    headers={'User-Agent': 'sports-edge-research/0.2'})
            response.raise_for_status()
            return response.json()
    stats = {}
    for sport_key, (sport, domain, league) in LEAGUES.items():
        odds = odds_by_sport.get(sport_key) or []
        upcoming = []
        for item in odds:
            try:
                start = datetime.fromisoformat(item['commence_time'].replace('Z', '+00:00'))
            except (KeyError, ValueError, TypeError):
                continue
            if start.tzinfo and start > now and start.astimezone(PT).date() == today and item.get('id'):
                upcoming.append(item)
        if not upcoming:
            stats[sport] = {'upcoming': 0, 'eligible': 0, 'unverified': 0}
            continue
        poll = None
        try:
            payload = fetch_json(f'https://site.api.espn.com/apis/site/v2/sports/{domain}/{league}/rankings', {})
            poll = _ap_poll(payload, today)
        except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
            logging.warning('AP verification: %s rankings fetch failed: %s', sport, exc)
        if poll is None:
            logging.warning('AP verification: %s has no complete, dated AP Top 25 poll within 9 days; failing closed', sport)
        # Fetch ESPN scoreboard for odds events; match both teams and home/away.
        board_events = []
        if poll is not None:
            try:
                for offset in (-1, 0, 1):
                    payload = fetch_json(f'https://site.api.espn.com/apis/site/v2/sports/{domain}/{league}/scoreboard',
                                         {'dates': (today + timedelta(days=offset)).strftime('%Y%m%d'), 'limit': 500})
                    board_events.extend(payload.get('events') or [])
            except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
                logging.warning('AP verification: %s scoreboard unavailable: %s', sport, exc)
                board_events = []
        def normalize(s):
            import re
            return re.sub(r'[^a-z0-9]+', '', str(s or '').lower())
        match_index = {}
        for e in board_events:
            competitors = ((e.get('competitions') or [{}])[0]).get('competitors') or []
            h = next((c for c in competitors if c.get('homeAway') == 'home'), None)
            a = next((c for c in competitors if c.get('homeAway') == 'away'), None)
            if not h or not a:
                continue
            ht, at = h.get('team') or {}, a.get('team') or {}
            if not ht.get('id') or not at.get('id'):
                continue
            key = (normalize(ht.get('displayName')), normalize(at.get('displayName')))
            match_index.setdefault(key, []).append((e, str(ht['id']), str(at['id'])))
        eligible = 0
        for item in upcoming:
            key = (normalize(item.get('home_team')), normalize(item.get('away_team')))
            matches = match_index.get(key) or []
            valid = len(matches) == 1 and (matches[0][0].get('season') or {}).get('type') == 2
            ranked = bool(valid and poll and (matches[0][1] in poll[1] or matches[0][2] in poll[1]))
            eligible += int(ranked)
            if valid and poll:
                source = f'ESPN {poll[2]} ({sport}), poll date {poll[0].isoformat()}, team-ID matched; {league}'
            else:
                source = f'AP eligibility unverified: missing/stale poll or unmatched ESPN regular-season event ({league})'
            conn.execute('''INSERT INTO verified_events
                (event_id,sport,is_preseason,ap_top25_team_in_match,verified_at,verification_source)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(event_id) DO UPDATE SET sport=EXCLUDED.sport,
                is_preseason=EXCLUDED.is_preseason,
                ap_top25_team_in_match=EXCLUDED.ap_top25_team_in_match,
                verified_at=EXCLUDED.verified_at,verification_source=EXCLUDED.verification_source''',
                (item['id'], sport, not valid, ranked, now, source))
        stats[sport] = {'upcoming': len(upcoming), 'eligible': eligible, 'unverified': len(upcoming)-eligible}
        logging.info('AP verification %s: %s', sport, stats[sport])
    return stats
