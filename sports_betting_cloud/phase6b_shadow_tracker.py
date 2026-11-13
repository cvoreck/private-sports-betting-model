#!/usr/bin/env python3
"""Phase 6B: immutable prospective shadow forecast ledger and later grading.

Reads existing canonical game finals and stored odds with a READ ONLY PostgreSQL
transaction. Writes JSON files *locally* for a separate GitHub data branch.
Never creates, updates, recommends or publishes a bet; makes no vendor API calls.

Requires existing Phase 2, Phase 5A, Phase 5B and Phase 6A modules unchanged.
"""
from __future__ import annotations

import argparse
import collections
import copy
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path

UTC = dt.timezone.utc
SCHEMA = 1
BRANCH = 'phase6b-shadow-ledger'
MAX_CAPTURE_AGE = dt.timedelta(hours=24)
DEFAULT_CAPTURE_WINDOW_HOURS = 72
MAX_ALLOWED_CAPTURE_WINDOW_HOURS = 72
MIN_TIME_TO_EVENT = dt.timedelta(minutes=30)
RESULTS_LAG = dt.timedelta(hours=24)
FINAL_TIME_TOLERANCE = dt.timedelta(hours=18)
MIN_LINE_LEAD = dt.timedelta(minutes=10)
MAX_DUPLICATE_MATCHUP_START_DRIFT = dt.timedelta(hours=8)
MAX_REPORT_LAG = dt.timedelta(minutes=30)
SUPPORTED_MARKETS = ('spreads', 'totals')
VALID_FORECAST_STATUSES = ('SHADOW_ONLY', 'ABSTAIN')


def stamp(value):
    from phase5a_event_matching_audit import parse_time
    return parse_time(value)


def utc_iso(value):
    return value.astimezone(UTC).isoformat()


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def digest(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def empty_ledger(now):
    return {'schema_version': SCHEMA, 'created_at': utc_iso(now),
            'updated_at': utc_iso(now), 'forecasts': []}


def key_of(forecast):
    return '|'.join([forecast['sport'], forecast['odds_event_id'],
                     forecast['market'], forecast['model_version']])


def load_ledger(path, now):
    if not path.exists():
        raise FileNotFoundError(
            'Previous ledger file missing. GitHub workflow must explicitly seed an empty ledger '
            'only when the isolated data branch does not exist.')
    ledger = json.loads(path.read_text(encoding='utf-8'))
    if ledger.get('schema_version') != SCHEMA or not isinstance(ledger.get('forecasts'), list):
        raise ValueError('Unsupported ledger schema')
    seen = set()
    for rec in ledger['forecasts']:
        forecast = rec.get('forecast')
        if not isinstance(forecast, dict) or key_of(forecast) != rec.get('key'):
            raise ValueError('Corrupted immutable forecast key')
        if digest(forecast) != rec.get('forecast_sha256'):
            raise ValueError('Immutable forecast hash mismatch; refusing silent history rewrite')
        grade = rec.get('grade')
        if grade is not None and digest(grade) != rec.get('grade_sha256'):
            raise ValueError('Locked grade hash mismatch; refusing silent history rewrite')
        if rec['key'] in seen:
            raise ValueError('Duplicate ledger key: '+rec['key'])
        seen.add(rec['key'])
    return ledger


def quote_structure_is_valid(market, data):
    from phase6a_market_anchor_shadow import market_reference
    from phase5b_real_line_backtest import implied_probability
    if market not in SUPPORTED_MARKETS or not isinstance(data, dict):
        return False
    sides = ('home', 'away') if market == 'spreads' else ('over', 'under')
    selections = data.get('selections')
    if not isinstance(selections, dict) or set(selections) != set(sides):
        return False
    try:
        points = {side: selections[side]['point'] for side in sides}
        prices = {side: selections[side]['american_odds'] for side in sides}
        if any(not finite(v) for v in points.values()):
            return False
        for side in sides:
            d = selections[side]
            for x in ('market_no_vig_conditional', 'shadow_conditional_nonpush'):
                if not finite(d[x]) or not (0 < d[x] < 1):
                    return False
        p1, p2 = implied_probability(prices[sides[0]]), implied_probability(prices[sides[1]])
        if not p1 or not p2:
            return False
        ref = market_reference({'points': points}, market)
        if abs(ref - data['market_reference']) > 0.0002:
            return False
        if abs(selections[sides[0]]['market_no_vig_conditional'] - p1/(p1+p2)) > .00002:
            return False
        if abs(sum(selections[s]['shadow_conditional_nonpush'] for s in sides) - 1) > .00002:
            return False
        if abs(sum(selections[s]['market_no_vig_conditional'] for s in sides) - 1) > .00002:
            return False
        if any(abs(selections[s]['shadow_conditional_nonpush'] -
                   selections[s]['market_no_vig_conditional']) > .02002 for s in sides):
            return False
        if abs(ref-round(ref)) < 1e-8:
            if data.get('push_probability_status') != 'NOT_ESTIMATED':
                return False
            if any(selections[s].get('shadow_unconditional_win') is not None for s in sides):
                return False
        else:
            if data.get('push_probability_status') != 'NO_PUSH_POSSIBLE':
                return False
            if any(not finite(selections[s].get('shadow_unconditional_win')) or
                   abs(selections[s]['shadow_unconditional_win']-
                       selections[s]['shadow_conditional_nonpush']) > 0.00002
                   for s in sides):
                return False
        if not all(finite(data[x]) for x in ('anchored_forecast', 'trained_forecast', 'baseline_forecast')):
            return False
        if data.get('top5_eligible') is not False or data.get('bet_selected') is not False:
            return False
        return True
    except (ValueError, TypeError, KeyError, ZeroDivisionError):
        return False


def extract_candidates(report, now, capture_window_hours=DEFAULT_CAPTURE_WINDOW_HOURS):
    """Capture only *fresh, genuinely prospective* 6A report predictions.

    Never import an old report retrospectively. A scheduled run builds the report
    and immediately captures it in the same process.
    """
    if not isinstance(capture_window_hours, int) or isinstance(capture_window_hours, bool) or not (1 <= capture_window_hours <= MAX_ALLOWED_CAPTURE_WINDOW_HOURS):
        raise ValueError('Capture window must be an integer between 1 and 72 hours')
    max_time_to_event = dt.timedelta(hours=capture_window_hours)
    counts = collections.Counter()
    asof = stamp(report.get('as_of'))
    generated = stamp(report.get('generated_at'))
    if not asof or not generated or asof > now or generated > now + dt.timedelta(minutes=1):
        raise ValueError('Missing/future shadow as-of timestamp')
    if now-asof > MAX_REPORT_LAG or abs((generated-asof).total_seconds()) > 30*60:
        raise ValueError('Shadow report is too old for prospective capture')
    if not (report.get('read_only') is True and report.get('publisher_changed') is False
            and report.get('moneyline_changed') is False
            and report.get('model_probabilities_calibrated') is False):
        raise ValueError('Unexpected Phase 6A safety status')
    if (report.get('production_changes') != 0 or report.get('model_promotions') != 0
            or report.get('database_writes') != 0 or report.get('external_api_calls') != 0
            or report.get('paid_api_calls') != 0):
        raise ValueError('Unexpected Phase 6A production writes or API calls')
    candidates = []
    for sport, sport_report in report.get('sports', {}).items():
        training_cutoff = stamp((sport_report.get('training') or {}).get('latest_eligible_training_start'))
        for game in sport_report.get('games', []):
            eid = str(game.get('odds_event_id') or '')
            start = stamp(game.get('commence_time'))
            home, away = game.get('home_team'), game.get('away_team')
            if not (eid and start and home and away and home != away):
                counts['missing_event_metadata'] += 1
                continue
            for market, output in (game.get('markets') or {}).items():
                if market not in SUPPORTED_MARKETS:
                    continue
                if (output.get('status') not in VALID_FORECAST_STATUSES
                        or output.get('model_version') != report.get('model_version')):
                    counts['nonforecast_market'] += 1
                    continue
                if not (MIN_TIME_TO_EVENT <= start-now <= max_time_to_event):
                    counts['outside_capture_window'] += 1
                    continue
                captured = stamp(output.get('capture_time'))
                if not captured or captured > asof or captured > now:
                    counts['invalid_snapshot_timestamp'] += 1
                    continue
                if not (dt.timedelta(0) <= now-captured <= MAX_CAPTURE_AGE
                        and captured <= start-MIN_LINE_LEAD):
                    counts['stale_or_late_snapshot'] += 1
                    continue
                if not training_cutoff or training_cutoff > captured-RESULTS_LAG:
                    counts['unsafe_training_cutoff'] += 1
                    continue
                if not quote_structure_is_valid(market, output):
                    counts['invalid_shadow_quote_or_probability'] += 1
                    continue
                forecast = {
                    'sport': sport, 'odds_event_id': eid, 'market': market,
                    'model_version': report['model_version'],
                    'home_team': home, 'away_team': away,
                    'event_start': utc_iso(start),
                    'source_report_asof': utc_iso(asof),
                    'source_report_generated_at': utc_iso(generated),
                    'source_quote_captured_at': utc_iso(captured),
                    'source_quote_category': output['quote_category'],
                    'training_latest_eligible_start': utc_iso(training_cutoff),
                    'bookmaker': output['bookmaker'],
                    'status': output['status'],
                    'abstain_reason': output.get('abstain_reason'),
                    'market_reference': output['market_reference'],
                    'baseline_forecast': output['baseline_forecast'],
                    'trained_forecast': output['trained_forecast'],
                    'anchored_forecast': output['anchored_forecast'],
                    'anchored_shift': output['anchored_shift'],
                    'conditional_probability_status': output['conditional_probability_status'],
                    'push_probability_status': output['push_probability_status'],
                    'selections': copy.deepcopy(output['selections']),
                }
                candidates.append(forecast)
    counts['valid_capture_candidates'] = len(candidates)
    return candidates, counts


def add_new_forecasts(ledger, candidates, now):
    existing = {r['key'] for r in ledger['forecasts']}
    # Do not treat a second Odds API ID for the same event/date as independent.
    existing_matchups = [(r['forecast']['sport'], r['forecast']['market'],
                          r['forecast']['home_team'], r['forecast']['away_team'],
                          stamp(r['forecast']['event_start']), r['forecast']['model_version'])
                         for r in ledger['forecasts']]
    n_new = n_duplicate = n_duplicate_game = 0
    for fc in sorted(candidates, key=lambda f: (f['event_start'], key_of(f))):
        key = key_of(fc)
        pairing = (fc['sport'], fc['market'], fc['home_team'], fc['away_team'],
                   stamp(fc['event_start']), fc['model_version'])
        if key in existing:
            n_duplicate += 1
            continue
        if any((old[:4] == pairing[:4] and old[5] == pairing[5] and
                abs(old[4]-pairing[4]) <= MAX_DUPLICATE_MATCHUP_START_DRIFT)
               for old in existing_matchups):
            n_duplicate_game += 1
            continue
        rec = {'key': key, 'forecast': fc, 'forecast_sha256': digest(fc),
               'first_recorded_at': utc_iso(now), 'grade': None,
               'grade_sha256': None, 'latest_observed_pregame_quote': None}
        ledger['forecasts'].append(rec)
        existing.add(key)
        existing_matchups.append(pairing)
        n_new += 1
    ledger['forecasts'].sort(key=lambda r: (r['forecast']['event_start'], r['key']))
    return {'new': n_new, 'already_frozen': n_duplicate,
            'duplicate_matchup_filtered': n_duplicate_game}


def find_unique_final(forecast, finals):
    from phase5a_event_matching_audit import norm
    start = stamp(forecast['event_start'])
    matching = []
    for game in finals:
        if (norm(game['home_team']) != norm(forecast['home_team']) or
                norm(game['away_team']) != norm(forecast['away_team'])):
            continue
        played = stamp(game['played_at'])
        if played and abs(played-start) <= FINAL_TIME_TOLERANCE:
            matching.append(game)
    if len(matching) != 1:
        return None, 'MULTIPLE_MATCHING_FINALS' if matching else 'FINAL_NOT_FOUND'
    return matching[0], None


def grade_forecast(forecast, final, now):
    from phase5b_real_line_backtest import as_result
    market = forecast['market']
    hs, aws = final['home_score'], final['away_score']
    if not all(finite(v) and v >= 0 and abs(v-round(v)) < 1e-6 for v in (hs, aws)):
        raise ValueError('Noninteger, negative or missing final scores')
    actual = float(hs-aws) if market == 'spreads' else float(hs+aws)
    sides = ('home', 'away') if market == 'spreads' else ('over', 'under')
    side_results = {side: as_result(actual, market, side,
                                   forecast['selections'][side]['point']) for side in sides}
    # If both sides produce the same nonpush result, score orientation is corrupt.
    if side_results[sides[0]] == side_results[sides[1]] and side_results[sides[0]] != 'P':
        raise ValueError('Inconsistent opposite-side settlement')
    return {
        'final_event_id': str(final['event_id']),
        'canonical_played_at': utc_iso(stamp(final['played_at'])),
        'home_score': int(hs), 'away_score': int(aws),
        'actual_value': actual,
        'side_results': side_results,
        'graded_at': utc_iso(now),
        'absolute_errors': {
            'market': round(abs(actual-forecast['market_reference']), 6),
            'baseline': round(abs(actual-forecast['baseline_forecast']), 6),
            'trained': round(abs(actual-forecast['trained_forecast']), 6),
            'anchored': round(abs(actual-forecast['anchored_forecast']), 6),
        },
    }


def find_later_pregame_quote(forecast, events):
    """Diagnostic only: latest *observed* later SAME-BOOK pregame quote.

    It is NOT necessarily a closing line; it must never change the frozen forecast.
    """
    from phase6a_market_anchor_shadow import market_reference
    event = events.get(forecast['odds_event_id'])
    if not event or len(event['meta']) != 1:
        return None
    home, away, start = next(iter(event['meta']))
    if (home != forecast['home_team'] or away != forecast['away_team']
            or start != stamp(forecast['event_start'])):
        return None
    original = stamp(forecast['source_quote_captured_at'])
    quotes = [q for q in event['quotes'] if q['kind'] == forecast['market']
              and q['book'] == forecast['bookmaker']
              and original < q['captured_at'] <= start-MIN_LINE_LEAD]
    if not quotes:
        return None
    latest = sorted(quotes, key=lambda q: (q['captured_at'],
                    q['updated_at'] or dt.datetime.min.replace(tzinfo=UTC)))[-1]
    later_reference = market_reference(latest, forecast['market'])
    return {'description': 'latest_observed_same_book_pregame_not_guaranteed_closing',
            'bookmaker': latest['book'],
            'captured_at': utc_iso(latest['captured_at']),
            'reference': round(later_reference, 6),
            'delta_from_frozen_reference': round(later_reference-forecast['market_reference'], 6)}


def apply_grades(ledger, finals_by_sport, events_by_sport, now):
    counts = collections.Counter()
    blockers = collections.Counter()
    for rec in ledger['forecasts']:
        fc = rec['forecast']
        start = stamp(fc['event_start'])
        if now < start+RESULTS_LAG:
            counts['awaiting_result_lag'] += 1
            continue
        final, reason = find_unique_final(fc, finals_by_sport.get(fc['sport'], []))
        if reason:
            blockers[reason] += 1
        elif rec['grade'] is None:
            grade = grade_forecast(fc, final, now)
            rec['grade'] = grade
            rec['grade_sha256'] = digest(grade)
            counts['newly_graded'] += 1
        elif (str(final['event_id']) != rec['grade']['final_event_id']
              or final['home_score'] != rec['grade']['home_score']
              or final['away_score'] != rec['grade']['away_score']):
            # Fail closed: never rewrite an established grade silently.
            raise ValueError('Canonical result changed for locked grade '+rec['key'])
        else:
            counts['already_graded'] += 1
        if fc['sport'] in events_by_sport:
            latest = find_later_pregame_quote(fc, events_by_sport[fc['sport']])
            if latest is not None:
                rec['latest_observed_pregame_quote'] = latest
                counts['later_quote_observed'] += 1
    return {'counts': dict(counts), 'pending_reasons': dict(blockers)}


def summarize(ledger, now, capture, grading, candidate_counts, capture_window_hours=DEFAULT_CAPTURE_WINDOW_HOURS):
    by_sport_market = collections.defaultdict(list)
    for rec in ledger['forecasts']:
        fc = rec['forecast']
        by_sport_market[(fc['sport'], fc['market'])].append(rec)
    sports = {}
    for (sport, market), records in sorted(by_sport_market.items()):
        results = [r for r in records if r['grade'] is not None]
        # Abstentions remain archived/graded, but are excluded from primary
        # accuracy and probability comparisons because they weren't forecasts.
        evaluated = [r for r in results if r['forecast']['status'] == 'SHADOW_ONLY']
        summary = {'captured': len(records), 'graded': len(results),
                   'graded_non_abstained': len(evaluated),
                   'pending': len(records)-len(results),
                   'abstained': sum(r['forecast']['status']=='ABSTAIN' for r in records),
                   'mean_absolute_error': {},
                   'nonpush_conditional_brier': {},
                   'nonpush_sample': 0,
                   'calibration_bins': [],
                   'line_movement_sample': 0,
                   'mean_later_observed_line_shift': None}
        if evaluated:
            for kind in ('market', 'baseline', 'trained', 'anchored'):
                summary['mean_absolute_error'][kind] = round(sum(
                    r['grade']['absolute_errors'][kind] for r in evaluated)/len(evaluated), 4)
            brier_market, brier_shadow, bins = [], [], collections.defaultdict(list)
            for rec in evaluated:
                fc, grade = rec['forecast'], rec['grade']
                primary = 'home' if market == 'spreads' else 'over'
                actual = grade['side_results'][primary]
                if actual == 'P':
                    continue
                y = 1 if actual == 'W' else 0
                market_p = fc['selections'][primary]['market_no_vig_conditional']
                shadow_p = fc['selections'][primary]['shadow_conditional_nonpush']
                brier_market.append((market_p-y)**2)
                brier_shadow.append((shadow_p-y)**2)
                bins[min(9, int(shadow_p*10))].append((shadow_p, y))
            summary['nonpush_sample'] = len(brier_market)
            if brier_market:
                summary['nonpush_conditional_brier'] = {
                    'market_no_vig': round(sum(brier_market)/len(brier_market), 6),
                    'shadow_heuristic': round(sum(brier_shadow)/len(brier_shadow), 6)}
                summary['calibration_bins'] = [
                    {'bin': idx, 'n': len(pts),
                     'mean_shadow_probability': round(sum(p for p, _ in pts)/len(pts), 4),
                     'observed_rate': round(sum(v for _,v in pts)/len(pts), 4)}
                    for idx, pts in sorted(bins.items())]
        movements = [r['latest_observed_pregame_quote']['delta_from_frozen_reference']
                     for r in records if r['latest_observed_pregame_quote'] is not None]
        if movements:
            summary['line_movement_sample'] = len(movements)
            summary['mean_later_observed_line_shift'] = round(sum(movements)/len(movements), 4)
        sports.setdefault(sport, {})[market] = summary
    total_graded = sum(r['grade'] is not None for r in ledger['forecasts'])
    return {'generated_at': utc_iso(now), 'evaluation_mode': 'IMMUTABLE_PROSPECTIVE_SHADOW_GRADING',
            'ledger_branch': BRANCH, 'read_only_database': True,
            'database_writes': 0, 'vendor_api_calls': 0,
            'github_branch_write': True,
            'publisher_changed': False, 'moneyline_changed': False,
            'real_bets_selected': 0, 'model_promotions': 0,
            'probability_calibrated': False,
            'first_capture_per_sport_event_market_version': True,
            'capture_window_hours_before_start': capture_window_hours,
            'max_age_of_quote_when_recorded_hours': 24,
            'grading_wait_hours_after_start': 24,
            'accuracy_comparisons_exclude_abstentions': True,
            'returns_or_ev_reported': False,
            'records_total': len(ledger['forecasts']),
            'records_graded': total_graded,
            'records_pending': len(ledger['forecasts'])-total_graded,
            'capture_this_run': capture,
            'candidate_screening': candidate_counts,
            'grading_this_run': grading,
            'sports': sports,
            'limitations': [
                'This does not establish a betting edge, profitability, or calibrated probability.',
                'Sportsbook market line is the forecast anchor; MAE comparison alone is not an edge.',
                'Same-game spread and total grades are correlated; count independent games separately.',
                'Probabilities are conditional on nonpush; whole-point push probability is not modeled.',
                'Later observed line is not a guaranteed market close; same bookmaker only.',
                'Quotes may be stale or unavailable in stored snapshots; no paid Odds API requests.',
                'No injuries, lineups, pace, goaltender or late injury news features.',
                'Historical game records may be corrected; conflicting locked grades fail closed.',
                'Data lives on a dedicated GitHub branch in a private repository; protect that branch.',
                'A single matched final uses exact normalized home/away names within 18h of start.'
            ]}


def grading_windows(ledger, now):
    """Restrict the scores read for grading to recorded event-time windows.

    Includes locked grades as well as pending grades to detect corrections.
    Phase 6A training-data queries remain unchanged.
    """
    per_sport = collections.defaultdict(list)
    for rec in ledger['forecasts']:
        fc = rec['forecast']
        start = stamp(fc['event_start'])
        if now >= start + RESULTS_LAG:
            per_sport[fc['sport']].append(
                (start - FINAL_TIME_TOLERANCE, start + FINAL_TIME_TOLERANCE))
    merged = {}
    for sport, intervals in per_sport.items():
        current = []
        for lo, hi in sorted(intervals):
            if current and lo <= current[-1][1]:
                current[-1] = (current[-1][0], max(hi, current[-1][1]))
            else:
                current.append((lo, hi))
        merged[sport] = current
    return merged


def fetch_relevant_finals(conn, ledger, now):
    """Avoid transferring all historical finals for every scheduled run.

    Query only ±18h event-time windows. Chunk OR terms for larger ledgers.
    """
    windows_by_sport = grading_windows(ledger, now)
    fetched = collections.defaultdict(list)
    read_counts = {}
    for sport, intervals in sorted(windows_by_sport.items()):
        read_counts[sport] = {'windows': len(intervals), 'rows': 0}
        for start_idx in range(0, len(intervals), 40):
            batch = intervals[start_idx:start_idx+40]
            predicates = ' OR '.join('(played_at BETWEEN %s AND %s)' for _ in batch)
            params = [sport, now - RESULTS_LAG]
            for lo, hi in batch:
                params.extend((lo, hi))
            sql = ("SELECT event_id, played_at, home_team, away_team, "
                   "home_score, away_score FROM canonical_completed_games "
                   "WHERE sport=%s AND season_type=2 AND played_at <= %s "
                   "AND (" + predicates + ") ORDER BY played_at, event_id")
            rows = conn.execute(sql, params).fetchall()
            fetched[sport].extend(rows)
            read_counts[sport]['rows'] += len(rows)
    return dict(fetched), read_counts


def perform(conn, ledger, now, capture_window_hours=DEFAULT_CAPTURE_WINDOW_HOURS):
    from psycopg.rows import dict_row
    from phase6a_market_anchor_shadow import produce_report, SPORTS
    from phase5b_real_line_backtest import gather_events
    conn.execute('SET TRANSACTION READ ONLY')
    prospective = produce_report(conn, now)
    candidates, cnt = extract_candidates(prospective, now, capture_window_hours)
    capture = add_new_forecasts(ledger, candidates, now)
    # Only read completed-game score windows tied to recorded forecasts.
    # This avoids a redundant full-history scoreboard download each run.
    finals_by_sport, final_read_counts = fetch_relevant_finals(conn, ledger, now)
    events_by_sport = {}
    # Line-movement audit is optional: if no past snapshots exist, keep null.
    relevant_sports = {r['forecast']['sport'] for r in ledger['forecasts']
                       if now >= stamp(r['forecast']['event_start'])+RESULTS_LAG}
    for sport in sorted(relevant_sports):
        events_by_sport[sport] = gather_events(conn, sport, collections.Counter())
    grading = apply_grades(ledger, finals_by_sport, events_by_sport, now)
    conn.rollback()
    ledger['updated_at'] = utc_iso(now)
    report = summarize(ledger, now, capture, grading, dict(cnt), capture_window_hours)
    report['grading_database_read_counts'] = final_read_counts
    return report


def main():
    cli = argparse.ArgumentParser(description='Phase 6B immutable prospective tracking')
    cli.add_argument('--ledger-in', default='phase6b_previous_ledger.json')
    cli.add_argument('--ledger-out', default='phase6b_shadow_ledger.json')
    cli.add_argument('--report-out', default='phase6b_shadow_summary.json')
    cli.add_argument('--capture-window-hours', type=int, default=DEFAULT_CAPTURE_WINDOW_HOURS,
                     choices=range(1, MAX_ALLOWED_CAPTURE_WINDOW_HOURS + 1),
                     metavar='1..72', help='Prospective capture horizon (default: 72 hours)')
    args = cli.parse_args()
    now = dt.datetime.now(UTC)
    ledger = load_ledger(Path(args.ledger_in), now)
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL required')
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(url, row_factory=dict_row, connect_timeout=20) as conn:
        summary = perform(conn, ledger, now, args.capture_window_hours)
    Path(args.ledger_out).write_text(json.dumps(ledger, indent=2, sort_keys=True,
                                               allow_nan=False)+'\n', encoding='utf-8')
    Path(args.report_out).write_text(json.dumps(summary, indent=2, sort_keys=True,
                                               allow_nan=False)+'\n', encoding='utf-8')
    print('Phase 6B: new=', summary['capture_this_run']['new'],
          'graded=', summary['grading_this_run']['counts'].get('newly_graded', 0),
          'total=', summary['records_total'], 'pending=', summary['records_pending'])
    print('No Supabase writes, vendor calls, model promotions, or Top 5 changes.')


if __name__ == '__main__':
    main()
