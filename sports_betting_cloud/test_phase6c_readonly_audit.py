"""Offline tests for the immutable, read-only Phase 6C audit (no DB/network)."""
import copy
import datetime as dt
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase6c_readonly_audit import (analyze, markdown_report, paired_bootstrap,
                                    sha256_json, validate_ledger)

NOW = dt.datetime(2026, 11, 18, 16, 0, tzinfo=dt.timezone.utc)


def item(eid="1", market="spreads", home=110, away=100, status="SHADOW_ONLY",
         grade_present=True, model_version="phase6a-market-anchor-v1-SHADOW"):
    start = NOW - dt.timedelta(days=3)
    record_time = start - dt.timedelta(hours=12)
    source_time = record_time - dt.timedelta(hours=1)
    reference = 8.5 if market == "spreads" else 210.5
    anchor = 9.1 if market == "spreads" else 211.2
    trained = 10.2 if market == "spreads" else 212
    baseline = 10 if market == "spreads" else 211
    sides = ("home", "away") if market == "spreads" else ("over", "under")
    selection = {
        side: {
            "point": ((-reference if side == "home" else reference)
                      if market == "spreads" else reference),
            "market_no_vig_conditional": .53 if side in ("home", "over") else .47,
            "shadow_conditional_nonpush": .55 if side in ("home", "over") else .45,
        }
        for side in sides
    }
    fc = {
        "sport": "basketball_nba", "odds_event_id": eid,
        "market": market, "model_version": model_version,
        "status": status, "home_team": "boston celtics", "away_team": "new york knicks",
        "event_start": start.isoformat(),
        "source_quote_captured_at": source_time.isoformat(),
        "market_reference": reference, "baseline_forecast": baseline,
        "trained_forecast": trained, "anchored_forecast": anchor,
        "selections": selection,
    }
    actual = home-away if market == "spreads" else home+away
    result = {"home": "W", "away": "L"} if market == "spreads" else {"over": "L", "under": "W"}
    if market == "totals" and actual > reference:
        result = {"over": "W", "under": "L"}
    grade = {
        "final_event_id": "final-" + eid, "home_score": home, "away_score": away,
        "canonical_played_at": start.isoformat(), "actual_value": actual,
        "graded_at": NOW.isoformat(),
        "side_results": result,
        "absolute_errors": {
            "market": abs(actual-reference),
            "anchored": abs(actual-anchor),
            "baseline": abs(actual-baseline),
            "trained": abs(actual-trained),
        }
    }
    return {
        "key": "|".join((fc["sport"], eid, market, fc["model_version"])),
        "forecast": fc, "forecast_sha256": sha256_json(fc),
        "first_recorded_at": record_time.isoformat(),
        "grade": grade if grade_present else None,
        "grade_sha256": sha256_json(grade) if grade_present else None,
        "latest_observed_pregame_quote": None,
    }


def ledger(*items):
    return {"schema_version": 1, "updated_at": NOW.isoformat(),
            "created_at": (NOW-dt.timedelta(days=5)).isoformat(),
            "forecasts": list(items)}


def rehash(record, grade=False):
    k = "grade" if grade else "forecast"
    record[k+"_sha256"] = sha256_json(record[k])


class Phase6CReadOnlyTests(unittest.TestCase):
    def test_two_markets_one_independent_game(self):
        l = ledger(item(market="spreads"), item(market="totals"))
        report = analyze(l, ledger_commit="abcd")
        self.assertEqual(report["forecasts_total"], 2)
        self.assertEqual(report["graded_total"], 2)
        self.assertEqual(report["unique_graded_games_total"], 1)
        self.assertEqual(report["games_with_both_graded_spread_and_total"], 1)
        self.assertEqual(report["unique_graded_games_by_sport"]["basketball_nba"], 1)
        self.assertEqual(report["resources"]["paid_odds_api_requests"], 0)
        self.assertEqual(report["resources"]["database_queries"], 0)
        self.assertEqual(report["resources"]["production_changes"], 0)

    def test_forecast_market_error_improvement_sign_and_no_roi(self):
        l = ledger(item(market="spreads"))
        report = analyze(l)
        v = report["markets"]["basketball_nba"]["spreads"]["phase6a-market-anchor-v1-SHADOW"]
        self.assertEqual(v["graded_evaluated_games"], 1)
        # market 8.5 vs 10 actual => abs 1.5; anchored 9.1 => abs .9
        self.assertAlmostEqual(v["improvement"]["anchored_vs_market_mean_market_minus_anchored"], .6)
        self.assertEqual(v["mae"]["market"], 1.5)
        self.assertEqual(v["mae"]["anchored"], .9)
        self.assertEqual(v["probabilities"]["nonpush_games"], 1)
        metric = v
        self.assertNotIn("roi", metric)
        self.assertNotIn("profit", metric)

    def test_grade_hash_cannot_change(self):
        l = ledger(item())
        l["forecasts"][0]["grade"]["home_score"] += 1
        with self.assertRaisesRegex(ValueError, "Grade hash mismatch"):
            analyze(l)

    def test_forecast_hash_cannot_change(self):
        l = ledger(item())
        l["forecasts"][0]["forecast"]["anchored_forecast"] = 999
        with self.assertRaisesRegex(ValueError, "Forecast hash mismatch"):
            analyze(l)

    def test_reject_conflicting_results_same_event(self):
        b = item(market="totals")
        b["grade"]["home_score"] = 100
        b["grade"]["actual_value"] = 200
        b["grade"]["side_results"] = {"over": "L", "under": "W"}
        b["grade"]["absolute_errors"] = {
            "market": 10.5, "anchored": 11.2, "baseline": 11, "trained": 12}
        rehash(b, grade=True)
        with self.assertRaisesRegex(ValueError, "Conflicting frozen finals"):
            analyze(ledger(item(), b))

    def test_no_future_data_as_pregame(self):
        a = item()
        a["first_recorded_at"] = a["forecast"]["event_start"]
        with self.assertRaisesRegex(ValueError, "Nonpregame"):
            analyze(ledger(a))

    def test_pending_excluded_from_accuracy_and_counted(self):
        l = ledger(item(grade_present=False), item(eid="2"))
        v = analyze(l)
        self.assertEqual(v["pending_forecasts"], 1)
        self.assertEqual(v["graded_total"], 1)
        self.assertEqual(v["unique_graded_games_total"], 1)

    def test_duplicate_forecast_rejected(self):
        a = item()
        with self.assertRaisesRegex(ValueError, "duplicate immutable"):
            analyze(ledger(a, copy.deepcopy(a)))

    def test_push_is_excluded_only_from_brier(self):
        # total equals 210 and line is exactly 210
        a = item(market="totals")
        a["forecast"]["market_reference"] = 210
        a["forecast"]["selections"]["over"]["point"] = 210
        a["forecast"]["selections"]["under"]["point"] = 210
        a["grade"]["side_results"] = {"over": "P", "under": "P"}
        a["grade"]["absolute_errors"]["market"] = 0
        rehash(a)
        rehash(a, grade=True)
        x = analyze(ledger(a))["markets"]["basketball_nba"]["totals"][a["forecast"]["model_version"]]
        self.assertEqual(x["graded_evaluated_games"], 1)
        self.assertEqual(x["probabilities"]["nonpush_games"], 0)
        self.assertEqual(x["probabilities"]["pushes_excluded"], 1)
        self.assertIsNone(x["probabilities"]["brier_market_no_vig"])

    def test_abstentions_excluded_but_retained(self):
        x = item(status="ABSTAIN")
        result = analyze(ledger(x))
        m = result["markets"]["basketball_nba"]["spreads"][x["forecast"]["model_version"]]
        self.assertEqual(m["graded"], 1)
        self.assertEqual(m["graded_evaluated_games"], 0)
        self.assertEqual(m["abstained"], 1)

    def test_paired_ci_is_deterministic_and_requires_multiple_games(self):
        values = [0.1, -0.25, 0.03, 0.15, 0.5, -0.3]
        a = paired_bootstrap(values, 1234, repetitions=1000)
        b = paired_bootstrap(values, 1234, repetitions=1000)
        self.assertEqual(a, b)
        self.assertIsNone(paired_bootstrap([.5], 1234))
        self.assertLessEqual(a[0], a[1])

    def test_markdown_explicit_uncertainty_and_resource_use(self):
        text = markdown_report(analyze(ledger(item(),item(market="totals")),
                                       ledger_commit="deadbeef"))
        self.assertIn("paired bootstrap", text)
        self.assertIn("uncalibrated", text.lower())
        self.assertIn("No paid Odds API calls", text)
        self.assertIn("deadbeef", text)
        self.assertIn("no-vig", text)


if __name__ == "__main__":
    unittest.main()
