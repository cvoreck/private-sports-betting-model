"""Conservative ESPN/Odds event matching; never guesses from mascot alone."""
import re
import unicodedata
from datetime import datetime, timezone


def norm(value):
    text = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode('ascii').lower()
    text = re.sub(r"\bst[.]?\b", 'state', text) if False else text
    return re.sub(r'[^a-z0-9]+', '', text)


def team_keys(team):
    """Only complete team names; short mascot-only ESPN names are excluded."""
    names = [team.get('displayName'), team.get('shortDisplayName')]
    location, mascot = team.get('location'), team.get('name')
    if location and mascot:
        names.append(f'{location} {mascot}')
    return {norm(name) for name in names if name and len(norm(name)) >= 7}


def _start(event):
    try:
        return datetime.fromisoformat(event['date'].replace('Z', '+00:00')).astimezone(timezone.utc)
    except (KeyError, ValueError, TypeError):
        return None


def build_index(events):
    """Deduplicate repeated scoreboard days by ESPN event ID."""
    index = {}
    seen = set()
    for event in events:
        event_id = str(event.get('id') or '')
        if not event_id or event_id in seen:
            continue
        seen.add(event_id)
        comps = (event.get('competitions') or [{}])[0].get('competitors') or []
        home = next((c for c in comps if c.get('homeAway') == 'home'), None)
        away = next((c for c in comps if c.get('homeAway') == 'away'), None)
        if not home or not away:
            continue
        for hk in team_keys(home.get('team') or {}):
            for ak in team_keys(away.get('team') or {}):
                index.setdefault((hk, ak), []).append((event, home, away))
    return index


def lookup(index, home, away, start=None, tolerance_hours=4):
    """Return a match only when one distinct ESPN event fits orientation/time."""
    matches = index.get((norm(home), norm(away)), [])
    if start is not None:
        matches = [m for m in matches if _start(m[0]) is not None
                   and abs((_start(m[0]) - start).total_seconds()) <= tolerance_hours * 3600]
    unique = {str(m[0].get('id')): m for m in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def diagnose(index, home, away, start=None):
    """Classification for logs only, never relaxes the selection standard."""
    exact = index.get((norm(home), norm(away)), [])
    if not exact:
        return 'team_names_not_found_in_espn_scoreboards'
    if start is not None and not lookup(index, home, away, start):
        return 'start_time_mismatch_or_ambiguous'
    return 'ambiguous_event'
