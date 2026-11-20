"""Offline regression for per-sport diagnostic counters (zero network / DB access).

Tests the real publisher candidate-selection function with a fake connection
that counts its existing SQL reads, and a synthetic cached-odds fixture.
"""
import ast
from datetime import date, datetime, timedelta, timezone
import json
import logging
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).parent
PUBLISHER = (ROOT / "daily_top5.py").read_text(encoding="utf-8")
ENGINE = (ROOT / "prediction_engine.py").read_text(encoding="utf-8")

tree = ast.parse(PUBLISHER)
names = {"find_candidates", "american_break_even", "expected_value",
         "no_vig_probability", "value_adjustment", "rank_candidates"}
functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
assert {f.name for f in functions} == names

SPORT_KEYS = {
    "americanfootball_nfl": "NFL",
    "basketball_nba": "NBA",
    "baseball_mlb": "MLB",
    "americanfootball_ncaaf": "College Football",
    "icehockey_nhl": "NHL",
    "basketball_ncaab": "College Basketball",
}
ns = dict(
    json=json, logging=logging, math=math,
    datetime=datetime, timedelta=timedelta, PT=ZoneInfo("America/Los_Angeles"),
    requests=SimpleNamespace(HTTPError=RuntimeError),
    SPORT_KEYS=SPORT_KEYS, COLLEGE={"College Football", "College Basketball"},
    MAX_POSITIVE_AMERICAN_ODDS=200, MIN_NEGATIVE_AMERICAN_ODDS=-200,
    MAX_PREDICTION_AGE_HOURS=3, TRAINED_PREFIXES=("rolling-homewin-v2-", "nhl-context-v3-"),
    EXPERIMENTAL_PREFIX="archive-market-shrink-v1-experimental",
    MODEL_WEIGHT=.35, UNCERTAINTY_HAIRCUT_PP=1.0,
)
exec(compile(ast.Module(body=functions, type_ignores=[]),
             "daily_top5.py", "exec"), ns)


def event(eid, markets):
    return {
        "id": eid, "commence_time": "2026-11-21T01:00:00+00:00",
        "home_team": "Home", "away_team": "Away",
        "bookmakers": [{"title": "Fake Book", "markets": markets}],
    }


def market(key, left, right, odds_left=-110, odds_right=-110, point=None):
    return {"key": key, "outcomes": [
        {"name": left, "price": odds_left, **({"point": point} if point is not None else {})},
        {"name": right, "price": odds_right, **({"point": point} if point is not None else {})},
    ]}


def result(rows=None, row=None):
    return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: row)


class FakeConn:
    def __init__(self):
        self.statements = []
        self.promoted = [
            {"sport": k, "version": "rolling-homewin-v2-test"}
            for k in ("basketball_nba", "americanfootball_nfl",
                      "americanfootball_ncaaf", "basketball_ncaab")
        ]
        self.verified = {
            "nba": {"ap_top25_team_in_match": None, "verification_source": "ESPN"},
            "nhl": {"ap_top25_team_in_match": None, "verification_source": "ESPN"},
            "cfb_ranked": {"ap_top25_team_in_match": True, "verification_source": "AP"},
            "cfb_unranked": {"ap_top25_team_in_match": False, "verification_source": "AP"},
            "cbb": {"ap_top25_team_in_match": True, "verification_source": "AP"},
        }
        self.predictions = {
            ("nba", "h2h", "Home", ""): {"estimated_prob": .70, "model_version": "rolling-homewin-v2-test"},
            ("nba", "h2h", "Away", ""): {"estimated_prob": .30, "model_version": "rolling-homewin-v2-test"},
            ("nba", "spreads", "Home", "-2.5"):
                {"estimated_prob": .60, "model_version": "archive-market-shrink-v1-experimental"},
            ("nba", "spreads", "Away", "-2.5"):
                {"estimated_prob": .40, "model_version": "archive-market-shrink-v1-experimental"},
            ("cbb", "h2h", "Home", ""): {"estimated_prob": .51, "model_version": "rolling-homewin-v2-test"},
            ("cbb", "h2h", "Away", ""): {"estimated_prob": .49, "model_version": "rolling-homewin-v2-test"},
        }

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        if "FROM sports_edge_trained_models" in statement:
            return result(rows=self.promoted)
        if "FROM verified_events" in statement:
            return result(row=self.verified.get(params[0]))
        if "FROM prediction_context" in statement:
            return result(row={})
        if "FROM model_predictions" in statement:
            return result(row=self.predictions.get(tuple(params[:4])))
        raise AssertionError("Unexpected SQL issued by diagnostics: " + statement)


class PerSportDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConn()
        odds = {
            "basketball_nba": [
                event("nba", [
                    market("h2h", "Home", "Away"),
                    market("spreads", "Home", "Away", point=-2.5),
                ])
            ],
            "icehockey_nhl": [event("nhl", [market("h2h", "Home", "Away")])],
            "americanfootball_ncaaf": [
                event("cfb_ranked", [market("h2h", "Home", "Away", -350, 300)]),
                event("cfb_unranked", [market("h2h", "Home", "Away")]),
                event("cfb_missing", [market("h2h", "Home", "Away")]),
            ],
            "basketball_ncaab": [
                event("cbb", [market("h2h", "Home", "Away")])
            ],
        }
        ns["odds_events"] = lambda conn, key, day: odds.get(key, [])

    def test_funnel_counts_do_not_alter_candidate_selection_or_add_sql(self):
        today = date(2026, 11, 20)
        now = datetime(2026, 11, 20, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        with self.assertLogs(level="INFO") as captured:
            candidates = ns["find_candidates"](self.conn, today, now)
        log = next(x for x in captured.output
                   if "Per-sport publisher rejection funnel" in x)
        funnel = json.loads(log.split("): ", 1)[1])

        # The NBA produces the only eligible outcome in the synthetic fixture.
        self.assertEqual([(c["sport"], c["selection"]) for c in candidates],
                         [("NBA", "Home")])
        selected = ns["rank_candidates"](candidates, today)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["sport"], "NBA")

        nba = funnel["NBA"]
        self.assertEqual(nba["events"]["cached_events"], 1)
        self.assertEqual(nba["events"]["verified_events"], 1)
        self.assertEqual(nba["moneyline_quotes"]["seen"], 2)
        self.assertEqual(nba["moneyline_quotes"]["positive_raw_ev"], 1)
        self.assertEqual(nba["moneyline_quotes"]["nonpositive_raw_ev"], 1)
        self.assertEqual(nba["spread_total_quotes"]["shadow_only_not_publishable"], 2)

        nhl = funnel["NHL"]
        self.assertFalse(nhl["promoted_moneyline_model"])
        self.assertEqual(nhl["events"]["verified_events"], 1)
        self.assertEqual(nhl["moneyline_quotes"]["missing_fresh_prediction"], 2)

        cfb = funnel["College Football"]
        self.assertEqual(cfb["events"]["cached_events"], 3)
        self.assertEqual(cfb["events"]["upcoming_events"], 3)
        self.assertEqual(cfb["events"]["verified_events"], 1)
        self.assertEqual(cfb["events"]["ap_top25_not_eligible"], 1)
        self.assertEqual(cfb["events"]["no_verified_event"], 1)
        self.assertEqual(cfb["moneyline_quotes"]["outside_price_limits"], 2)
        self.assertEqual(cfb["moneyline_quotes"]["seen"], 2)

        cbb = funnel["College Basketball"]
        self.assertEqual(cbb["moneyline_quotes"]["nonpositive_raw_ev"], 2)
        self.assertEqual(cbb["moneyline_quotes"]["positive_raw_ev"], 0)
        self.assertEqual(funnel["NFL"]["events"]["cached_events"], 0)
        self.assertEqual(funnel["MLB"]["moneyline_quotes"]["seen"], 0)

        # Existing reads only: 1 promoted-model lookup, 6 event-verification
        # lookups, 4 context lookups and 8 price-qualified prediction lookups.
        self.assertEqual(len(self.conn.statements), 19)
        self.assertTrue(all(stmt.lstrip().startswith("SELECT ")
                            for stmt, _ in self.conn.statements))

    def test_engine_instrumentation_is_log_only(self):
        engine = ast.parse(ENGINE)
        main = next(n for n in engine.body if isinstance(n, ast.FunctionDef)
                    and n.name == "main")
        names = {n.id for n in ast.walk(main) if isinstance(n, ast.Name)}
        self.assertIn("sport_funnel", names)
        self.assertIn("json", names)
        self.assertIn("Prediction engine per-sport event funnel:", ENGINE)
        self.assertIn("Per-sport Top 5 selections (in-memory, before publication):",
                      PUBLISHER)


if __name__ == "__main__":
    unittest.main()
