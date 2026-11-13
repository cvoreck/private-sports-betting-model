"""Pure unit tests; no DB access, calls, credentials or prod changes."""
import datetime as dt
import unittest

from phase5b_real_line_backtest import (
    empirical_probs, as_result, choose_quote, quote_from_market, choose_side,
    features_as_of, american_decimal_profit, settle, summary, evaluate_sport, UTC)
from spread_total_research_phase2 import build_examples


class Phase5BTests(unittest.TestCase):
    def test_american_odds(self):
        self.assertAlmostEqual(american_decimal_profit(-110),100/110)
        self.assertEqual(american_decimal_profit(150),1.5)
        self.assertIsNone(american_decimal_profit(1.91))

    def test_real_pushes(self):
        self.assertEqual(as_result(6, 'spreads', 'home', -6),'P')
        self.assertEqual(as_result(6, 'spreads', 'away', 6),'P')
        self.assertEqual(as_result(212,'totals','under',212),'P')
        self.assertEqual(as_result(213,'totals','over',212.5),'W')

    def test_price_parsing_and_opposite_spreads(self):
        raw = {'key':'spreads','outcomes':[
            {'name':'Boston Celtics','point':-5.5,'price':-110},
            {'name':'Miami Heat','point':5.5,'price':-110}]}
        parsed=quote_from_market('spreads',raw,'boston celtics','miami heat')
        self.assertEqual(parsed['points']['home'],-5.5)
        raw['outcomes'][1]['point']=6.5
        self.assertIsNone(quote_from_market('spreads',raw,'boston celtics','miami heat'))

    def test_deterministic_latest_snapshot(self):
        t=dt.datetime(2026,11,1,20,tzinfo=UTC)
        q={'kind':'totals','points':{'over':211.5,'under':211.5},
           'prices':{'over':-110,'under':-110},'updated_at':None,
           'snapshot_category':'pregame_odds'}
        latest=choose_quote([{**q,'book':'fanduel','captured_at':t},
                             {**q,'book':'draftkings','captured_at':t},
                             {**q,'book':'draftkings','captured_at':t-dt.timedelta(hours=1)}], 'totals')
        self.assertEqual(latest['book'],'draftkings')

    def test_residual_push_smoothing(self):
        ps=empirical_probs(100,[0,1,-1],'totals','over',100)
        self.assertTrue(ps['P'] > 0)
        self.assertAlmostEqual(sum(ps.values()),1.0)

    def test_half_point_cannot_push(self):
        ps=empirical_probs(100,[0,1,-1],'totals','over',100.5)
        self.assertEqual(ps['P'],0.0)
        self.assertAlmostEqual(sum(ps.values()),1.0)

    def test_graded_bet(self):
        event={'home_score':105,'away_score':100}
        self.assertEqual(settle(event,'spreads',{'side':'home','point':-5,
                                                'american_odds':-110}),('P',0.0))

    def test_asof_excludes_not_yet_known_results(self):
        # Test a simplified 2-team history, with a recent game just 1 hour
        # before capture. This game must never enter the pregame history.
        base=dt.datetime(2026,10,30,20,tzinfo=UTC)
        games=[]
        for n in range(12):
            games.append({'event_id':str(n),'played_at':base-dt.timedelta(days=20-n),
                          'home_team_id':'A','away_team_id':'B',
                          'home_score':100+n, 'away_score':90+n})
        late={'event_id':'late','played_at':base-dt.timedelta(hours=1),
              'home_team_id':'A','away_team_id':'B',
              'home_score':300, 'away_score':10}
        games.append(late)
        final={'event_id':'target','home_team_id':'A','away_team_id':'B'}
        actual=features_as_of(games,base,final,'basketball_nba')
        expected=build_examples(games[:-1]+[{'event_id':'__phase5b_target__',
            'played_at':base,'home_team_id':'A','away_team_id':'B',
            'home_score':0,'away_score':0}], 'basketball_nba')[-1]
        self.assertEqual(actual['features'],expected['features'])

    def test_two_odds_ids_cannot_double_count_same_final(self):
        start = dt.datetime(2026,11,3,20,tzinfo=UTC)
        qtime = start-dt.timedelta(hours=4)
        def snapshot(odd_id):
            return {'category':'pregame_odds', 'event_id':odd_id,
                    'collected_at':qtime,
                    'payload':{'id':odd_id,'commence_time':start.isoformat(),
                        'home_team':'Boston Celtics','away_team':'Miami Heat',
                        'bookmakers':[{'key':'draftkings', 'markets':[
                          {'key':'totals','outcomes':[
                             {'name':'Over','point':201.5,'price':-110},
                             {'name':'Under','point':201.5,'price':-110}]}]}]}}
        class Cursor:
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def execute(self,*args): return None
            def __iter__(self): return iter((snapshot('odd1'),snapshot('odd2')))
        class FakeConnection:
            def execute(self,*args): return self
            def fetchall(self): return [{'event_id':'canonical1','played_at':start,
                  'home_team_id':'1','away_team_id':'2',
                  'home_team':'Boston Celtics','away_team':'Miami Heat',
                  'home_score':103,'away_score':99}]
            def cursor(self,*args,**kwargs): return Cursor()
        result=evaluate_sport(FakeConnection(),'basketball_nba',start+dt.timedelta(days=1))
        self.assertEqual(result['status'],'NO_ELIGIBLE_MATCHES')
        self.assertEqual(result['counts']['multiple_odds_events_same_final'],2)

    def test_side_selection_not_using_final_score(self):
        q={'points':{'over':200.5,'under':200.5},
           'prices':{'over':-110,'under':-110}}
        picked=choose_side(210,[-20,-5,0,5,20], 'totals',q)
        self.assertIn(picked['side'],('over','under'))
        self.assertAlmostEqual(picked['p_win']+picked['p_loss']+picked['p_push'],1)


if __name__=='__main__':
    unittest.main()
