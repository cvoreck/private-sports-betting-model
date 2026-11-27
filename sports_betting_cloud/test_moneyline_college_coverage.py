"""Offline regression tests for college coverage and read-only moneyline audit."""
import ast
import logging
import re
import sys
import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from espn_event_match import build_index, lookup, scoreboard_params, norm
from moneyline_adjustment_audit import adjust_probability, break_even, evaluate, extract_inputs


def event(eid, home="Kansas Jayhawks", away="UNLV Rebels", start="2026-11-17T21:00:00Z"):
    return {"id": eid, "date": start, "season": {"type": 2}, "competitions": [{
        "competitors": [
            {"homeAway": "home", "team": {"id": "2305", "displayName": home}},
            {"homeAway": "away", "team": {"id": "2439", "displayName": away}},
        ]}]}


class CollegeCoverageTests(unittest.TestCase):
    def test_division_one_scoreboard_params(self):
        p = scoreboard_params("mens-college-basketball", "20261117")
        self.assertEqual(p, {"dates": "20261117", "limit": 500, "groups": "50"})

    def test_other_league_unchanged(self):
        self.assertEqual(scoreboard_params("nba", "20261117"),
                         {"dates": "20261117", "limit": 200})

    def test_unique_exact_oriented_time_match(self):
        idx = build_index([event("1")])
        start = datetime(2026, 11, 17, 21, tzinfo=timezone.utc)
        self.assertIsNotNone(lookup(idx, "Kansas Jayhawks", "UNLV Rebels", start))
        self.assertIsNone(lookup(idx, "UNLV Rebels", "Kansas Jayhawks", start))
        self.assertIsNone(lookup(idx, "Kansas Jayhawks", "UNLV Rebels",
                                 datetime(2026, 11, 18, 4, tzinfo=timezone.utc)))

    def test_ambiguous_event_fails_closed(self):
        idx = build_index([event("1"), event("2")])
        start = datetime(2026, 11, 17, 21, tzinfo=timezone.utc)
        self.assertIsNone(lookup(idx, "Kansas Jayhawks", "UNLV Rebels", start))


class MarketAdjustmentTests(unittest.TestCase):
    NOTES = ("Why: MARKET-AWARE ESTIMATE: Raw trained-model probability 0.6500; "
             "same-bookmaker no-vig probability 0.5800.")

    def test_note_parser(self):
        self.assertEqual(extract_inputs(self.NOTES),
                         {"raw_model": .65, "fair_market": .58})
        self.assertIsNone(extract_inputs("published edge +2.0 pp"))

    def test_production_formula_matches(self):
        self.assertAlmostEqual(adjust_probability(.65, .58, .35, .01), .5945)

    def test_all_variants_descriptive_only(self):
        row = {"market": "h2h", "result": "Win", "notes": self.NOTES,
               "odds": -130, "stake": 1}
        result = evaluate([row])
        self.assertEqual(result["graded_annotated_moneyline_picks"], 1)
        self.assertEqual(len(result["variants"]), 6)
        self.assertTrue(all("passing_observed_only" in x for x in result["variants"]))
        self.assertTrue(any("selection-biased" in x.lower() for x in result["limitations"]))

    def test_excludes_unannotated_and_experimental(self):
        row = {"market": "spreads", "result": "Win", "notes": self.NOTES,
               "odds": -110, "stake": 1}
        self.assertEqual(evaluate([row])["status"], "insufficient_annotated_picks")


class SettlementCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load actual settlement functions without importing DB/network drivers.
        source = Path(__file__).with_name("settle_picks.py").read_text()
        tree = ast.parse(source)
        names = {"norm", "scoreboard", "teams_and_scores", "_neutral_college_reverse_match", "event_match", "grade_pick"}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        assert {n.name for n in nodes} == names
        cls.ns = {"re": re, "date": date, "datetime": datetime,
                  "PT": ZoneInfo("America/Los_Angeles"), "LOG": logging.getLogger("settlement-test"),
                  "ESPN": {"College Basketball": ("basketball", "mens-college-basketball"),
                           "NBA": ("basketball", "nba")},
                  "scoreboard_params": scoreboard_params}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "settle_picks.py", "exec"), cls.ns)

    @staticmethod
    def game(eid="fixture-1", start="2026-11-27T02:30:00Z", completed=True):
        result = event(eid, "USC Trojans", "Utah State Aggies", start)
        result["status"] = {"type": {"completed": completed}}
        competitors = result["competitions"][0]["competitors"]
        competitors[0]["score"] = "74"
        competitors[1]["score"] = "78"
        return result

    @staticmethod
    def pick():
        return {"id": 125, "date": "2026-11-26", "sport": "College Basketball", "matchup": "Utah State Aggies @ USC Trojans",
                "market": "h2h", "selection": "USC Trojans"}

    def test_full_college_scoreboard_settles_omitted_game(self):
        session = MagicMock()
        game = self.game()
        def get(url, params, timeout):
            response = MagicMock()
            response.json.return_value = {"events": [game] if params.get("groups") == "50" else []}
            return response
        session.get.side_effect = get
        events = self.ns["scoreboard"](session, "College Basketball", date(2026, 11, 26))
        session.get.assert_called_once_with(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
            params={"dates": "20261126", "limit": 500, "groups": "50"}, timeout=20)
        matched = self.ns["event_match"](self.pick(), events)
        self.assertIsNotNone(matched)
        self.assertEqual(self.ns["grade_pick"](self.pick(), matched[1]), "Loss")

    def test_non_college_request_unchanged(self):
        session = MagicMock()
        session.get.return_value.json.return_value = {"events": []}
        self.ns["scoreboard"](session, "NBA", date(2026, 11, 26))
        session.get.assert_called_once_with(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            params={"dates": "20261126", "limit": 500}, timeout=20)

    def test_nonfinal_and_wrong_orientation_still_unresolved(self):
        game = self.game(completed=False)
        matched = self.ns["event_match"](self.pick(), [game])
        self.assertIsNone(self.ns["grade_pick"](self.pick(), matched[1]))
        wrong = dict(self.pick(), matchup="USC Trojans @ Utah State Aggies")
        self.assertIsNone(self.ns["event_match"](wrong, [self.game()]))

    def test_adjacent_day_disambiguation_and_same_day_ambiguity(self):
        same = self.game()
        next_day = self.game("fixture-2", "2026-11-28T02:30:00Z")
        match = self.ns["event_match"](self.pick(), [next_day, same])
        self.assertEqual(match[0]["id"], same["id"])
        self.assertIsNone(self.ns["event_match"](self.pick(), [same, self.game("fixture-3")]))

    @staticmethod
    def reversed_game(eid="fixture-neutral", start="2026-11-26T21:30:00Z", neutral=True, completed=True):
        result = event(eid, "Utah State Aggies", "USC Trojans", start)
        result["status"] = {"type": {"completed": completed}}
        comp = result["competitions"][0]
        comp["neutralSite"] = neutral
        comp["competitors"][0].update({"score": "78", "team": {"id": "328", "displayName": "Utah State Aggies", "location": "Utah State", "name": "Aggies"}})
        comp["competitors"][1].update({"score": "74", "team": {"id": "30", "displayName": "USC Trojans", "location": "USC", "name": "Trojans"}})
        return result

    def test_neutral_reversal_grades_actual_team_scores(self):
        matched = self.ns["event_match"](self.pick(), [self.reversed_game()])
        self.assertIsNotNone(matched)
        self.assertEqual(self.ns["grade_pick"](self.pick(), matched[1]), "Loss")
        self.assertEqual(self.ns["grade_pick"](dict(self.pick(), selection="Utah State Aggies"), matched[1]), "Win")

    def test_neutral_reversal_rejects_wrong_date_sport_names_and_non_neutral(self):
        match = self.ns["event_match"]
        for game in (self.reversed_game(neutral=False), self.reversed_game(neutral=None),
                     self.reversed_game(start="2026-11-27T21:30:00Z"),
                     self.reversed_game(start="2026-11-26T21:30:00")):
            self.assertIsNone(match(self.pick(), [game]))
        self.assertIsNone(match(dict(self.pick(), sport="NBA"), [self.reversed_game()]))
        self.assertIsNone(match(dict(self.pick(), matchup="Aggies @ Trojans"), [self.reversed_game()]))

    def test_neutral_reversal_rejects_ambiguity_and_nonfinal(self):
        match = self.ns["event_match"]
        self.assertIsNone(match(self.pick(), [self.reversed_game(), self.game()]))
        self.assertIsNone(match(self.pick(), [self.reversed_game(), self.reversed_game(eid="fixture-2")]))
        pending = match(self.pick(), [self.reversed_game(completed=False)])
        self.assertIsNotNone(pending)
        self.assertIsNone(self.ns["grade_pick"](self.pick(), pending[1]))


if __name__ == "__main__":
    unittest.main()
