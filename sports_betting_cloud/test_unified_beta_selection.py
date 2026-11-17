"""Offline safety regressions for unified beta selection (no network/database)."""
import ast
import json
import logging
import math
import unittest
from pathlib import Path

source = Path(__file__).with_name("daily_top5.py").read_text()
tree = ast.parse(source)
names = {"american_break_even", "expected_value", "value_adjustment", "rank_candidates"}
funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
assert {node.name for node in funcs} == names
ns = {
    "math": math, "json": json, "logging": logging,
    "MIN_NEGATIVE_AMERICAN_ODDS": -200, "MAX_POSITIVE_AMERICAN_ODDS": 200,
    "MODEL_WEIGHT": 0.35, "UNCERTAINTY_HAIRCUT_PP": 1.0,
}
exec(compile(ast.Module(body=funcs, type_ignores=[]), "daily_top5.py", "exec"), ns)


def candidate(eid, prob, fair, selection="Home", market="h2h",
              quality="trained", odds=-110):
    ev = ns["expected_value"](prob, odds)
    return dict(event_id=eid, sport="NBA", matchup="Away @ Home", selection=selection,
                market=market, quality=quality, point=None,
                odds=odds, prob=prob, ev=ev, market_fair_prob=fair,
                edge_pp=100*(prob - ns["american_break_even"](odds)))


class TestUnifiedBetaSelection(unittest.TestCase):
    def test_strict_precedes_provisional_and_both_count(self):
        bets = [candidate("p1", .55, .50), candidate("s1", .70, .57),
                candidate("p2", .55, .50), candidate("s2", .66, .57),
                candidate("p3", .55, .50)]
        selected = ns["rank_candidates"](bets)
        self.assertEqual(len(selected), 5)
        self.assertEqual([c["selection_tier"] for c in selected], [1,1,2,2,2])
        self.assertEqual(len({c["event_id"] for c in selected}),5)

    def test_provisional_has_negative_adjusted_ev_but_positive_raw_ev(self):
        selected = ns["rank_candidates"]([candidate("1", .55, .50)])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["selection_tier"], 2)
        self.assertGreater(selected[0]["ev"], 0)
        self.assertLessEqual(selected[0]["adjusted_ev"], 0)
        self.assertLess(selected[0]["adjusted_prob"],
                        ns["american_break_even"](selected[0]["odds"]))

    def test_only_one_side_per_game_even_when_two_are_positive(self):
        same = [candidate("1", .65, .56, "Home", odds=-110),
                candidate("1", .65, .56, "Away", odds=-110),
                candidate("2", .55, .50)]
        selected = ns["rank_candidates"](same)
        self.assertEqual(len(selected),2)
        self.assertEqual(len({c["event_id"] for c in selected}),2)
        self.assertTrue(all(c["same_game_exposure"] is False for c in selected))

    def test_unapproved_market_and_raw_negative_ev_excluded(self):
        bets = [candidate("1", .9, .6, market="spreads"),
                candidate("2", .9, .6, quality="experimental"),
                candidate("3", .40, .50)]
        self.assertEqual(ns["rank_candidates"](bets), [])

    def test_odds_limits_enforced_and_no_fifth_fabricated(self):
        bets = [candidate("1", .7, .6, odds=210),
                candidate("2", .7, .6, odds=-210),
                candidate("3", .55, .50)]
        selected = ns["rank_candidates"](bets)
        self.assertEqual(len(selected),1)
        self.assertEqual(selected[0]["event_id"], "3")

    def test_invalid_market_fair_prob_never_selected(self):
        self.assertEqual(ns["rank_candidates"]([candidate("1", .6, None)]), [])


class TestRecordPresentation(unittest.TestCase):
    def test_single_existing_ledger_and_provisional_warning(self):
        self.assertIn("PROVISIONAL BETA PICK", source)
        self.assertIn("c['adjusted_prob']", source)
        self.assertIn("'Hypothetical','Pending'", source)
        self.assertIn("same testing-period W-L-P", source)
        app = Path(__file__).with_name("app.py").read_text()
        self.assertIn("'PROVISIONAL BETA PICK' in notes_text", app)
        self.assertIn("Conserv. Edge", app)
        self.assertIn("Raw model {raw_edge_text}", app)
        self.assertNotIn("tier_label", app)

    def test_raw_model_edge_parser_handles_archived_notes(self):
        app = Path(__file__).with_name("app.py").read_text()
        tree = ast.parse(app)
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == "_raw_model_probability_in_saved_notes"]
        self.assertEqual(len(funcs), 1)
        import re
        env = {"re": re}
        exec(compile(ast.Module(body=funcs, type_ignores=[]),
                     "app.py", "exec"), env)
        parse = env["_raw_model_probability_in_saved_notes"]
        notes = ("Why: MARKET-AWARE ESTIMATE: published probability 0.5610; "
                 "Raw trained-model probability 0.6228; "
                 "same-bookmaker no-vig probability 0.5700.")
        self.assertAlmostEqual(parse(notes), 0.6228)
        self.assertIsNone(parse("No raw model estimate recorded"))
        self.assertIsNone(parse("Raw trained-model probability 1.2"))
        self.assertIsNone(parse("Raw trained-model probability 0"))


if __name__ == "__main__":
    unittest.main()
