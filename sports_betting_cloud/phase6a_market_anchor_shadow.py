#!/usr/bin/env python3
"""Phase 6A - prospective, read-only, market-anchored spread/total shadow.

No calls to external providers, no INSERT/UPDATE/DDL, no picks, no promotion.
Dependencies: user's existing Phase 2, Phase 5A and Phase 5B modules.

Probabilities are *provisional conditional non-push* diagnostics, not calibrated
betting probabilities, and must not be used by daily_top5.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import os
import statistics
from pathlib import Path

UTC = dt.timezone.utc
VERSION = 'phase6a-market-anchor-v1-SHADOW-NOT-CALIBRATED'
SPORTS = ('basketball_nba', 'americanfootball_nfl', 'icehockey_nhl')
HORIZON = dt.timedelta(days=3)
RESULTS_LAG = dt.timedelta(hours=24)
MAX_DELTA_PROB = 0.02   # max +/-2 percentage points from same-book no-vig

# Predeclared, conservative engineering defaults for exploration, NOT tuned for ROI.
# All market probabilities remain unvalidated; no betting EV is produced.
PARAMETERS = {
    'basketball_nba': {
        'spreads': {'weight': 0.10, 'max_shift': 1.25, 'max_disagreement': 18.0},
        'totals': {'weight': 0.05, 'max_shift': 1.25, 'max_disagreement': 25.0}},
    'americanfootball_nfl': {
        'spreads': {'weight': 0.10, 'max_shift': 1.0, 'max_disagreement': 14.0},
        'totals': {'weight': 0.05, 'max_shift': 1.25, 'max_disagreement': 20.0}},
    'icehockey_nhl': {
        'spreads': {'weight': 0.05, 'max_shift': 0.15, 'max_disagreement': 3.0},
        'totals': {'weight': 0.05, 'max_shift': 0.15, 'max_disagreement': 3.0}},
}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clamp(value, minimum, maximum):
    return min(maximum, max(minimum, value))


def market_reference(quote, market):
    """Translate sportsbook home spread into home winning margin, totals unchanged."""
    points = quote['points']
    if market == 'spreads':
        if not finite(points.get('home')) or not finite(points.get('away')):
            raise ValueError('Invalid spread points')
        if abs(points['home'] + points['away']) > 1e-6:
            raise ValueError('Asymmetric spread points')
        return -points['home']
    if market == 'totals':
        if not finite(points.get('over')) or not finite(points.get('under')):
            raise ValueError('Invalid total points')
        if abs(points['over'] - points['under']) > 1e-6:
            raise ValueError('Asymmetric total points')
        return points['over']
    raise ValueError('Unknown market')


def anchor_forecast(sport, market, raw_prediction, sportsbook_reference):
    """Fixed low-weight anchoring; abstain on implausible disagreement."""
    if not finite(raw_prediction) or not finite(sportsbook_reference):
        raise ValueError('Nonfinite inputs')
    cfg = PARAMETERS[sport][market]
    difference = raw_prediction - sportsbook_reference
    if abs(difference) > cfg['max_disagreement']:
        return {
            'anchored': sportsbook_reference, 'shift': 0.0,
            'raw_minus_market': difference, 'abstain': True,
            'abstain_reason': 'raw_model_market_disagreement_exceeds_guardrail'}
    shift = clamp(cfg['weight'] * difference, -cfg['max_shift'], cfg['max_shift'])
    return {
        'anchored': sportsbook_reference + shift, 'shift': shift,
        'raw_minus_market': difference, 'abstain': False, 'abstain_reason': None}


def provisional_market_probability(market_no_vig, shift, residual_std):
    """Bounded market-relative diagnostic, not a validated calibration curve."""
    if not (finite(market_no_vig) and 0 < market_no_vig < 1 and finite(shift)):
        raise ValueError('Invalid probability/shift')
    if not finite(residual_std) or residual_std <= 0:
        raise ValueError('Invalid historical residual std')
    cdf_shift = 0.5 * (1 + math.erf(shift / (residual_std * math.sqrt(2)))) - 0.5
    return clamp(market_no_vig + clamp(cdf_shift, -MAX_DELTA_PROB, MAX_DELTA_PROB), .01, .99)


def preperiod_std(residuals, sport, market):
    """Historical preperiod scoring residual scale; floors avoid false certainty."""
    floors = {'basketball_nba': {'spreads': 11., 'totals': 15.},
              'americanfootball_nfl': {'spreads': 10., 'totals': 10.},
              'icehockey_nhl': {'spreads': 2., 'totals': 1.8}}
    if not residuals or len(residuals) < 50:
        return None
    if not all(finite(x) for x in residuals):
        return None
    return max(floors[sport][market], statistics.pstdev(residuals))


def format_market_shadow(sport, market, quote, raw, baseline, residuals):
    """Generate one event-market report, never picks or expected value."""
    from phase5b_real_line_backtest import implied_probability

    first, second = ('home', 'away') if market == 'spreads' else ('over', 'under')
    p_first_raw = implied_probability(quote['prices'][first])
    p_second_raw = implied_probability(quote['prices'][second])
    if p_first_raw is None or p_second_raw is None or p_first_raw+p_second_raw <= 0:
        raise ValueError('Invalid same-book price pair')
    reference = market_reference(quote, market)
    anchor = anchor_forecast(sport, market, raw, reference)
    scale = preperiod_std(residuals, sport, market)
    if scale is None:
        raise ValueError('Insufficient residual history')
    no_vig_first = p_first_raw / (p_first_raw+p_second_raw)
    p_first = provisional_market_probability(no_vig_first, anchor['shift'], scale)
    # Conditional-on-no-push probabilities complement one another.
    is_integer_line = abs(reference - round(reference)) < 1e-9
    status = 'ABSTAIN' if anchor['abstain'] else 'SHADOW_ONLY'
    return {
        'status': status, 'model_version': VERSION, 'market': market,
        'bookmaker': quote['book'],
        'capture_time': quote['captured_at'].isoformat(),
        'quote_category': quote['snapshot_category'],
        'market_reference': round(reference, 4),
        'trained_forecast': round(raw, 4),
        'baseline_forecast': round(baseline, 4),
        'anchored_forecast': round(anchor['anchored'], 4),
        'anchored_shift': round(anchor['shift'], 4),
        'raw_minus_market': round(anchor['raw_minus_market'], 4),
        'historical_validation_residual_std': round(scale, 4),
        'conditional_probability_status': 'UNVALIDATED_HEURISTIC_NOT_FOR_PUBLISHING',
        'push_probability_status': 'NOT_ESTIMATED' if is_integer_line else 'NO_PUSH_POSSIBLE',
        'push_probability': None if is_integer_line else 0.0,
        'selections': {
            first: {'point': quote['points'][first], 'american_odds': quote['prices'][first],
                    'market_no_vig_conditional': round(no_vig_first, 6),
                    'shadow_conditional_nonpush': round(p_first, 6),
                    'shadow_unconditional_win': None if is_integer_line else round(p_first, 6)},
            second: {'point': quote['points'][second], 'american_odds': quote['prices'][second],
                     'market_no_vig_conditional': round(1-no_vig_first, 6),
                     'shadow_conditional_nonpush': round(1-p_first, 6),
                     'shadow_unconditional_win': None if is_integer_line else round(1-p_first, 6)}},
        'abstain_reason': anchor['abstain_reason'],
        'top5_eligible': False, 'bet_selected': False,
    }


def names_to_ids(games, before, normalizer, parser):
    """Use only *past* canonical games to resolve event team names to IDs."""
    known = collections.defaultdict(set)
    for g in games:
        played = parser(g['played_at'])
        if not played or played > before:
            continue
        for side in ('home', 'away'):
            name, identifier = g.get(side+'_team'), g.get(side+'_team_id')
            if name and identifier is not None:
                known[normalizer(name)].add(str(identifier))
    return {name: next(iter(ids)) for name, ids in known.items() if len(ids) == 1}


def eligible_future_quotes(events, asof, choose_quote):
    """Only unambiguous events with quotes already observed by asof."""
    selected = []
    counters = collections.Counter()
    for eid, rec in events.items():
        if len(rec['meta']) != 1:
            counters['ambiguous_event_metadata'] += 1
            continue
        home, away, start = next(iter(rec['meta']))
        if not (home and away and start):
            counters['invalid_event_metadata'] += 1
            continue
        if not (asof < start <= asof + HORIZON):
            counters['outside_future_window'] += 1
            continue
        if home == away:
            counters['same_team'] += 1
            continue
        found = {}
        for market in ('spreads', 'totals'):
            # gather_events has already verified capture/update timestamps.
            candidates = [q for q in rec['quotes'] if q['kind'] == market and q['captured_at'] <= asof]
            quote = choose_quote(candidates, market)
            if quote is not None:
                found[market] = quote
        if not found:
            counters['no_valid_quotes'] += 1
            continue
        selected.append({'odds_event_id': eid, 'home': home, 'away': away,
                         'start': start, 'quotes': found})
    return sorted(selected, key=lambda x: (x['start'], x['odds_event_id'])), counters


def produce_report(conn, asof):
    from phase5a_event_matching_audit import norm, parse_time
    from phase5b_real_line_backtest import (gather_events, choose_quote, choose_training,
                                           features_as_of)
    from spread_total_research_phase2 import build_examples, prediction

    report = {'generated_at': dt.datetime.now(UTC).isoformat(),
              'as_of': asof.isoformat(), 'model_version': VERSION,
              'read_only': True, 'database_writes': 0, 'paid_api_calls': 0,
              'external_api_calls': 0, 'production_changes': 0, 'model_promotions': 0,
              'publisher_changed': False, 'moneyline_changed': False,
              'evaluation_mode': 'PROSPECTIVE_SHADOW_NO_GRADING',
              'forecast_horizon_hours': int(HORIZON.total_seconds()/3600),
              'model_probabilities_calibrated': False,
              'limitations': [
                  'No sportsbook-return or EV computation; no bets are selected.',
                  'Bounded market-relative probabilities are heuristics, not validated calibration.',
                  'Whole-point push probability is unknown, so unconditional win probability is omitted.',
                  'Coefficients and residual scale fit only to canonical history before earliest chosen quote.',
                  'Results are for research only, excluded from the Daily Top 5.',
                  'No injuries, projected lineups, pace, goalie or late news inputs.',
                  'Sportsbook prices captured earlier may have moved before real execution.'
              ], 'sports': {}}
    for sport in SPORTS:
        counts = collections.Counter()
        odds_events = gather_events(conn, sport, counts)
        upcoming, screening = eligible_future_quotes(odds_events, asof, choose_quote)
        counts.update(screening)
        counts['future_events_with_quotes'] = len(upcoming)
        sport_result = {'status': 'SHADOW_ONLY', 'counts': {}, 'training': {}, 'games': []}
        report['sports'][sport] = sport_result
        if not upcoming:
            sport_result['status'] = 'NO_FUTURE_PRICED_GAMES'
            sport_result['counts'] = dict(counts)
            continue

        earliest_quote = min(q['captured_at'] for e in upcoming for q in e['quotes'].values())
        training_cutoff = earliest_quote - RESULTS_LAG
        games = conn.execute('''SELECT event_id, played_at, home_team, away_team,
                       home_team_id, away_team_id, home_score, away_score
                       FROM canonical_completed_games
                       WHERE sport=%s AND season_type=2 AND played_at <= %s
                       ORDER BY played_at,event_id''', (sport, asof - RESULTS_LAG)).fetchall()
        id_lookup = names_to_ids(games, training_cutoff, norm, parse_time)
        train_games = [g for g in games if parse_time(g['played_at']) <= training_cutoff]
        examples = build_examples(train_games, sport)
        fitted = {market: choose_training(examples, target)
                  for market, target in (('spreads', 'margin'), ('totals', 'total'))}
        sport_result['training'] = {
            'first_quote': earliest_quote.isoformat(),
            'latest_eligible_training_start': training_cutoff.isoformat(),
            'historical_examples': len(examples),
            'markets': {m: ({'n': x['fit_games'], 'lambda': x['lambda'],
                             'validation_n': x['validation_games'],
                             'validation_mae': x['validation_mae']}
                            if x else {'status': 'INSUFFICIENT_HISTORY'})
                        for m, x in fitted.items()}}

        for upcoming_event in upcoming:
            hid, aid = id_lookup.get(upcoming_event['home']), id_lookup.get(upcoming_event['away'])
            game = {'odds_event_id': upcoming_event['odds_event_id'],
                    'home_team': upcoming_event['home'], 'away_team': upcoming_event['away'],
                    'commence_time': upcoming_event['start'].isoformat(), 'markets': {}}
            if not (hid and aid and hid != aid):
                counts['unresolved_team_ids'] += 1
                game['status'] = 'ABSTAIN_UNRESOLVED_TEAM_ID'
                sport_result['games'].append(game)
                continue
            target = {'event_id': '__phase6a_future_target__',
                      'home_team_id': hid, 'away_team_id': aid}
            for market, quote in upcoming_event['quotes'].items():
                model = fitted.get(market)
                if not model:
                    counts['untrained_market'] += 1
                    game['markets'][market] = {'status': 'ABSTAIN_INSUFFICIENT_TRAINING'}
                    continue
                row = features_as_of(games, quote['captured_at'], target, sport)
                if row is None:
                    counts['missing_asof_features'] += 1
                    game['markets'][market] = {'status': 'ABSTAIN_MISSING_ASOF_FEATURES'}
                    continue
                target_key, baseline_key = (('margin', 'baseline_margin') if market == 'spreads'
                                            else ('total', 'baseline_total'))
                try:
                    raw = prediction(model['model'], row)
                    output = format_market_shadow(sport, market, quote, raw,
                                                  row[baseline_key], model['residuals'])
                except (ValueError, KeyError, ZeroDivisionError, OverflowError) as exc:
                    counts['invalid_market_output'] += 1
                    game['markets'][market] = {'status': 'ABSTAIN_INVALID_OUTPUT', 'reason': str(exc)}
                    continue
                game['markets'][market] = output
                counts['shadow_market_outputs'] += 1
                if output['status'] == 'ABSTAIN':
                    counts['disagreement_abstentions'] += 1
            game['status'] = ('SHADOW_ONLY' if any(v.get('status') == 'SHADOW_ONLY'
                                                    for v in game['markets'].values()) else 'ABSTAIN')
            sport_result['games'].append(game)
        sport_result['counts'] = dict(counts)
    return report


def run():
    parser = argparse.ArgumentParser(description='Read-only Phase 6A market-anchored shadow report')
    parser.add_argument('--as-of', help='UTC ISO timestamp; omit for current prospective report')
    args = parser.parse_args()
    now = dt.datetime.now(UTC)
    if args.as_of:
        from phase5a_event_matching_audit import parse_time
        asof = parse_time(args.as_of)
        if asof is None or asof > now:
            raise ValueError('--as-of must be a valid past UTC timestamp')
    else:
        asof = now
    db = os.getenv('DATABASE_URL')
    if not db:
        raise RuntimeError('DATABASE_URL secret missing')
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(db, row_factory=dict_row, connect_timeout=20) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        result = produce_report(conn, asof)
        conn.rollback()
    filename = 'phase6a_shadow_market_anchor_report.json'
    Path(filename).write_text(json.dumps(result, indent=2, sort_keys=True)+'\n', encoding='utf-8')
    print('Saved', filename, '| database unchanged | no API calls | no publication')
    for sport, v in result['sports'].items():
        print(sport, 'priced_future_games=', v['counts'].get('future_events_with_quotes', 0),
              'shadow_outputs=', v['counts'].get('shadow_market_outputs', 0),
              'abstentions=', v['counts'].get('disagreement_abstentions', 0))


if __name__ == '__main__':
    run()
