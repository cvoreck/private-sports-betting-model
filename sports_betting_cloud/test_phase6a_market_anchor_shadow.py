"""Offline logic checks. No database credentials, network or production writes."""
import datetime as dt
import unittest

from phase6a_market_anchor_shadow import (
    anchor_forecast, market_reference, provisional_market_probability,
    preperiod_std, names_to_ids, eligible_future_quotes, PARAMETERS,
)

UTC = dt.timezone.utc


class Phase6aShadowTests(unittest.TestCase):
    def test_spread_sign_reference(self):
        self.assertEqual(market_reference({'points': {'home': -4.5, 'away': 4.5}}, 'spreads'), 4.5)

    def test_total_reference(self):
        self.assertEqual(market_reference({'points': {'over': 222.5, 'under': 222.5}}, 'totals'), 222.5)

    def test_opp_side_asymmetry_rejected(self):
        with self.assertRaises(ValueError):
            market_reference({'points': {'home': -3.5, 'away': 4.5}}, 'spreads')

    def test_market_anchored_weight(self):
        a = anchor_forecast('basketball_nba', 'spreads', 8.0, 4.0)
        self.assertAlmostEqual(a['anchored'], 4.4)
        self.assertFalse(a['abstain'])

    def test_extreme_disagreement_abstains(self):
        a = anchor_forecast('basketball_nba', 'spreads', 35.0, 4.0)
        self.assertTrue(a['abstain'])
        self.assertEqual(a['anchored'], 4.0)

    def test_shift_cap(self):
        a = anchor_forecast('basketball_nba', 'totals', 245.0, 221.0)
        self.assertLessEqual(abs(a['shift']), PARAMETERS['basketball_nba']['totals']['max_shift'])

    def test_probability_cap(self):
        p = provisional_market_probability(.5, 80., 10.)
        self.assertAlmostEqual(p, .52)
        self.assertAlmostEqual(provisional_market_probability(.5, -80., 10.), .48)

    def test_zero_shift_equals_market(self):
        self.assertEqual(provisional_market_probability(.51, 0., 12.), .51)

    def test_std_floor(self):
        self.assertGreaterEqual(preperiod_std([1.,-1.]*40, 'basketball_nba', 'spreads'), 11.)
        self.assertIsNone(preperiod_std([1.] * 10, 'basketball_nba', 'spreads'))

    def test_name_map_past_only_and_ambiguous(self):
        before = dt.datetime(2026, 11, 1, tzinfo=UTC)
        games = [
            {'played_at': before-dt.timedelta(days=2), 'home_team': 'A', 'home_team_id': '1',
             'away_team': 'B', 'away_team_id': '2'},
            {'played_at': before+dt.timedelta(days=1), 'home_team': 'C', 'home_team_id': '3',
             'away_team': 'A', 'away_team_id': '9'},
        ]
        result = names_to_ids(games, before, lambda x: x.lower(), lambda x: x)
        self.assertEqual(result, {'a': '1', 'b': '2'})

    def test_ambiguous_name_map_rejected(self):
        before = dt.datetime(2026, 11, 1, tzinfo=UTC)
        games = [
            {'played_at': before, 'home_team': 'A', 'home_team_id': '1', 'away_team': 'B', 'away_team_id': '2'},
            {'played_at': before, 'home_team': 'A', 'home_team_id': '9', 'away_team': 'C', 'away_team_id': '3'},
        ]
        result = names_to_ids(games, before, lambda x: x.lower(), lambda x: x)
        self.assertNotIn('a', result)

    def test_future_quote_filters_timestamp(self):
        asof = dt.datetime(2026, 11, 12, 16, tzinfo=UTC)
        quote = {'kind': 'totals', 'captured_at': asof-dt.timedelta(hours=1)}
        later = {'kind': 'totals', 'captured_at': asof+dt.timedelta(minutes=1)}
        event = {'x': {'meta': {('home','away',asof+dt.timedelta(hours=3))},
                       'quotes': [quote, later]}}
        def choose(quotes, market):
            if market == 'totals':
                self.assertEqual(quotes, [quote])
            else:
                self.assertEqual(quotes, [])
            return quotes[0] if quotes else None
        rows, counts = eligible_future_quotes(event, asof, choose)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['quotes']['totals'], quote)

    def test_conflicting_event_metadata_rejected(self):
        asof = dt.datetime(2026, 11, 12, 16, tzinfo=UTC)
        future = asof+dt.timedelta(hours=3)
        event = {'x': {'meta': {('home','away',future), ('home','away',future+dt.timedelta(minutes=5))},
                       'quotes': []}}
        rows, counts = eligible_future_quotes(event, asof, lambda x,y: None)
        self.assertEqual(rows, [])
        self.assertEqual(counts['ambiguous_event_metadata'], 1)


if __name__ == '__main__':
    unittest.main()
