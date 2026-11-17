"""Offline regression tests for college coverage and read-only moneyline audit."""
import sys
import unittest
from datetime import datetime, timezone
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


if __name__ == "__main__":
    unittest.main()
