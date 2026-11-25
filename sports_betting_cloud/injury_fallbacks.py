"""Bounded optional injury evidence. No database, odds calls or model inputs."""
import datetime as dt
import functools
import json
import logging
import os
import re
import urllib.request
import urllib.error

PRO_SPORTS = frozenset(("NFL", "NBA", "MLB", "NHL"))
SUMMARY_CAP = 5
PROPLINE_URLS = {
    "College Football": "https://api.prop-line.com/v1/sports/football_ncaaf/injuries",
    "College Basketball": "https://api.prop-line.com/v1/sports/basketball_ncaab/injuries",
}
# Shared circuit breaker across both college sports for this publisher process.
_propline_blocked = False
MAX_BYTES = 2 * 1024 * 1024
QUOTA_RESERVE = 100
MAX_AGE = dt.timedelta(hours=72)
STATUS_CODES = frozenset(("out", "out_first_half", "doubtful", "questionable",
                          "probable", "game_time_decision", "day_to_day", "injured_list", "suspended"))
from zoneinfo import ZoneInfo
PT = ZoneInfo("America/Los_Angeles")

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def clean(value, cap=180):
    return " ".join(value.split())[:cap] if isinstance(value, str) else ""

def norm(value):
    return re.sub(r"[^a-z0-9]", "", clean(value).lower())

def parse_timestamp(value):
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None

@functools.cache
def fetch_college(sport):
    """At most one attempt per supported sport, two total per process. Never retry.

    The primary workflow grants credentials only to first-attempt scheduled
    publishing runs; its pre-existing daily gate suppresses duplicate publishing.
    PropLine enforces its account-wide cap. Quota headers are available after
    the request, so this cannot pre-check other clients' usage.
    """
    global _propline_blocked
    if _propline_blocked or sport not in PROPLINE_URLS:
        return ()
    key = os.environ.get("PROPLINE_API_KEY", "").strip()
    if not key or os.environ.get("GITHUB_EVENT_NAME") != "schedule" or os.environ.get("GITHUB_RUN_ATTEMPT") != "1":
        return ()
    try:
        req = urllib.request.Request(PROPLINE_URLS[sport], headers={
            "X-API-Key": key, "Accept": "application/json",
            "User-Agent": "SportsEdge-availability/1.0"})
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=10) as response:
            limit = int(response.headers.get("X-Daily-Limit", "-1"))
            used = int(response.headers.get("X-Daily-Used", "-1"))
            remaining = int(response.headers.get("X-Daily-Remaining", "-1"))
            logging.info("PropLine quota: sport=%s limit=%d used=%d remaining=%d", sport, limit, used, remaining)
            if not (0 < limit <= 1000 and 0 <= used <= limit and remaining == limit-used and remaining >= QUOTA_RESERVE):
                _propline_blocked = True
                logging.warning("PropLine quota guard: discard optional data; stop both sports")
                return ()
            if remaining == QUOTA_RESERVE:
                _propline_blocked = True  # Accept this response, but never spend another request.
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            _propline_blocked = True
            return ()
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("players"), list):
            _propline_blocked = True
            return ()
        rows = tuple(row for row in data["players"] if isinstance(row, dict))
        logging.info("PropLine availability: sport=%s returned_rows=%d", sport, len(rows))
        return rows
    except urllib.error.HTTPError as exc:
        logging.warning("PropLine request stopped: HTTP %d; no retries for either sport", exc.code)
    except (OSError, ValueError, TypeError):
        logging.warning("PropLine request unavailable; no retries for either sport")
    _propline_blocked = True
    return ()

def conference_event_eligible(event, start, now):
    comp = (event.get("competitions") or [{}])[0]
    return (comp.get("conferenceCompetition") is True and
            start.tzinfo is not None and now.tzinfo is not None and
            dt.timedelta(0) < start-now <= MAX_AGE)

def college_reports(event, rows, start, now):
    """Exact school aliases; fresh official conference statuses only.

    Upstream rows lack an opponent/game ID. Treat them as team availability
    snapshots, not confirmed game-specific injury or lineup reports.
    """
    if not conference_event_eligible(event, start, now):
        return {}
    teams = [c.get("team") or {} for c in (event.get("competitions") or [{}])[0].get("competitors", [])]
    if len(teams) != 2:
        return {}
    aliases = {}
    for t in teams:
        name = clean(t.get("displayName"))
        if not name:
            return {}
        aliases[name] = {norm(t.get(k)) for k in ("displayName", "location", "shortDisplayName")} - {""}
    if len(aliases) != 2:
        return {}
    by_team = {name: {} for name in aliases}
    for row in rows:
        if row.get("source") != "conference_report" or row.get("status_code") not in STATUS_CODES:
            continue
        updated = parse_timestamp(row.get("source_updated_at"))
        if updated is None or not (now-MAX_AGE <= updated <= now) or updated < start-MAX_AGE:
            continue
        matches = [t for t in teams if norm(row.get("team")) in aliases[clean(t.get("displayName"))]]
        if len(matches) != 1:
            continue
        t = matches[0]
        source_abbr, team_abbr = norm(row.get("team_abbr")), norm(t.get("abbreviation"))
        if source_abbr and team_abbr and source_abbr != team_abbr:
            continue
        if norm(row.get("team")) != norm(t.get("displayName")) and not (source_abbr and source_abbr == team_abbr):
            continue
        player, status = clean(row.get("player_name"), 100), clean(row.get("status"), 60)
        if not player or not status:
            continue
        # The provider reports availability; do not infer a medical injury reason.
        source_time = updated.astimezone(PT).strftime("%Y-%m-%d %I:%M %p PT")
        note = f"Team availability report; source updated {source_time}; specific matchup not independently verified"
        entry = {"player": player, "status": status, "source": "PropLine official conference report",
                 "checked_at": now.astimezone(PT).strftime("%Y-%m-%d %I:%M %p PT"),
                 "note": note}
        group = by_team[clean(t.get("displayName"))]
        identity = norm(player)
        previous = group.get(identity)
        if previous is None or updated > previous[0]:
            group[identity] = (updated, entry, row.get("status_code"))
        elif updated == previous[0] and entry != previous[1]:
            # Conflicting same-time reports cannot be resolved by feed order.
            group[identity] = (updated, None, "")
    order = {"out": 0, "suspended": 0, "injured_list": 0, "out_first_half": 1,
             "doubtful": 2, "questionable": 3, "game_time_decision": 3,
             "day_to_day": 4, "probable": 5}
    return {team: [v[1] for v in sorted((v for v in group.values() if v[1] is not None),
                       key=lambda v: (order.get(v[2], 6), -v[0].timestamp(), v[1]["player"]))[:8]]
            for team, group in by_team.items()}

def supplement_college(sport, event, existing, home, away, start, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    out = {team: list((existing or {}).get(team) or [])[:8] for team in (home, away)}
    if sport not in PROPLINE_URLS or not conference_event_eligible(event, start, now):
        return out
    reports = college_reports(event, fetch_college(sport), start, now)
    for team in (home, away):
        # Primary ESPN evidence wins conflicts; never duplicate a player.
        seen = {norm(x.get("player")) for x in out[team]}
        for entry in reports.get(team, []):
            if len(out[team]) >= 8:
                break
            identity = norm(entry["player"])
            if identity not in seen:
                out[team].append(entry)
                seen.add(identity)
    return out
