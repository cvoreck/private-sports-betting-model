"""Standalone selection tests; no database, network or paid APIs."""
import ast, json, logging, math, unittest
from pathlib import Path
source = Path(__file__).with_name('daily_top5.py').read_text()
tree = ast.parse(source)
funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('american_break_even', 'expected_value', 'value_adjustment', 'rank_candidates')]
ns = {'math': math, 'json': json, 'logging': logging, 'MIN_NEGATIVE_AMERICAN_ODDS': -200, 'MAX_POSITIVE_AMERICAN_ODDS': 200, 'RANKING_SHRINKAGE': {'trained': .8, 'experimental': .6}, 'RANKING_UNCERTAINTY_PP': {'trained': 1., 'experimental': 1.}}
exec(compile(ast.Module(body=funcs, type_ignores=[]), 'daily_top5.py', 'exec'), ns)
def candidate(eid, selection, p, market='spreads', point=-2.5, odds=-110):
    return dict(event_id=eid, selection=selection, market=market, point=point, odds=odds, prob=p, ev=ns['expected_value'](p,odds), edge_pp=100*(p-ns['american_break_even'](odds)), quality='experimental', sport='NBA')
class TestTiered(unittest.TestCase):
    def test_five_from_distinct_events(self):
        bets = [candidate(str(i),'Home',.60) for i in range(5)]
        result=ns['rank_candidates'](bets)
        self.assertEqual(len(result),5)
        self.assertEqual(len({c['event_id'] for c in result}),5)
    def test_raw_fallback(self):
        bets = [candidate(str(i),'Home',.54) for i in range(5)]
        result=ns['rank_candidates'](bets)
        self.assertEqual(len(result),5)
        self.assertTrue(any(c['selection_tier']==2 for c in result))
    def test_same_game_fallback_and_no_opposing_side(self):
        bets = [candidate(str(i),'Home',.60) for i in range(3)]
        bets += [candidate('0','Over',.57,'totals',220.5),candidate('1','Under',.57,'totals',220.5)]
        bets += [candidate('0','Away',.59,'spreads',-2.5)]
        result=ns['rank_candidates'](bets)
        self.assertEqual(len(result),5)
        self.assertEqual(sum(c['same_game_exposure'] for c in result),2)
        self.assertFalse(any(c['selection']=='Away' for c in result))
    def test_insufficient_remains_insufficient(self):
        self.assertEqual(len(ns['rank_candidates']([candidate('0','Home',.54)])),1)
    def test_negative_raw_ev_never_selected(self):
        self.assertEqual(ns['rank_candidates']([candidate('0','Home',.45)]),[])
if __name__=='__main__': unittest.main()
