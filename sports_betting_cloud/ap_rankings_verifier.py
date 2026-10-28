"""Fail-closed AP Top 25 eligibility verification for today's college odds events.

Uses ESPN's explicitly labelled AP poll, not generic ESPN game rankings.
No Odds API calls. Invoked by prediction_engine before predicting college games.
Includes diagnostics to distinguish excluded, unmatched, and unverifiable events.
"""
import logging
import re
from datetime import datetime, timedelta
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


def _poll_diagnostics(payload, today, sport):
    """Log why the AP poll is accepted or rejected, without changing criteria."""
    if not isinstance(payload, dict):
        logging.warning('AP diagnostic %s: rankings response is not an object (%s)', sport, type(payload).__name__)
        return
    rankings = payload.get('rankings') or []
    logging.info('AP diagnostic %s: ranking entries=%d, latestWeek=%s',
                 sport, len(rankings), payload.get('latestWeek'))
    for p in rankings:
        if not isinstance(p, dict):
            continue
        ranks = p.get('ranks') or []
        rank_ids = set()
        rank_fields = []
        for row in ranks:
            if not isinstance(row, dict):
                continue
            rank_fields.append(row.get('current'))
            try:
                number = int(row.get('current'))
            except (ValueError, TypeError):
                continue
            team_id = (row.get('team') or {}).get('id')
            if 1 <= number <= 25 and team_id is not None:
                rank_ids.add(str(team_id))
        logging.info('AP diagnostic %s: poll name=%r type=%r date=%r lastUpdated=%r rows=%d valid_ranked_ids=%d first_rank_values=%s',
                     sport, p.get('name'), p.get('type'), p.get('date'), p.get('lastUpdated'),
                     len(ranks), len(rank_ids), rank_fields[:5])
    selected = _ap_poll(payload, today)
    if selected:
        logging.info('AP diagnostic %s: ACCEPTED poll=%r poll_date=%s team_count=%d age_days=%d',
                     sport, selected[2], selected[0], len(selected[1]), (today - selected[0]).days)
    else:
        logging.warning('AP diagnostic %s: REJECTED poll (AP identity, 25 distinct ranked IDs, or poll date within 0-9 days failed)', sport)


def _ap_poll(payload, today):
    """Return (poll date, team ids, poll label), or None if unverifiable."""
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
    if len(ids) != 25:
        return None
    poll_date = (_parse_date(poll.get('lastUpdated')) or _parse_date(poll.get('date'))
                 or _parse_date((payload.get('latestWeek') or {}).get('startDate')))
    if poll_date is None or poll_date > today or today - poll_date > timedelta(days=9):
        return None
    return poll_date, ids, str(poll.get('name') or 'AP Top 25')


def verify_college_events(conn, now, today, odds_by_sport, fetch_json=None):
    """Write verified_events rows for cached college odds events, failing closed."""
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
            logging.info('AP verification %s: %s', sport, stats[sport])
            continue

        poll = None
        try:
            payload = fetch_json(f'https://site.api.espn.com/apis/site/v2/sports/{domain}/{league}/rankings', {})
            _poll_diagnostics(payload, today, sport)
            poll = _ap_poll(payload, today)
        except (requests.RequestException, ValueError, TypeError, AttributeError, KeyError) as exc:
            logging.warning('AP verification: %s rankings fetch/parse failed: %s', sport, exc)
        if poll is None:
            logging.warning('AP verification: %s has no complete, dated AP Top 25 poll within 9 days; failing closed', sport)

        board_events = []
        if poll is not None:
            try:
                for offset in (-1, 0, 1):
                    board_date = today + timedelta(days=offset)
                    payload = fetch_json(f'https://site.api.espn.com/apis/site/v2/sports/{domain}/{league}/scoreboard',
                                         {'dates': board_date.strftime('%Y%m%d'), 'limit': 500})
                    events = payload.get('events') or []
                    logging.info('AP diagnostic %s: ESPN scoreboard date=%s events=%d',
                                 sport, board_date.isoformat(), len(events))
                    board_events.extend(events)
            except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
                logging.warning('AP verification: %s scoreboard unavailable: %s', sport, exc)
                board_events = []

        def normalize(s):
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
        reasons = {}
        for item in upcoming:
            key = (normalize(item.get('home_team')), normalize(item.get('away_team')))
            matches = match_index.get(key) or []
            valid = len(matches) == 1 and (matches[0][0].get('season') or {}).get('type') == 2
            ranked = bool(valid and poll and (matches[0][1] in poll[1] or matches[0][2] in poll[1]))
            eligible += int(ranked)
            if poll is None:
                reason = 'poll_unavailable_or_rejected'
            elif not matches:
                reason = 'espn_match_not_found'
            elif len(matches) != 1:
                reason = 'ambiguous_espn_match'
            elif not valid:
                reason = 'not_verified_regular_season'
            elif not ranked:
                reason = 'verified_but_neither_team_ap_ranked'
            else:
                reason = 'eligible'
            reasons[reason] = reasons.get(reason, 0) + 1
            if matches:
                e, home_id, away_id = matches[0]
                logging.info('AP diagnostic %s: odds=%r @ %r, event_id=%s; ESPN event=%s home_id=%s away_id=%s season_type=%s; result=%s',
                             sport, item.get('away_team'), item.get('home_team'), item.get('id'),
                             e.get('id'), home_id, away_id, (e.get('season') or {}).get('type'), reason)
            else:
                logging.info('AP diagnostic %s: odds=%r @ %r, event_id=%s; ESPN match count=0; result=%s',
                             sport, item.get('away_team'), item.get('home_team'), item.get('id'), reason)
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
        logging.info('AP diagnostic %s: exclusion reasons=%s', sport, reasons)
        logging.info('AP verification %s: %s', sport, stats[sport])
    return stats
