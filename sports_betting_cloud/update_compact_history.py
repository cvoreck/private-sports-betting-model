"""Offline Phase 6B safeguards; no DB, network, credentials or writes to GitHub."""
import datetime as dt
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

# The production GitHub workflow installs psycopg; support local pure-logic
# testing even when a PostgreSQL driver isn't installed in the host runtime.
try:
    import psycopg  # noqa: F401
except ImportError:
    db = types.ModuleType('psycopg')
    rows = types.ModuleType('psycopg.rows')
    rows.dict_row = lambda *args, **kwargs: None
    db.rows = rows
    sys.modules['psycopg'] = db
    sys.modules['psycopg.rows'] = rows

from phase6b_shadow_tracker import (UTC, add_new_forecasts, apply_grades,
    digest, empty_ledger, extract_candidates, find_later_pregame_quote,
    find_unique_final, grade_forecast, key_of, load_ledger, summarize,
    grading_windows, fetch_relevant_finals)

NOW = dt.datetime(2026, 11, 12, 20, 0, tzinfo=UTC)


def sample_report(start=None, captured=None, market='spreads', status='SHADOW_ONLY'):
    start = start or NOW + dt.timedelta(hours=12)
    captured = captured or NOW - dt.timedelta(hours=3)
    first, second = ('home', 'away') if market == 'spreads' else ('over', 'under')
    ref = 3.5 if market == 'spreads' else 219.5
    point = -3.5 if market == 'spreads' else 219.5
    output = {
        'status': status, 'model_version': 'phase6a-market-anchor-v1-SHADOW-NOT-CALIBRATED',
        'market': market, 'bookmaker': 'draftkings', 'capture_time': captured.isoformat(),
        'quote_category': 'pregame_odds', 'market_reference': ref,
        'trained_forecast': ref+1., 'baseline_forecast': ref+2.,
        'anchored_forecast': ref+.1, 'anchored_shift': .1,
        'conditional_probability_status': 'UNVALIDATED_HEURISTIC_NOT_FOR_PUBLISHING',
        'push_probability_status': 'NO_PUSH_POSSIBLE', 'top5_eligible': False,
        'bet_selected': False, 'abstain_reason': 'disagreement' if status == 'ABSTAIN' else None,
        'selections': {
            first: {'point': point, 'american_odds': -110,
                    'market_no_vig_conditional': .5, 'shadow_conditional_nonpush': .51,
                    'shadow_unconditional_win': .51},
            second: {'point': -point if market == 'spreads' else point,
                     'american_odds': -110,
                     'market_no_vig_conditional': .5, 'shadow_conditional_nonpush': .49,
                     'shadow_unconditional_win': .49}}}
    return {'as_of': NOW.isoformat(), 'generated_at': (NOW+dt.timedelta(seconds=1)).isoformat(),
            'model_version': output['model_version'],
            'read_only': True, 'publisher_changed': False,
            'moneyline_changed': False, 'model_probabilities_calibrated': False,
            'production_changes': 0, 'model_promotions': 0,
            'database_writes': 0, 'external_api_calls': 0, 'paid_api_calls': 0,
            'sports': {'basketball_nba': {'training': {
                'latest_eligible_training_start': (captured-dt.timedelta(days=2)).isoformat()},
                'games': [{'odds_event_id': 'odds-id-1', 'home_team': 'boston celtics',
                           'away_team': 'miami heat', 'commence_time': start.isoformat(),
                           'markets': {market: output}}]}}}


def sample_forecast(market='spreads'):
    return extract_candidates(sample_report(market=market), NOW)[0][0]


def sample_final(start=None, home=110, away=104):
    return {'event_id': 'espn-123', 'played_at': start or NOW + dt.timedelta(hours=12),
            'home_team': 'Boston Celtics', 'away_team': 'Miami Heat',
            'home_score': home, 'away_score': away}


class ShadowTrackerTests(unittest.TestCase):
    def test_can_capture_valid_prospective_forecast(self):
        f, counts = extract_candidates(sample_report(), NOW)
        self.assertEqual(len(f), 1)
        self.assertEqual(counts['valid_capture_candidates'], 1)
        self.assertEqual(f[0]['market_reference'], 3.5)

    def test_no_postgame_capture(self):
        report = sample_report(start=NOW-dt.timedelta(hours=1))
        self.assertEqual(extract_candidates(report, NOW)[0], [])

    def test_30_minute_minimum_before_game(self):
        report = sample_report(start=NOW+dt.timedelta(minutes=20))
        self.assertEqual(extract_candidates(report, NOW)[0], [])

    def test_72_hour_default_accepts_48_hour_game(self):
        report = sample_report(start=NOW+dt.timedelta(hours=48))
        self.assertEqual(len(extract_candidates(report, NOW)[0]), 1)

    def test_36_hour_override_rejects_48_hour_game(self):
        report = sample_report(start=NOW+dt.timedelta(hours=48))
        self.assertEqual(extract_candidates(report, NOW, 36)[0], [])

    def test_72_hour_limit_rejects_73_hour_game(self):
        report = sample_report(start=NOW+dt.timedelta(hours=73))
        self.assertEqual(extract_candidates(report, NOW)[0], [])

    def test_invalid_capture_windows_rejected(self):
        for value in (0, -1, 73, 72.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                extract_candidates(sample_report(), NOW, value)

    def test_summary_reports_configured_capture_window(self):
        self.assertEqual(summarize(empty_ledger(NOW), NOW, {}, {}, {}, 48)['capture_window_hours_before_start'], 48)

    def test_stale_bookmaker_snapshot_rejected(self):
        report = sample_report(captured=NOW-dt.timedelta(hours=28))
        self.assertEqual(extract_candidates(report, NOW)[0], [])

    def test_future_snapshot_rejected(self):
        report = sample_report(captured=NOW+dt.timedelta(minutes=1))
        self.assertEqual(extract_candidates(report, NOW)[0], [])

    def test_backfilled_old_report_rejected(self):
        with self.assertRaises(ValueError):
            extract_candidates(sample_report(), NOW+dt.timedelta(hours=1))

    def test_unsafe_training_cutoff_rejected(self):
        report = sample_report()
        report['sports']['basketball_nba']['training']['latest_eligible_training_start'] = NOW.isoformat()
        self.assertEqual(extract_candidates(report, NOW)[0], [])

    def test_asymmetric_spread_rejected(self):
        report = sample_report()
        report['sports']['basketball_nba']['games'][0]['markets']['spreads']['selections']['away']['point'] = 4.5
        self.assertEqual(extract_candidates(report, NOW)[0], [])

    def test_impossible_probabilities_rejected(self):
        report = sample_report()
        report['sports']['basketball_nba']['games'][0]['markets']['spreads']['selections']['home']['shadow_conditional_nonpush'] = .90
        self.assertEqual(extract_candidates(report, NOW)[0], [])

    def test_top5_marked_true_rejected(self):
        report = sample_report()
        report['sports']['basketball_nba']['games'][0]['markets']['spreads']['top5_eligible'] = True
        self.assertEqual(extract_candidates(report, NOW)[0], [])

    def test_report_with_production_change_rejected(self):
        report = sample_report()
        report['production_changes'] = 1
        with self.assertRaises(ValueError):
            extract_candidates(report, NOW)

    def test_first_forecast_frozen_on_rerun(self):
        ledger = empty_ledger(NOW)
        f = sample_forecast()
        self.assertEqual(add_new_forecasts(ledger, [f], NOW)['new'], 1)
        changed = dict(f, anchored_forecast=50)
        counts = add_new_forecasts(ledger, [changed], NOW+dt.timedelta(hours=1))
        self.assertEqual(counts['new'], 0)
        self.assertEqual(counts['already_frozen'], 1)
        self.assertEqual(ledger['forecasts'][0]['forecast']['anchored_forecast'], 3.6)

    def test_odds_id_duplicate_matchup_rejected(self):
        ledger = empty_ledger(NOW)
        first = sample_forecast()
        second = dict(first, odds_event_id='different-bookmaker-id',
                      event_start=(NOW+dt.timedelta(hours=12, minutes=5)).isoformat())
        self.assertEqual(add_new_forecasts(ledger, [first, second], NOW)['new'], 1)
        self.assertEqual(len(ledger['forecasts']), 1)

    def test_ledger_hash_tampering_detected(self):
        ledger = empty_ledger(NOW)
        add_new_forecasts(ledger, [sample_forecast()], NOW)
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)/'data.json'
            p.write_text(json.dumps(ledger), encoding='utf-8')
            self.assertEqual(len(load_ledger(p, NOW)['forecasts']), 1)
            ledger['forecasts'][0]['forecast']['anchored_forecast'] += 20
            p.write_text(json.dumps(ledger), encoding='utf-8')
            with self.assertRaises(ValueError):
                load_ledger(p, NOW)

    def test_missing_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(FileNotFoundError):
                load_ledger(Path(root)/'missing.json', NOW)

    def test_exact_pair_and_time_required(self):
        fc = sample_forecast()
        found, reason = find_unique_final(fc, [sample_final()])
        self.assertIsNone(reason)
        self.assertEqual(found['event_id'], 'espn-123')
        far = sample_final(start=NOW+dt.timedelta(days=3))
        self.assertEqual(find_unique_final(fc,[far])[1], 'FINAL_NOT_FOUND')
        self.assertEqual(find_unique_final(fc,[sample_final(),sample_final()])[1],
                         'MULTIPLE_MATCHING_FINALS')

    def test_correct_spread_and_total_grade(self):
        spread = grade_forecast(sample_forecast(), sample_final(home=110,away=104), NOW)
        self.assertEqual(spread['side_results'], {'home': 'W', 'away': 'L'})
        total = grade_forecast(sample_forecast('totals'), sample_final(home=110,away=104), NOW)
        self.assertEqual(total['side_results'], {'over':'L','under':'W'})

    def test_whole_point_push_does_not_count_as_win(self):
        fc = sample_forecast('totals')
        fc['selections']['over']['point'] = 214.0
        fc['selections']['under']['point'] = 214.0
        grade = grade_forecast(fc, sample_final(home=110,away=104), NOW)
        self.assertEqual(grade['side_results'], {'over':'P','under':'P'})

    def test_24h_grading_lag(self):
        ledger = empty_ledger(NOW)
        add_new_forecasts(ledger,[sample_forecast()],NOW)
        final = sample_final()
        early = NOW+dt.timedelta(hours=18)
        result = apply_grades(ledger, {'basketball_nba':[final]}, {}, early)
        self.assertEqual(result['counts']['awaiting_result_lag'], 1)
        self.assertIsNone(ledger['forecasts'][0]['grade'])

    def test_grading_is_idempotent_and_correction_fails_closed(self):
        ledger = empty_ledger(NOW)
        add_new_forecasts(ledger, [sample_forecast()], NOW)
        later = NOW + dt.timedelta(days=3)
        good = sample_final()
        result = apply_grades(ledger, {'basketball_nba':[good]}, {}, later)
        self.assertEqual(result['counts']['newly_graded'], 1)
        grade_hash = ledger['forecasts'][0]['grade_sha256']
        again = apply_grades(ledger, {'basketball_nba':[good]}, {}, later)
        self.assertEqual(again['counts']['already_graded'], 1)
        self.assertEqual(ledger['forecasts'][0]['grade_sha256'], grade_hash)
        with self.assertRaises(ValueError):
            apply_grades(ledger, {'basketball_nba':[dict(good, home_score=111)]}, {}, later)

    def test_no_final_remains_pending(self):
        ledger = empty_ledger(NOW)
        add_new_forecasts(ledger, [sample_forecast()], NOW)
        late = NOW+dt.timedelta(days=3)
        result = apply_grades(ledger, {'basketball_nba':[]}, {}, late)
        self.assertEqual(result['pending_reasons']['FINAL_NOT_FOUND'], 1)
        self.assertIsNone(ledger['forecasts'][0]['grade'])

    def test_brier_and_mae_excludes_abstention(self):
        ledger = empty_ledger(NOW)
        f = sample_forecast()
        abstain = dict(f, odds_event_id='odds-id-2', status='ABSTAIN',
                       home_team='new york knicks')
        add_new_forecasts(ledger, [f, abstain], NOW)
        for rec in ledger['forecasts']:
            grade = grade_forecast(rec['forecast'], sample_final(), NOW+dt.timedelta(days=2))
            rec['grade'],rec['grade_sha256'] = grade,digest(grade)
        s = summarize(ledger,NOW,{}, {}, {})
        sm = s['sports']['basketball_nba']['spreads']
        self.assertEqual(sm['graded'],2)
        self.assertEqual(sm['graded_non_abstained'],1)
        self.assertEqual(sm['nonpush_sample'],1)
        self.assertAlmostEqual(sm['mean_absolute_error']['market'],2.5)

    def test_grading_windows_skip_future_and_merge_spread_total(self):
        ledger = empty_ledger(NOW)
        spread = sample_forecast()
        total = sample_forecast('totals')
        total['odds_event_id'] = 'odds-id-2'
        add_new_forecasts(ledger, [spread, total], NOW)
        self.assertEqual(grading_windows(ledger, NOW), {})
        windows = grading_windows(ledger, NOW + dt.timedelta(days=3))
        self.assertEqual(list(windows), ['basketball_nba'])
        self.assertEqual(len(windows['basketball_nba']), 1)
        self.assertEqual(windows['basketball_nba'][0][0], NOW - dt.timedelta(hours=6))
        self.assertEqual(windows['basketball_nba'][0][1], NOW + dt.timedelta(hours=30))

    def test_relevant_final_query_is_bounded_and_no_query_before_lag(self):
        class FakeConnection:
            def __init__(self):
                self.calls = []
            def execute(self, sql, args):
                self.calls.append((sql, args))
                return self
            def fetchall(self):
                return [sample_final()]
        ledger = empty_ledger(NOW)
        add_new_forecasts(ledger, [sample_forecast()], NOW)
        conn = FakeConnection()
        result, counts = fetch_relevant_finals(conn, ledger, NOW)
        self.assertEqual(result, {})
        self.assertEqual(counts, {})
        self.assertEqual(conn.calls, [])
        result, counts = fetch_relevant_finals(conn, ledger, NOW+dt.timedelta(days=3))
        self.assertEqual(counts['basketball_nba']['rows'], 1)
        self.assertEqual(len(conn.calls), 1)
        sql, args = conn.calls[0]
        self.assertIn('played_at BETWEEN %s AND %s', sql)
        self.assertEqual(args[0], 'basketball_nba')
        self.assertEqual(args[2], NOW-dt.timedelta(hours=6))
        self.assertEqual(args[3], NOW+dt.timedelta(hours=30))

    def test_later_quote_same_book_only(self):
        fc = sample_forecast()
        start = dt.datetime.fromisoformat(fc['event_start'])
        originally = dt.datetime.fromisoformat(fc['source_quote_captured_at'])
        events = {'odds-id-1': {'meta': {(fc['home_team'],fc['away_team'], start)},
                  'quotes': [
                    {'book':'fanduel','kind':'spreads','captured_at':originally+dt.timedelta(minutes=30),
                     'points':{'home':-7.5,'away':7.5},'updated_at':None},
                    {'book':'draftkings','kind':'spreads','captured_at':originally+dt.timedelta(hours=1),
                     'points':{'home':-4.5,'away':4.5},'updated_at':None},
                    {'book':'draftkings','kind':'spreads','captured_at':start,
                     'points':{'home':-9.5,'away':9.5},'updated_at':None}]}}
        later = find_later_pregame_quote(fc, events)
        self.assertEqual(later['reference'],4.5)
        self.assertEqual(later['delta_from_frozen_reference'],1.0)


if __name__ == '__main__':
    unittest.main()
