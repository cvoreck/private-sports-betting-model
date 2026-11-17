"""Phase 6B read-only archive-final fallback; no database or external API."""
import datetime as dt
import unittest

from test_phase6b_shadow_tracker import NOW, sample_final, sample_forecast
from phase6b_shadow_tracker import (UTC, apply_grades, add_new_forecasts, digest,
                                    empty_ledger, fetch_relevant_finals, find_unique_final)


def espn_snapshot(score_home="110", score_away="104", completed=True, season=2):
    start = NOW + dt.timedelta(hours=12)
    return {"event_id": "espn-123", "collected_at": NOW + dt.timedelta(days=1),
            "payload": {
                "date": start.isoformat(), "season": {"type": season},
                "status": {"type": {"completed": completed}},
                "competitions": [{"competitors": [
                    {"homeAway": "home", "score": score_home,
                     "team": {"id": "1", "displayName": "Boston Celtics"}},
                    {"homeAway": "away", "score": score_away,
                     "team": {"id": "2", "displayName": "Miami Heat"}},
                ]}]}}


class FakeConnection:
    def __init__(self, canonical=None, snapshots=None):
        self.canonical = canonical or []
        self.snapshots = snapshots or []
        self.queries = []
        self.last_sql = None

    def execute(self, sql, args):
        self.last_sql = sql
        self.queries.append((sql, args))
        return self

    def fetchall(self):
        if "FROM canonical_completed_games" in self.last_sql:
            return self.canonical
        if "FROM historical_snapshots" in self.last_sql:
            return self.snapshots
        raise AssertionError("Unexpected query")


def due_ledger():
    ledger = empty_ledger(NOW)
    add_new_forecasts(ledger, [sample_forecast()], NOW)
    return ledger


class ArchiveFinalFallbackTests(unittest.TestCase):
    def test_recovers_missing_canonical_final_and_grades_immutable(self):
        ledger = due_ledger()
        now = NOW + dt.timedelta(days=3)
        conn = FakeConnection(snapshots=[espn_snapshot()])
        finals, counts = fetch_relevant_finals(conn, ledger, now)
        self.assertEqual(counts["basketball_nba"]["rows"], 0)
        self.assertEqual(counts["basketball_nba"]["snapshot_finals_added"], 1)
        self.assertEqual(len(conn.queries), 2)
        sql, args = conn.queries[1]
        self.assertIn("source='espn'", sql)
        self.assertIn("category='game'", sql)
        self.assertIn("payload #>> '{status,type,completed}'='true'", sql)
        self.assertEqual(args[0], "basketball_nba")
        self.assertIn("20261113", args[1])
        final, reason = find_unique_final(ledger["forecasts"][0]["forecast"],
                                          finals["basketball_nba"])
        self.assertIsNone(reason)
        grade = apply_grades(ledger, finals, {}, now)
        self.assertEqual(grade["counts"]["newly_graded"], 1)
        self.assertEqual(ledger["forecasts"][0]["grade"]["home_score"], 110)
        self.assertEqual(ledger["forecasts"][0]["grade_sha256"],
                         digest(ledger["forecasts"][0]["grade"]))

    def test_rejects_unfinished_preseason_and_broken_score(self):
        now = NOW + dt.timedelta(days=3)
        for snapshot in (espn_snapshot(completed=False),
                         espn_snapshot(season=1),
                         espn_snapshot(score_home="unavailable")):
            with self.subTest(snapshot=snapshot):
                finals, counts = fetch_relevant_finals(
                    FakeConnection(snapshots=[snapshot]), due_ledger(), now)
                self.assertEqual(finals["basketball_nba"], [])
                self.assertEqual(counts["basketball_nba"]["snapshot_finals_added"], 0)

    def test_canonical_finishes_with_no_snapshot_query(self):
        canonical = sample_final()
        conn = FakeConnection(canonical=[canonical], snapshots=[espn_snapshot()])
        finals, counts = fetch_relevant_finals(conn, due_ledger(), NOW + dt.timedelta(days=3))
        self.assertEqual(counts["basketball_nba"]["rows"], 1)
        self.assertEqual(len(conn.queries), 1)
        self.assertEqual(len(finals["basketball_nba"]), 1)

    def test_conflicting_scores_fail_closed(self):
        # Different canonical event ID keeps canonical present but unmatched,
        # forcing snapshot verification for the contested ESPN event ID.
        canon = sample_final(start=NOW + dt.timedelta(hours=40), home=109, away=101)
        conn = FakeConnection(canonical=[canon], snapshots=[espn_snapshot()])
        with self.assertRaisesRegex(ValueError, "Canonical/ESPN final score conflict"):
            fetch_relevant_finals(conn, due_ledger(), NOW+dt.timedelta(days=3))

    def test_before_result_lag_has_no_database_reads(self):
        conn = FakeConnection(snapshots=[espn_snapshot()])
        finals, counts = fetch_relevant_finals(conn, due_ledger(), NOW)
        self.assertEqual(finals, {})
        self.assertEqual(counts, {})
        self.assertEqual(conn.queries, [])


if __name__ == "__main__":
    unittest.main()
