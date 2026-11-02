"""Evidence-first matchup research.

Uses completed ESPN results for form/rest and Open-Meteo for confirmed outdoor
venues in weather-sensitive sports. Injury notes are only shown when supplied
by a verified, timestamped upstream injury feed; no statuses are inferred.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import logging
import re
import statistics
import requests

PT = ZoneInfo('America/Los_Angeles')
HEADERS = {'User-Agent': 'sports-edge-analytics/0.3 (personal research)'}
INDOOR_SPORTS = {'NBA', 'NHL', 'CBB', 'NCAAB', 'NCAAMB', 'COLLEGE BASKETBALL'}
WEATHER_SPORTS = {'NFL', 'CFB', 'NCAAF', 'COLLEGE FOOTBALL', 'MLB'}


def norm(s):
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())


def get_json(url, params=None):
    response = requests.get(url, params=params or {}, headers=HEADERS, timeout=12)
    response.raise_for_status()
    return response.json()


def completed_games(sport, league, day, lookback=18):
    """Return up to 10 completed regular-season games per team, newest first."""
    games = {}
    for offset in range(1, lookback + 1):
        date = day - timedelta(days=offset)
        try:
            board = get_json(
                f'https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard',
                {'dates': date.strftime('%Y%m%d'), 'limit': 200},
            )
        except (requests.RequestException, ValueError) as exc:
            logging.warning('ESPN recent-games request failed %s %s: %s', league, date, exc)
            continue
        for event in board.get('events') or []:
            if (event.get('season') or {}).get('type') != 2:
                continue
            if not ((event.get('status') or {}).get('type') or {}).get('completed'):
                continue
            competition = (event.get('competitions') or [{}])[0]
            competitors = competition.get('competitors') or []
            if len(competitors) != 2:
                continue
            try:
                scores = [float(c['score']) for c in competitors]
                played = datetime.fromisoformat(event['date'].replace('Z', '+00:00'))
            except (ValueError, KeyError, TypeError):
                continue
            for i, competitor in enumerate(competitors):
                name = (competitor.get('team') or {}).get('displayName')
                if name:
                    games.setdefault(norm(name), []).append({
                        'date': played,
                        'scored': scores[i],
                        'allowed': scores[1 - i],
                        'win': scores[i] > scores[1 - i],
                        'home': competitor.get('homeAway') == 'home',
                    })
    return {name: sorted(rows, key=lambda r: r['date'], reverse=True)[:10]
            for name, rows in games.items()}


def summarize(team, games, sport):
    if not games:
        return f'{team}: last-10 performance unavailable from completed ESPN regular-season games.'
    n = len(games)
    wins = sum(g['win'] for g in games)
    scored = statistics.mean(g['scored'] for g in games)
    allowed = statistics.mean(g['allowed'] for g in games)
    label = (sport or '').upper()
    unit = 'goals' if label == 'NHL' else 'runs' if label == 'MLB' else 'points'
    return (f'{team} last {n} completed regular-season games: {wins}-{n-wins}; '
            f'{scored:.1f} {unit} scored and {allowed:.1f} allowed per game.')


def rest_note(home, away, home_games, away_games, start):
    if not home_games or not away_games:
        return None
    home_gap = (start.astimezone(PT).date() - home_games[0]['date'].astimezone(PT).date()).days - 1
    away_gap = (start.astimezone(PT).date() - away_games[0]['date'].astimezone(PT).date()).days - 1
    if min(home_gap, away_gap) < 0:
        return None
    if home_gap == 0 or away_gap == 0 or abs(home_gap - away_gap) >= 2:
        parts = [f'Rest: {home} {home_gap} day(s) off; {away} {away_gap} day(s) off.']
        if home_gap == 0:
            parts.append(f'{home} is on a back-to-back.')
        if away_gap == 0:
            parts.append(f'{away} is on a back-to-back.')
        if abs(home_gap - away_gap) >= 2:
            parts.append('Notable rest disparity.')
        return ' '.join(parts)
    return None


def outdoor_weather(espn_event, start):
    """Return a weather note only for ESPN-confirmed outdoor venues."""
    competition = (espn_event.get('competitions') or [{}])[0]
    venue = competition.get('venue') or {}
    if venue.get('indoor') is not False:
        return None
    address = venue.get('address') or {}
    city, state = address.get('city'), address.get('state')
    if not city:
        return None
    try:
        geo = get_json('https://geocoding-api.open-meteo.com/v1/search', {
            'name': city, 'count': 10, 'language': 'en', 'format': 'json', 'countryCode': 'US'
        })
        results = geo.get('results') or []
        matches = [r for r in results if not state or
                   (r.get('admin1') or '').lower() == str(state).lower() or
                   (r.get('admin1_code') or '').lower() == str(state).lower()]
        # Don't select an unrelated city if the state doesn't match.
        loc = (matches[0] if matches else None) if state else (results[0] if results else None)
        if not loc:
            return None
        forecast = get_json('https://api.open-meteo.com/v1/forecast', {
            'latitude': loc['latitude'], 'longitude': loc['longitude'],
            'hourly': 'temperature_2m,precipitation_probability,precipitation,wind_speed_10m,wind_gusts_10m',
            'wind_speed_unit': 'mph', 'temperature_unit': 'fahrenheit',
            'precipitation_unit': 'inch', 'timezone': 'America/Los_Angeles',
            'forecast_days': 7,
        })
        hourly = forecast.get('hourly') or {}
        times = hourly.get('time') or []
        local = start.astimezone(PT)
        target = local.strftime('%Y-%m-%dT%H:00')
        if target not in times:
            return None
        idx = times.index(target)
        def v(key):
            values = hourly.get(key) or []
            return values[idx] if idx < len(values) else None
        temp, wind, gust = v('temperature_2m'), v('wind_speed_10m'), v('wind_gusts_10m')
        chance, precip = v('precipitation_probability'), v('precipitation')
        parts = [f'Outdoor forecast near {city} ({local:%I:%M %p PT}):']
        if temp is not None:
            parts.append(f'{temp:.0f}°F')
        if wind is not None:
            parts.append(f'wind {wind:.0f} mph')
        if gust is not None:
            parts.append(f'gusts {gust:.0f} mph')
        if chance is not None:
            parts.append(f'precipitation chance {chance:.0f}%')
        if precip is not None:
            parts.append(f'precipitation {precip:.2f} in')
        return ', '.join(parts) + '. Open-Meteo city-level forecast; roof status may change.'
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        logging.warning('Weather lookup failed for %s: %s', city, exc)
        return None


def verified_injury_notes(injury_reports, home, away):
    """Format optional verified injury entries, without asserting unverified statuses.

    Expected mapping: {team name: [{player, status, source, checked_at, note?}, ...]}.
    Caller is responsible for verifying source authenticity and freshness.
    """
    if not isinstance(injury_reports, dict):
        return []
    notes = []
    for team in (home, away):
        entries = next((items for key, items in injury_reports.items()
                        if norm(key) == norm(team)), [])
        if not isinstance(entries, list):
            continue
        for entry in entries[:8]:
            if not isinstance(entry, dict):
                continue
            player = str(entry.get('player') or '').strip()
            status = str(entry.get('status') or '').strip()
            source = str(entry.get('source') or '').strip()
            checked = str(entry.get('checked_at') or '').strip()
            if not all((player, status, source, checked)):
                continue
            extra = str(entry.get('note') or '').strip()
            notes.append(f'Injury ({team}): {player} — {status}' +
                         (f' ({extra})' if extra else '') +
                         f'. Source: {source}; checked {checked}.')
    return notes


def research(espn_event, sport, home, away, recent, start, home_probability=None,
             injury_reports=None):
    """Return (why, watch) lists; signature remains backward compatible."""
    hg = recent.get(norm(home), [])
    ag = recent.get(norm(away), [])
    why = [summarize(home, hg, sport), summarize(away, ag, sport)]
    if hg and ag:
        why.append(
            f'Last-{len(hg)} vs last-{len(ag)} sample: {home} scored '
            f'{statistics.mean(x["scored"] for x in hg):.1f} per game; '
            f'{away} allowed {statistics.mean(x["allowed"] for x in ag):.1f} per game. '
            'Descriptive matchup comparison, not a causal prediction.'
        )
    if home_probability is not None:
        why.append(
            f'Experimental independent record-strength estimate: {home} '
            f'{home_probability:.1%} win probability. Not backtested or calibrated.'
        )
    watch = []
    rest = rest_note(home, away, hg, ag, start)
    if rest:
        watch.append(rest)
    injuries = verified_injury_notes(injury_reports, home, away)
    watch.extend(injuries if injuries else ['Injury report unavailable; player availability not verified.'])
    if (sport or '').upper() in WEATHER_SPORTS:
        weather = outdoor_weather(espn_event, start)
        if weather:
            watch.append(weather)
    return why, watch
