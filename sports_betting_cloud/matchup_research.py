"""Evidence-first matchup research using completed ESPN games and optional Open-Meteo forecasts.
No injury/lineup claims without a separately verified feed. Weather is queried ONLY for
venues explicitly marked outdoor by ESPN; retractable roof status is not assumed.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import logging, math, re, statistics
import requests

PT = ZoneInfo('America/Los_Angeles')
HEADERS = {'User-Agent': 'sports-edge-analytics/0.3 (personal research)'}

def norm(s):
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())

def get_json(url, params=None):
    response = requests.get(url, params=params or {}, headers=HEADERS, timeout=12)
    response.raise_for_status()
    return response.json()

def completed_games(sport, league, day, lookback=18):
    """Returns last ten completed regular-season games per team, newest first."""
    games = {}
    for offset in range(1, lookback + 1):
        date = day - timedelta(days=offset)
        try:
            board = get_json(f'https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard',
                             {'dates': date.strftime('%Y%m%d'), 'limit': 200})
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
                if not name:
                    continue
                games.setdefault(norm(name), []).append({
                    'date': played, 'scored': scores[i], 'allowed': scores[1-i],
                    'win': scores[i] > scores[1-i],
                    'home': competitor.get('homeAway') == 'home',
                })
    return {name: sorted(rows, key=lambda r: r['date'], reverse=True)[:10] for name, rows in games.items()}

def summarize(team, games, sport):
    if not games:
        return f'{team}: last-10 performance unavailable from completed ESPN regular-season games.'
    n = len(games)
    wins = sum(g['win'] for g in games)
    scored = statistics.mean(g['scored'] for g in games)
    allowed = statistics.mean(g['allowed'] for g in games)
    unit = 'points' if sport in ('NBA', 'NFL') else 'goals'
    return (f'{team} last {n} completed regular-season games: {wins}-{n-wins}; '
            f'{scored:.1f} {unit} scored and {allowed:.1f} allowed per game.')

def rest_note(home, away, home_games, away_games, start):
    if not home_games or not away_games:
        return 'Rest comparison unavailable: recent completed game dates could not be confirmed.'
    # Calendar-day rest is measured in local Pacific calendar days.
    home_gap = (start.astimezone(PT).date() - home_games[0]['date'].astimezone(PT).date()).days - 1
    away_gap = (start.astimezone(PT).date() - away_games[0]['date'].astimezone(PT).date()).days - 1
    if min(home_gap, away_gap) < 0:
        return 'Rest comparison unavailable: inconsistent game dates.'
    msg = f'Schedule: {home} has {home_gap} full day(s) off; {away} has {away_gap} full day(s) off.'
    if home_gap == 0:
        msg += f' {home} is on a back-to-back.'
    if away_gap == 0:
        msg += f' {away} is on a back-to-back.'
    if abs(home_gap-away_gap) >= 2:
        msg += ' Notable rest disparity.'
    return msg

def outdoor_weather(espn_event, start):
    """Only issue weather requests if ESPN explicitly confirms venue.indoor is False.

    Unknown venue/roof means NO outdoor forecast is presented as relevant.
    """
    competition = (espn_event.get('competitions') or [{}])[0]
    venue = competition.get('venue') or {}
    indoor = venue.get('indoor')
    if indoor is True:
        return 'Indoor venue confirmed by ESPN; outdoor wind and precipitation are not direct game factors.'
    if indoor is not False:
        return 'Venue exposure or retractable-roof status unverified; outdoor weather impact not assessed.'
    address = venue.get('address') or {}
    city = address.get('city')
    state = address.get('state')
    if not city:
        return 'Outdoor venue confirmed, but location is missing; game-time weather not verified.'
    try:
        geo = get_json('https://geocoding-api.open-meteo.com/v1/search',
                       {'name': city, 'count': 10, 'language': 'en', 'format': 'json', 'countryCode': 'US'})
        results = geo.get('results') or []
        # Prefer a state-matching result if available, but do not guess a distant city.
        matches = [r for r in results if not state or (r.get('admin1') or '').lower() == str(state).lower() or
                   (r.get('admin1_code') or '').lower() == str(state).lower()]
        loc = (matches or results)[0] if results else None
        if not loc:
            return f'Outdoor venue in {city}; forecast location not found, weather unverified.'
        forecast = get_json('https://api.open-meteo.com/v1/forecast', {
            'latitude': loc['latitude'], 'longitude': loc['longitude'],
            'hourly': 'temperature_2m,precipitation_probability,precipitation,wind_speed_10m,wind_gusts_10m,wind_direction_10m',
            'wind_speed_unit': 'mph', 'temperature_unit': 'fahrenheit', 'precipitation_unit': 'inch',
            'timezone': 'America/Los_Angeles', 'forecast_days': 7,
        })
        hourly = forecast.get('hourly') or {}
        times = hourly.get('time') or []
        local = start.astimezone(PT)
        target = local.strftime('%Y-%m-%dT%H:00')
        if target not in times:
            return 'Outdoor venue confirmed; game-time forecast outside available forecast window.'
        idx = times.index(target)
        def v(k):
            values = hourly.get(k) or []
            return values[idx] if idx < len(values) else None
        temp, wind, gust = v('temperature_2m'), v('wind_speed_10m'), v('wind_gusts_10m')
        direction, precip, chance = v('wind_direction_10m'), v('precipitation'), v('precipitation_probability')
        parts = [f'Outdoor game-time forecast near {city} ({local:%I:%M %p PT}):']
        if temp is not None: parts.append(f'{temp:.0f}°F')
        if wind is not None: parts.append(f'wind {wind:.0f} mph')
        if gust is not None: parts.append(f'gusts {gust:.0f} mph')
        if direction is not None: parts.append(f'wind from {direction:.0f}°')
        if chance is not None: parts.append(f'precipitation chance {chance:.0f}%')
        if precip is not None: parts.append(f'precipitation {precip:.2f} in')
        checked = datetime.now(PT).strftime('%Y-%m-%d %I:%M %p PT')
        return ', '.join(parts) + f'. Source: Open-Meteo forecast, checked {checked}; city-level, not stadium-measured. Roof status must be confirmed separately.'
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        logging.warning('Weather lookup failed for %s: %s', city, exc)
        return f'Outdoor venue confirmed in {city}; live game-time forecast unavailable, weather not verified.'

def research(espn_event, sport, home, away, recent, start, home_probability=None):
    hg = recent.get(norm(home), [])
    ag = recent.get(norm(away), [])
    why = [summarize(home, hg, sport), summarize(away, ag, sport)]
    if hg and ag:
        why.append(f'Last-{len(hg)} vs last-{len(ag)} sample: {home} scored {statistics.mean(x["scored"] for x in hg):.1f} per game; '
                   f'{away} allowed {statistics.mean(x["allowed"] for x in ag):.1f} per game. '
                   'Descriptive matchup comparison, not a causal prediction.')
    if home_probability is not None:
        why.append(f'Experimental independent record-strength estimate: {home} {home_probability:.1%} win probability. '
                   'Not backtested or calibrated.')
    watch = [rest_note(home, away, hg, ag, start)]
    watch.append('Current injuries, suspensions, confirmed starters and lineup changes have NOT been verified from official reports.')
    watch.append(outdoor_weather(espn_event, start))
    watch.append('Odds movement has not been independently verified; confirm current sportsbook price.')
    return why, watch
