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
                        'opponent_record': next((x.get('summary') for x in (competitors[1-i].get('records') or []) if x.get('type') == 'total'), None),
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
    """Show comparable, verified days off for both teams on every matchup."""
    def days_off(games):
        if not games:
            return None
        gap = (start.astimezone(PT).date() - games[0]['date'].astimezone(PT).date()).days - 1
        return gap if gap >= 0 else None
    h, a = days_off(home_games), days_off(away_games)
    h_text = f'{h} day(s) off' if h is not None else 'unavailable'
    a_text = f'{a} day(s) off' if a is not None else 'unavailable'
    parts = [f'Rest: {home} {h_text}; {away} {a_text}.']
    if h == 0:
        parts.append(f'{home} is on a back-to-back.')
    if a == 0:
        parts.append(f'{away} is on a back-to-back.')
    if h is not None and a is not None and abs(h-a) >= 2:
        parts.append(f'{home if h < a else away} has {abs(h-a)} fewer days off.')
    return ' '.join(parts)


def home_away_note(home, away, home_games, away_games):
    """Descriptive splits only when there is a useful sample for both sides."""
    h = [g for g in home_games if g.get('home') is True]
    a = [g for g in away_games if g.get('home') is False]
    if len(h) < 3 or len(a) < 3:
        return None
    return (f'Home/away form (recent available games): {home} at home '
            f'{sum(g["win"] for g in h)}-{len(h)-sum(g["win"] for g in h)} '
            f'({len(h)} games); {away} on road '
            f'{sum(g["win"] for g in a)}-{len(a)-sum(g["win"] for g in a)} '
            f'({len(a)} games). Small-sample descriptive splits.')


def road_trip_note(home, away, home_games, away_games):
    """Identify verified consecutive away games, not guessed travel mileage."""
    for name, games in ((home, home_games), (away, away_games)):
        if len(games) >= 2 and all(g.get('home') is False for g in games[:2]):
            return f'Travel context: {name} played its previous two games on the road; travel distance and time-zone impact not verified.'
    return None


def venue_weather_note(espn_event, sport, start):
    """Describe ESPN venue exposure and fetch forecast only for confirmed outdoor games.

    This does not infer roof-open status from stadium names or historical usage.
    Forecast is city-level, not a stadium sensor or a verified game-time observation.
    """
    competition = (espn_event.get('competitions') or [{}])[0]
    venue = competition.get('venue') or {}
    venue_name = str(venue.get('fullName') or venue.get('name') or 'Venue not specified').strip()
    indoor = venue.get('indoor')
    roof = str(venue.get('roofType') or venue.get('roof') or '').strip()
    prefix = f'Weather: Venue: {venue_name}. '
    if indoor is True:
        return prefix + ('ESPN identifies this venue as indoor. Outdoor weather is not expected to directly affect play. '
                         'Retractable-roof position is not independently verified.' if 'retract' in roof.lower() else
                         'ESPN identifies this venue as indoor; outdoor weather is not expected to directly affect play.')
    if indoor is not False:
        return prefix + 'ESPN has not confirmed whether the playing surface is outdoors. Forecast and roof status unverified.'

    address = venue.get('address') or {}
    city, state = address.get('city'), address.get('state')
    if not city:
        return prefix + 'ESPN identifies this venue as outdoor, but location details are missing; forecast unavailable.'
    # ESPN can return a state abbreviation, while Open-Meteo usually returns a full name.
    state_names = {'AL':'Alabama','AZ':'Arizona','AR':'Arkansas','CA':'California','CO':'Colorado',
                   'CT':'Connecticut','DC':'District of Columbia','FL':'Florida','GA':'Georgia',
                   'HI':'Hawaii','ID':'Idaho','IL':'Illinois','IN':'Indiana','IA':'Iowa',
                   'KS':'Kansas','KY':'Kentucky','LA':'Louisiana','ME':'Maine','MD':'Maryland',
                   'MA':'Massachusetts','MI':'Michigan','MN':'Minnesota','MS':'Mississippi',
                   'MO':'Missouri','MT':'Montana','NE':'Nebraska','NV':'Nevada','NH':'New Hampshire',
                   'NJ':'New Jersey','NM':'New Mexico','NY':'New York','NC':'North Carolina',
                   'ND':'North Dakota','OH':'Ohio','OK':'Oklahoma','OR':'Oregon','PA':'Pennsylvania',
                   'RI':'Rhode Island','SC':'South Carolina','SD':'South Dakota','TN':'Tennessee',
                   'TX':'Texas','UT':'Utah','VT':'Vermont','VA':'Virginia','WA':'Washington',
                   'WV':'West Virginia','WI':'Wisconsin','WY':'Wyoming'}
    state_full = state_names.get(str(state).upper(), str(state or '')).casefold()
    try:
        geo = get_json('https://geocoding-api.open-meteo.com/v1/search', {
            'name': city, 'count': 10, 'language': 'en', 'format': 'json', 'countryCode': 'US'})
        results = geo.get('results') or []
        matches = [r for r in results if not state_full or
                   str(r.get('admin1') or '').casefold() == state_full or
                   str(r.get('admin1_code') or '').casefold() == str(state).casefold()]
        loc = matches[0] if matches else None
        if loc is None:
            return prefix + 'ESPN confirms an outdoor venue; city/state could not be verified for forecasting.'
        forecast = get_json('https://api.open-meteo.com/v1/forecast', {
            'latitude': loc['latitude'], 'longitude': loc['longitude'],
            'hourly': 'temperature_2m,precipitation_probability,precipitation,wind_speed_10m,wind_gusts_10m,weather_code',
            'wind_speed_unit': 'mph', 'temperature_unit': 'fahrenheit',
            'precipitation_unit': 'inch', 'timezone': 'America/Los_Angeles', 'forecast_days': 7})
        hourly = forecast.get('hourly') or {}
        target = start.astimezone(PT).strftime('%Y-%m-%dT%H:00')
        times = hourly.get('time') or []
        if target not in times:
            return prefix + 'ESPN confirms an outdoor venue; kickoff forecast is outside the available forecast window.'
        idx = times.index(target)
        def value(key):
            values = hourly.get(key) or []
            return values[idx] if idx < len(values) else None
        temp, wind, gust = value('temperature_2m'), value('wind_speed_10m'), value('wind_gusts_10m')
        chance, precip, code = value('precipitation_probability'), value('precipitation'), value('weather_code')
        descriptions = {0:'clear sky',1:'mainly clear',2:'partly cloudy',3:'overcast',
                        45:'fog',48:'freezing fog',51:'light drizzle',53:'drizzle',55:'dense drizzle',
                        61:'light rain',63:'rain',65:'heavy rain',71:'light snow',73:'snow',75:'heavy snow',
                        80:'rain showers',81:'rain showers',82:'heavy rain showers',95:'thunderstorms'}
        parts = [f'Outdoor kickoff forecast near {city} ({start.astimezone(PT):%I:%M %p PT})']
        if code is not None:
            parts.append(descriptions.get(int(code), 'conditions not classified'))
        if temp is not None: parts.append(f'{temp:.0f}°F')
        if wind is not None: parts.append(f'wind {wind:.0f} mph')
        if gust is not None: parts.append(f'gusts {gust:.0f} mph')
        if chance is not None: parts.append(f'precipitation chance {chance:.0f}%')
        if precip is not None: parts.append(f'precipitation {precip:.2f} in')
        return prefix + ', '.join(parts) + '. Open-Meteo city-level forecast, not a live observation; roof status may change.'
    except (requests.RequestException, ValueError, KeyError, TypeError, OverflowError) as exc:
        logging.warning('Weather lookup failed for %s: %s', city, exc)
        return prefix + 'ESPN confirms an outdoor venue, but the weather forecast could not be retrieved.'


def outdoor_weather(espn_event, start):
    """Backward-compatible weather helper for existing callers."""
    return venue_weather_note(espn_event, 'NFL', start)



# ESPN's public injury feeds are informational and may be incomplete or delayed.
# One league-level request per sport per refresh, with no Odds API calls.
ESPN_INJURY_LEAGUES = {
    'NFL': ('football', 'nfl'), 'NBA': ('basketball', 'nba'),
    'MLB': ('baseball', 'mlb'), 'NHL': ('hockey', 'nhl'),
    'College Football': ('football', 'college-football'),
    'College Basketball': ('basketball', 'mens-college-basketball'),
}


def _injury_text(value):
    """Extract a human-readable ESPN field without turning dictionaries into text."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ('displayName', 'fullName', 'name', 'description', 'abbreviation', 'text', 'label'):
            found = _injury_text(value.get(key))
            if found:
                return found
    return ''


def _injury_team(value):
    if not isinstance(value, dict):
        return None
    candidate = value.get('team')
    if isinstance(candidate, dict):
        return candidate
    if isinstance(value.get('displayName'), str) and (value.get('id') is not None or value.get('abbreviation')):
        return value
    return None


def fetch_espn_injuries(sport):
    """Retrieve player-level injuries with conservative nested-schema handling.

    An HTTP success is not proof that the feed contains injuries. Missing or
    unrecognized data is reported as unavailable, never as a clean bill of health.
    """
    if sport not in ESPN_INJURY_LEAGUES:
        return None
    domain, league = ESPN_INJURY_LEAGUES[sport]
    url = f'https://site.api.espn.com/apis/site/v2/sports/{domain}/{league}/injuries'
    try:
        data = get_json(url)
    except (requests.RequestException, ValueError, TypeError) as exc:
        logging.warning('Injury feed request failed: sport=%s error=%s', sport, exc)
        return None
    if not isinstance(data, dict):
        logging.warning('Injury feed unexpected response: sport=%s type=%s', sport, type(data).__name__)
        return None
    raw = data.get('injuries')
    if not isinstance(raw, list):
        logging.warning('Injury feed unexpected schema: sport=%s top_keys=%s', sport, list(data)[:12])
        return None
    checked = datetime.now(PT).strftime('%Y-%m-%d %I:%M %p PT')
    reports = {}
    counters = {'nodes': 0, 'athletes': 0, 'missing_team': 0, 'missing_status': 0, 'accepted': 0}
    sample_keys = []
    seen = set()

    def walk(node, team=None, depth=0):
        if depth > 9 or counters['nodes'] > 20000:
            return
        if isinstance(node, list):
            for child in node:
                walk(child, team, depth + 1)
            return
        if not isinstance(node, dict):
            return
        counters['nodes'] += 1
        if len(sample_keys) < 5:
            sample_keys.append(sorted(node.keys())[:20])
        own_team = _injury_team(node)
        if own_team:
            team = own_team
        athlete = node.get('athlete') or node.get('player')
        # Only accept actual athlete-bearing records, not team or metadata nodes.
        if athlete is not None:
            counters['athletes'] += 1
            player = _injury_text(athlete)
            status = _injury_text(node.get('status') or node.get('designation') or node.get('injuryStatus'))
            injury = node.get('injury')
            if not status and isinstance(injury, dict):
                status = _injury_text(injury.get('status') or injury.get('designation'))
            if not status:
                status = _injury_text(node.get('type')) if isinstance(node.get('type'), dict) else ''
            team_id = str((team or {}).get('id') or '').strip()
            team_name = _injury_text((team or {}).get('displayName') or (team or {}).get('name'))
            if not (team_id or team_name):
                counters['missing_team'] += 1
            elif not status:
                counters['missing_status'] += 1
            elif player:
                detail = _injury_text(injury.get('type') or injury.get('description')) if isinstance(injury, dict) else _injury_text(injury)
                comment = _injury_text(node.get('shortComment') or node.get('longComment') or node.get('details'))
                note = ', '.join(x for x in (detail, comment) if x)
                identity = (team_id, team_name, player, status, note)
                if identity not in seen:
                    seen.add(identity)
                    item = {'player': player, 'status': status, 'source': 'ESPN injury feed',
                            'checked_at': checked, 'note': note[:180]}
                    keys = ([f'id:{team_id}'] if team_id else []) + ([f'name:{norm(team_name)}'] if team_name else [])
                    for key in keys:
                        reports.setdefault(key, []).append(item)
                    counters['accepted'] += 1
        # ESPN may nest entries under injuries, items, entries, or another list.
        # Only descend into containers; don't mistake athlete/team metadata for reports.
        for key, value in node.items():
            if key in ('athlete', 'player', 'team', 'injury', 'status', 'designation', 'details'):
                continue
            if isinstance(value, list) and (key in ('injuries', 'items', 'entries', 'athletes', 'players', 'reports', 'data') or
                                            any(isinstance(v, dict) and ('athlete' in v or 'player' in v or 'injuries' in v or 'items' in v) for v in value[:3])):
                walk(value, team, depth + 1)
            elif isinstance(value, dict) and key in ('injuries', 'items', 'entries', 'data', 'report', 'content'):
                walk(value, team, depth + 1)

    walk(raw)
    logging.info('Injury feed: sport=%s groups=%s parsed_records=%s teams=%s athlete_nodes=%s missing_team=%s missing_status=%s checked=%s',
                 sport, len(raw), counters['accepted'], len(reports), counters['athletes'],
                 counters['missing_team'], counters['missing_status'], checked)
    if counters['accepted'] == 0:
        logging.warning('Injury feed parsed zero player reports: sport=%s sample_node_keys=%s', sport, sample_keys)
    return reports


def injury_reports_for_event(espn_event, league_reports, home, away):
    """Match injury reports to both teams using ESPN IDs; never fuzzy-guess."""
    if league_reports is None:
        return None
    competitors = ((espn_event.get('competitions') or [{}])[0].get('competitors') or [])
    matched = {}
    for c in competitors:
        t = c.get('team') or {}
        name = str(t.get('displayName') or '').strip()
        tid = str(t.get('id') or '').strip()
        if norm(name) not in (norm(home), norm(away)):
            continue
        entries = league_reports.get(f'id:{tid}') if tid else None
        if entries is None:
            entries = league_reports.get(f'name:{norm(name)}', [])
        matched[name] = entries
    return matched

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
    why = [f'Recent form: {summarize(home, hg, sport)}',
           f'Recent form: {summarize(away, ag, sport)}']
    if hg and ag:
        home_scored = statistics.mean(g['scored'] for g in hg)
        away_allowed = statistics.mean(g['allowed'] for g in ag)
        away_scored = statistics.mean(g['scored'] for g in ag)
        home_allowed = statistics.mean(g['allowed'] for g in hg)
        why.append(f'Offensive vs. defensive matchup: {home} scores {home_scored:.1f} per game recently '
                   f'vs. {away} allowing {away_allowed:.1f}; {away} scores {away_scored:.1f} '
                   f'vs. {home} allowing {home_allowed:.1f}. Descriptive last-{len(hg)} / last-{len(ag)} samples.')
    splits = home_away_note(home, away, hg, ag)
    why.append('Home/away performance: ' + (splits or
               'Insufficient recent home/road games for a comparable split (minimum three per side).'))

    # Opponent W-L snapshots are only used if ESPN supplies them in completed-game records.
    def schedule_strength(games):
        vals = []
        for g in games:
            record = str(g.get('opponent_record') or '')
            match = re.fullmatch(r'(\d+)-(\d+)(?:-\d+)?', record)
            if match:
                w, l = int(match.group(1)), int(match.group(2))
                if w + l:
                    vals.append(w / (w + l))
        return vals
    home_opp, away_opp = schedule_strength(hg), schedule_strength(ag)
    if home_opp and away_opp:
        why.append(f'Strength of schedule: Recent opponents had average recorded win percentages '
                   f'of {statistics.mean(home_opp):.3f} for {home} ({len(home_opp)} opponents) '
                   f'and {statistics.mean(away_opp):.3f} for {away} ({len(away_opp)} opponents). '
                   'ESPN game-time record snapshots; not an adjusted strength-of-schedule rating.')
    else:
        why.append('Strength of schedule: Comparable opponent records were not available '
                   'from the recorded ESPN game results.')

    # ESPN scoreboard competitors sometimes provide season-to-date overall W-L.
    competitors = (espn_event.get('competitions') or [{}])[0].get('competitors') or []
    def season_record(team):
        for c in competitors:
            if norm((c.get('team') or {}).get('displayName')) == norm(team):
                for rec in c.get('records') or []:
                    if rec.get('type') == 'total' and rec.get('summary'):
                        return rec['summary']
        return None
    hrec, arec = season_record(home), season_record(away)
    if hrec or arec:
        why.append(f'Season-long performance: Overall records reported in the ESPN matchup: '
                   f'{home} {hrec or "unavailable"}; {away} {arec or "unavailable"}. '
                   'Season scoring averages and league rankings not verified.')
    else:
        why.append('Season-long performance: Overall season records and league rankings '
                   'not available in this matchup snapshot.')
    watch = []
    rest = rest_note(home, away, hg, ag, start)
    if rest:
        watch.append(rest)
    road = road_trip_note(home, away, hg, ag)
    if road:
        watch.append(road)
    injuries = verified_injury_notes(injury_reports, home, away)
    watch.extend(injuries if injuries else ['Injury report unavailable; player availability not verified.'])
    if (sport or '').upper() in WEATHER_SPORTS:
        watch.append(venue_weather_note(espn_event, sport, start))
    return why, watch
