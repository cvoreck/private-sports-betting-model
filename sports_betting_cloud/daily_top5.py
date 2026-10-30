"""Daily sports card publisher. Requires verified event eligibility and independent model probabilities.
No synthetic picks, no guessed rankings, no-vig market probability is NOT treated as a model.
"""
import os
import sys
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
import requests

import matchup_research as mr

PT = ZoneInfo('America/Los_Angeles')
SPORT_KEYS = {
    'americanfootball_nfl': 'NFL', 'basketball_nba': 'NBA',
    'baseball_mlb': 'MLB', 'americanfootball_ncaaf': 'College Football',
    'icehockey_nhl': 'NHL', 'basketball_ncaab': 'College Basketball',
}
COLLEGE = {'College Football', 'College Basketball'}
# Maximum age of the prediction at publication. This is NOT a freshness
# guarantee for cached sportsbook prices, which are timestamped separately.
MAX_PREDICTION_AGE_HOURS = 3
EXPERIMENTAL_PREFIX = 'archive-market-shrink-v1-experimental'
TRAINED_PREFIXES = ('rolling-homewin-v2-', 'nhl-context-v3-')
MIN_STANDARD_WIN_PROB = 0.50  # Presentation filter, not validated profitability
MAX_EXPERIMENTAL_UNITS = 2

ODDS_URL = 'https://api.the-odds-api.com/v4/sports/{sport}/odds/'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS prediction_context (event_id TEXT PRIMARY KEY, home_team TEXT, away_team TEXT, home_logo TEXT, away_logo TEXT, rationale TEXT, warnings TEXT, updated_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS model_predictions (
  event_id TEXT NOT NULL, market TEXT NOT NULL, selection TEXT NOT NULL,
  point_key TEXT NOT NULL DEFAULT '', estimated_prob DOUBLE PRECISION NOT NULL CHECK (estimated_prob > 0 AND estimated_prob < 1),
  generated_at TIMESTAMPTZ NOT NULL, model_version TEXT NOT NULL,
  PRIMARY KEY (event_id, market, selection, point_key)
);
CREATE TABLE IF NOT EXISTS verified_events (
  event_id TEXT PRIMARY KEY, sport TEXT NOT NULL,
  is_preseason BOOLEAN NOT NULL,
  ap_top25_team_in_match BOOLEAN,
  verified_at TIMESTAMPTZ NOT NULL,
  verification_source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_card_runs (
  card_date DATE PRIMARY KEY, status TEXT NOT NULL,
  candidates INTEGER NOT NULL DEFAULT 0, published INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), detail TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS picks_automated_daily_rank
 ON picks (date, kind, rank)
 WHERE kind='Hypothetical' AND rank IS NOT NULL AND source LIKE 'Auto:%%';
'''

def american_break_even(odds):
    return 100 / (odds + 100) if odds > 0 else abs(odds) / (abs(odds) + 100)

def expected_value(p, odds):
    profit_per_unit = odds / 100 if odds > 0 else 100 / abs(odds)
    return p * profit_per_unit - (1 - p)

def suggested_units(edge_pp):
    """Experimental 1u–5u stake tier from *unrounded* estimated edge (pp).

    This is a research label, not a validated bankroll recommendation.
    A pick's assigned stake is frozen at publication for correct settlement.
    """
    if not isinstance(edge_pp, (int, float)) or not math.isfinite(edge_pp):
        raise ValueError('Suggested units require a finite estimated edge')
    if edge_pp >= 15.0:
        return 5
    if edge_pp >= 10.0:
        return 4
    if edge_pp >= 6.0:
        return 3
    if edge_pp >= 3.0:
        return 2
    return 1


def odds_events(conn, key, today):
    row=conn.execute('''SELECT payload FROM historical_snapshots
      WHERE source='the-odds-api' AND sport=%s AND category='daily_odds_cache'
        AND item_key=%s ORDER BY collected_at DESC LIMIT 1''',
        (key,today.isoformat())).fetchone()
    return (row['payload'].get('events') or []) if row else []

def find_candidates(conn, today, now):
    candidates = []
    counts = dict(events=0, upcoming_today=0, verified=0, predictions=0,
                  positive_ev=0, below_probability_floor=0, stale_or_unapproved=0, trained=0, experimental=0)
    # Active promoted versions are authoritative. Reject stale superseded
    # moneyline predictions, even when their generated_at is recent.
    promoted = {row['sport']: row['version'] for row in conn.execute(
        'SELECT sport, version FROM sports_edge_trained_models').fetchall()}
    for key, sport in SPORT_KEYS.items():
        try:
            events = odds_events(conn, key, today)
        except requests.HTTPError as exc:
            # Missing subscription coverage is visible in logs, not silently invented.
            logging.warning('Odds unavailable for %s: %s', sport, exc)
            continue
        logging.info('%s: read %s cached odds events', sport, len(events))
        counts['events'] += len(events)
        for event in events:
            event_id = event.get('id')
            try:
                start = datetime.fromisoformat(event['commence_time'].replace('Z', '+00:00'))
            except (ValueError, KeyError):
                continue
            if start.astimezone(PT).date() != today or start <= now:
                continue
            counts['upcoming_today'] += 1
            verified = conn.execute('''SELECT * FROM verified_events WHERE event_id=%s
              AND sport=%s AND is_preseason=FALSE AND verified_at >= %s''',
              (event_id, sport, now - timedelta(days=7))).fetchone()
            if not verified:
                continue
            if sport in COLLEGE and verified['ap_top25_team_in_match'] is not True:
                continue
            counts['verified'] += 1
            matchup = f"{event.get('away_team','Away')} @ {event.get('home_team','Home')}"
            context = conn.execute('SELECT * FROM prediction_context WHERE event_id=%s', (event_id,)).fetchone()
            for bookmaker in event.get('bookmakers', []):
                for market in bookmaker.get('markets', []):
                    mkey = market.get('key')
                    if mkey not in ('h2h', 'spreads', 'totals'):
                        continue
                    for outcome in market.get('outcomes', []):
                        odds = outcome.get('price')
                        if not isinstance(odds, (int, float)) or (-100 < odds < 100):
                            continue
                        selection = outcome.get('name')
                        point = outcome.get('point')
                        prediction = conn.execute('''SELECT * FROM model_predictions
                          WHERE event_id=%s AND market=%s AND selection=%s
                            AND point_key=%s
                            AND generated_at >= %s''',
                          (event_id, mkey, selection, str(point) if point is not None else '', now - timedelta(hours=MAX_PREDICTION_AGE_HOURS))).fetchone()
                        if not prediction:
                            continue
                        counts['predictions'] += 1
                        version = prediction['model_version']
                        if (mkey == 'h2h' and version == promoted.get(key)
                                and version.startswith(TRAINED_PREFIXES)):
                            quality = 'trained'
                        elif (mkey in ('spreads', 'totals')
                              and version == EXPERIMENTAL_PREFIX):
                            quality = 'experimental'
                        else:
                            counts['stale_or_unapproved'] += 1
                            continue
                        p = prediction['estimated_prob']
                        if not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 < p < 1:
                            continue
                        ev = expected_value(p, odds)
                        if ev <= 0:
                            continue
                        counts['positive_ev'] += 1
                        if p < MIN_STANDARD_WIN_PROB:
                            counts['below_probability_floor'] += 1
                            continue
                        counts[quality] += 1
                        candidates.append({
                            'quality': quality,
                            'event_id': event_id, 'sport': sport, 'matchup': matchup,
                            'market': mkey, 'selection': selection, 'point': point,
                            'odds': int(odds), 'prob': p, 'edge_pp': 100*(p-american_break_even(odds)),
                            'ev': ev, 'start': start, 'bookmaker': bookmaker.get('title','Unknown'),
                            'model_version': prediction['model_version'],
                            'verification_source': verified['verification_source'],
                            'context': dict(context) if context else {}
                        })
    logging.info('Candidate funnel: %s', json.dumps(counts, sort_keys=True))
    return candidates

def rank_candidates(candidates, card_date=None):
    """Choose a varied Top 5, with modest game-day preferences.

    Selection bonuses never change model probabilities, odds, EV or the
    confidence order displayed on the published card.
    """
    card_date = card_date or datetime.now(PT).date()
    weekday = card_date.weekday()  # Monday=0, Sunday=6
    preferred_sport = ('NFL' if weekday in (0, 3, 6) else
                       'College Football' if weekday in (4, 5) else None)

    # Keep the best available sportsbook quote for each distinct bet.
    best = {}
    for c in candidates:
        k = (c['event_id'], c['market'], c['selection'], str(c['point']))
        if k not in best or c['ev'] > best[k]['ev']:
            best[k] = c

    def score(c):
        # Prefer trained models at comparable positive EV, without giving an
        # unvalidated heuristic an equal claim to probability accuracy.
        # Moderate-odds preference is a tie breaker, not an EV substitute.
        return (c['quality'] == 'trained', c['prob'], c['ev'], -abs(c['odds']))

    pool = [c for c in best.values() if c['prob'] >= MIN_STANDARD_WIN_PROB and c['ev'] > 0]
    selected = []
    used_events = set()
    sport_counts = {}
    while len(selected) < 5:
        eligible = [c for c in pool if c['event_id'] not in used_events]
        if not eligible:
            break
        strongest = max(eligible, key=score)
        # Only compare candidates of the same evidence tier; a sport-mix
        # preference must never elevate an experimental pick over a trained one.
        comparable = [c for c in eligible
                      if c['quality'] == strongest['quality']
                      and c['prob'] >= strongest['prob'] - 0.025]
        available_sports = {c['sport'] for c in comparable}
        selected_sports = set(sport_counts)
        # Aim for at least two sports when similarly strong options exist.
        if len(selected_sports) == 1 and len(selected) >= 2:
            diverse = [c for c in comparable if c['sport'] not in selected_sports]
            if diverse:
                comparable = diverse
        # Soft three-per-sport cap; allow more when alternatives are weak.
        under_cap = [c for c in comparable if sport_counts.get(c['sport'], 0) < 3]
        if under_cap:
            comparable = under_cap

        def selection_score(c):
            bonus = 0.01 if c['sport'] == preferred_sport else 0.0
            variety = 0.005 if c['sport'] not in selected_sports else 0.0
            return c['prob'] + bonus + variety

        chosen = max(comparable, key=selection_score)
        selected.append(chosen)
        used_events.add(chosen['event_id'])
        sport_counts[chosen['sport']] = sport_counts.get(chosen['sport'], 0) + 1

    # Ranks reflect evidence tier and estimated win probability, not calibrated
    # confidence. These are research-only selections.
    selected.sort(key=score, reverse=True)
    logging.info('Top 5 selection: day=%s preferred=%s sport_mix=%s',
                 card_date, preferred_sport, json.dumps(sport_counts, sort_keys=True))
    return selected


def research_notes(context, selection, market, model_version=None, edge_pp=None, ev=None, bookmaker=None, verification_source=None, quality=None):
    # Prefix each line to preserve the separate dashboard columns.
    ctx = context or {}
    why = [x.strip() for x in (ctx.get('rationale') or '').splitlines() if x.strip()]
    risks = [x.strip() for x in (ctx.get('warnings') or '').splitlines() if x.strip()]
    if not why:
        why = ['Recent-game evidence is not available for this matchup.']
    if not risks:
        risks = ['Injury, lineup and venue-specific weather checks are unavailable.']
    if quality == 'experimental' or market in ('spreads', 'totals'):
        why.append('MODEL QUALITY: EXPERIMENTAL spread/total heuristic; not historically validated or calibrated; market-anchored probability.')
        risks.append('Estimated edge and EV are unvalidated research estimates, not proven sportsbook advantages.')
    else:
        why.append('MODEL QUALITY: TRAINED moneyline; promoted after chronological holdout evaluation, not independently betting-validated.')
    if model_version and edge_pp is not None and ev is not None:
        why.append(f'Model {model_version}: estimated edge {edge_pp:+.1f} pp; estimated EV {ev:+.3f} units per unit at {bookmaker or "listed sportsbook"}.')
    image = (ctx.get('home_logo') if selection == ctx.get('home_team') else
             ctx.get('away_logo') if selection == ctx.get('away_team') else ctx.get('home_logo')) or ''
    lines = [f'Why: {x}' for x in why] + [f'Watch out for: {x}' for x in risks]
    if image:
        lines.append(f'Image URL: {image}')
    if verification_source:
        lines.append(f'Source: {verification_source}')
    return '\n'.join(lines)

# ESPN scoreboard endpoints used to research picks already saved in the ledger.
ESPN_LEAGUES = {
    'NFL': ('football', 'nfl'), 'NBA': ('basketball', 'nba'),
    'MLB': ('baseball', 'mlb'), 'NHL': ('hockey', 'nhl'),
    'College Football': ('football', 'college-football'),
    'College Basketball': ('basketball', 'mens-college-basketball'),
}


def _espn_event_teams(event):
    comps = (event.get('competitions') or [{}])[0].get('competitors') or []
    home = next((c for c in comps if c.get('homeAway') == 'home'), None)
    away = next((c for c in comps if c.get('homeAway') == 'away'), None)
    if not home or not away:
        return None
    return ((home.get('team') or {}).get('displayName') or '',
            (away.get('team') or {}).get('displayName') or '')


def _fetch_research_events(sport, day):
    domain, league = ESPN_LEAGUES[sport]
    url = f'https://site.api.espn.com/apis/site/v2/sports/{domain}/{league}/scoreboard'
    board = mr.get_json(url, {'dates': day.strftime('%Y%m%d'), 'limit': 200})
    return board.get('events') or []


def _match_saved_event(row, events):
    # Match both teams and the exact home/away orientation; do not guess a game.
    saved = row.get('matchup') or ''
    if ' @ ' not in saved:
        return None
    away, home = saved.rsplit(' @ ', 1)
    matches = []
    for event in events:
        teams = _espn_event_teams(event)
        if teams and mr.norm(teams[0]) == mr.norm(home) and mr.norm(teams[1]) == mr.norm(away):
            matches.append(event)
    return matches[0] if len(matches) == 1 else None


def _existing_note_lines(notes, prefix):
    return [line[len(prefix):].strip() for line in (notes or '').splitlines()
            if line.startswith(prefix) and line[len(prefix):].strip()]


def refresh_existing_card_research(conn, today):
    """Research existing picks from ESPN by matchup, without changing bet details.

    Previous code only joined prediction_context rows generated for the *current*
    event pool. A saved NHL pick could therefore never receive research if the
    prediction engine happened to process only NBA games in a later run.
    """
    rows = conn.execute("""SELECT id, sport, matchup, selection, market, notes FROM picks
      WHERE date=%s AND kind='Hypothetical' AND rank IS NOT NULL AND source LIKE 'Auto:%%'""",
      (today.isoformat(),)).fetchall()
    if not rows:
        return 0

    by_sport = {}
    for row in rows:
        by_sport.setdefault(row['sport'], []).append(dict(row))

    updated = matched = missing = 0
    for sport, sport_rows in by_sport.items():
        if sport not in ESPN_LEAGUES:
            logging.warning('Research: unsupported sport %s (%s saved picks)', sport, len(sport_rows))
            missing += len(sport_rows)
            continue
        domain, league = ESPN_LEAGUES[sport]
        try:
            events = _fetch_research_events(sport, today)
        except (requests.RequestException, ValueError, KeyError) as exc:
            logging.warning('Research: ESPN scoreboard unavailable for %s: %s', sport, exc)
            missing += len(sport_rows)
            continue
        logging.info('Research: %s ESPN scoreboard events=%s; saved picks=%s',
                     sport, len(events), len(sport_rows))
        found = [(row, _match_saved_event(row, events)) for row in sport_rows]
        found = [(row, event) for row, event in found if event is not None]
        missing += len(sport_rows) - len(found)
        if not found:
            logging.warning('Research: no saved %s matchups matched ESPN scoreboard; preserving old notes', sport)
            continue
        try:
            recent = mr.completed_games(domain, league, today, lookback=24)
        except Exception as exc:
            logging.warning('Research: recent-game retrieval failed for %s: %s', sport, exc)
            recent = {}
        for row, event in found:
            matched += 1
            home, away = _espn_event_teams(event)
            try:
                start = datetime.fromisoformat(event['date'].replace('Z', '+00:00'))
                why, risks = mr.research(event, sport, home, away, recent, start)
            except (ValueError, KeyError, TypeError, requests.RequestException) as exc:
                logging.warning('Research: failed to analyze %s: %s', row['matchup'], exc)
                continue
            # Preserve previously saved image URLs and metadata, if present.
            old = row.get('notes') or ''
            # Keep model provenance, estimated EV, uncertainty labels and
            # source URLs. Refresh only matchup-specific research lines.
            extra = [line for line in old.splitlines()
                     if line.startswith(('Image URL:', 'Source:'))
                     or ('MODEL QUALITY:' in line)
                     or ('estimated edge' in line)
                     or ('unvalidated research estimates' in line)
                     or ('not historically validated' in line)]
            notes = '\n'.join([f'Why: {x}' for x in why] +
                              [f'Watch out for: {x}' for x in risks] + extra)
            if notes != old:
                conn.execute('UPDATE picks SET notes=%s WHERE id=%s', (notes, row['id']))
                updated += 1
            logging.info('Research: matched %s; recent games home=%s away=%s; updated=%s',
                         row['matchup'], len(recent.get(mr.norm(home), [])),
                         len(recent.get(mr.norm(away), [])), notes != old)
    logging.info('Research refresh summary: saved=%s matched=%s updated=%s unmatched=%s',
                 len(rows), matched, updated, missing)
    return updated

def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    now = datetime.now(timezone.utc)
    local = now.astimezone(PT)
    if os.environ.get('REQUIRE_SIX_AM', '0') == '1' and local.hour != 6:
        logging.info('Not 06:00 Pacific; exiting (%s). Set REQUIRE_SIX_AM=0 for a manual run.', local.isoformat())
        return
    today = local.date()
    database_url = os.environ['DATABASE_URL']
    
    with psycopg.connect(database_url, row_factory=dict_row, connect_timeout=15) as conn:
        with conn.transaction():
            # Don't create picks table here: existing app/storage.py owns its schema.
            conn.execute(SCHEMA)
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (20261027,))
            existing = conn.execute('''SELECT count(*) AS n FROM picks
              WHERE date=%s AND kind='Hypothetical' AND rank IS NOT NULL''', (today.isoformat(),)).fetchone()['n']
            if existing:
                refreshed = refresh_existing_card_research(conn, today)
                logging.info('Already have %s ranked hypothetical picks for %s; refreshed research on %s, preserved picks', existing, today, refreshed)
                return
        candidates = find_candidates(conn, today, now)
        top = rank_candidates(candidates, today)
        status = ('published' if len(top) == 5 else
                  'partial_card' if top else 'insufficient_verified_candidates')
        # Publish only independently modeled positive-EV picks; fewer than five is allowed.
        with conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (20261027,))
            existing = conn.execute('''SELECT count(*) AS n FROM picks
              WHERE date=%s AND kind='Hypothetical' AND rank IS NOT NULL''', (today.isoformat(),)).fetchone()['n']
            if existing:
                logging.info('Another run published; skipping')
                return
            if top:
                for rank, c in enumerate(top, 1):
                    pick_label = c['selection']
                    if c['point'] is not None:
                        pick_label += f" ({c['point']:+g})" if isinstance(c['point'], (int,float)) else f" ({c['point']})"
                    ctx = c.get('context') or {}
                    notes = research_notes(ctx, c['selection'], c['market'], c['model_version'],
                                           c['edge_pp'], c['ev'], c['bookmaker'], c['verification_source'], c['quality'])
                    raw_tier = suggested_units(c['edge_pp'])
                    units = min(raw_tier, MAX_EXPERIMENTAL_UNITS)
                    notes += (f'\nWhy: EXPERIMENTAL STAKE TIER: {units}u (raw edge tier {raw_tier}u, capped at 2u) from '
                              f'{c["edge_pp"]:+.2f} percentage-point estimated edge; '
                              'uncalibrated, not a validated bankroll recommendation.')
                    conn.execute('''INSERT INTO picks
                      (date,sport,matchup,market,selection,odds,stake,kind,result,source,
                       notes,rank,start_pt,estimated_prob)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,'Hypothetical','Pending',%s,%s,%s,%s,%s)''',
                      (today.isoformat(),c['sport'],c['matchup'],c['market'],pick_label,c['odds'],units,
                       f"Auto: The Odds API / {c['bookmaker']}",notes,rank,
                       c['start'].astimezone(PT).strftime('%Y-%m-%d %I:%M %p PT'),c['prob']))
                    logging.info('Published rank=%s selection=%s edge_pp=%.3f suggested_units=%su',
                                 rank, pick_label, c['edge_pp'], units)
            conn.execute('''INSERT INTO daily_card_runs (card_date,status,candidates,published,detail)
              VALUES (%s,%s,%s,%s,%s) ON CONFLICT(card_date) DO UPDATE SET
              status=EXCLUDED.status,candidates=EXCLUDED.candidates,published=EXCLUDED.published,
              detail=EXCLUDED.detail,updated_at=NOW()''',
              (today,status,len(candidates),len(top),
               'Standard card requires model win probability >=50% and positive estimated EV; trained moneylines ranked first, then win probability. Experimental stakes capped at 2u. Fewer than five picks allowed; no props or injury/weather checks.'))
        logging.info('%s: %s; candidates=%s; published=%s', today,status,len(candidates),len(top))

if __name__ == '__main__':
    try:
        main()
    except Exception:
        logging.exception('Daily card failed')
        sys.exit(1)
