"""Offline regressions: no real API or database access."""
import ast
import datetime as dt
import io
import json
import logging
import os
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import urllib.error
import injury_fallbacks as f

NOW = dt.datetime(2026, 11, 25, 18, tzinfo=dt.timezone.utc)
START = NOW + dt.timedelta(hours=6)
HOME, AWAY = "Penn State Nittany Lions", "Ohio State Buckeyes"
EVENT = {"id": "123", "competitions": [{"conferenceCompetition": True, "competitors": [
    {"team": {"id": "213", "displayName": HOME, "location": "Penn State", "abbreviation": "PSU"}},
    {"team": {"id": "194", "displayName": AWAY, "location": "Ohio State", "abbreviation": "OSU"}}]}]}

def row(**overrides):
    return dict({"player_name": "Example Player", "team": "Penn State", "team_abbr": "PSU",
                 "status": "Out", "status_code": "out", "source": "conference_report",
                 "source_updated_at": NOW.isoformat()}, **overrides)

class InjuryFallbackTests(unittest.TestCase):
    def setUp(self):
        f.fetch_college_football.cache_clear()
        self.addCleanup(f.fetch_college_football.cache_clear)

    def test_exact_team_freshness_and_source(self):
        rows = [row(), row(player_name="Wrong school", team="Penn State Abington"),
                row(player_name="Wrong abbreviation", team_abbr="OSU"),
                row(player_name="Stale", source_updated_at=(NOW-dt.timedelta(hours=73)).isoformat()),
                row(player_name="Future", source_updated_at=START.isoformat()),
                row(player_name="Not official", source="espn"),
                row(player_name="Unknown", status_code="other"),
                row(player_name="No time", source_updated_at=None)]
        result = f.college_reports(EVENT, rows, START, NOW)
        self.assertEqual([x["player"] for x in result[HOME]], ["Example Player"])
        self.assertEqual(result[AWAY], [])
        self.assertIn("specific matchup not independently verified", result[HOME][0]["note"])

    def test_no_fetch_for_nonconference_or_past_game(self):
        nonconference = {"competitions": [dict(EVENT["competitions"][0], conferenceCompetition=False)]}
        with patch.object(f, "fetch_college_football") as fetch:
            f.supplement_college_football(nonconference, {}, HOME, AWAY, START, NOW)
            f.supplement_college_football(EVENT, {}, HOME, AWAY, NOW, NOW)
            fetch.assert_not_called()

    def test_merge_primary_wins_and_bounded(self):
        existing = {HOME: [{"player": "Example Player", "status": "Questionable"}]}
        rows = [row()] + [row(player_name="Player " + str(i)) for i in range(20)]
        with patch.object(f, "fetch_college_football", return_value=rows):
            result = f.supplement_college_football(EVENT, existing, HOME, AWAY, START, NOW)
        self.assertEqual(len(result[HOME]), 8)
        self.assertEqual(result[HOME][0]["status"], "Questionable")

    def test_same_timestamp_conflicts_rejected(self):
        result = f.college_reports(EVENT, [row(), row(status="Questionable", status_code="questionable")], START, NOW)
        self.assertEqual(result[HOME], [])

    def test_no_credentials_on_manual_or_rerun(self):
        with patch.dict(os.environ, {"PROPLINE_API_KEY": "test", "GITHUB_EVENT_NAME": "workflow_dispatch",
                                    "GITHUB_RUN_ATTEMPT": "1"}, clear=True), patch.object(f.urllib.request, "build_opener") as opener:
            self.assertEqual(f.fetch_college_football(), ())
            opener.assert_not_called()
        f.fetch_college_football.cache_clear()
        with patch.dict(os.environ, {"PROPLINE_API_KEY": "test", "GITHUB_EVENT_NAME": "schedule",
                                    "GITHUB_RUN_ATTEMPT": "2"}, clear=True), patch.object(f.urllib.request, "build_opener") as opener:
            self.assertEqual(f.fetch_college_football(), ())
            opener.assert_not_called()

    def test_one_attempt_and_quota_headers(self):
        with patch.dict(os.environ, {"PROPLINE_API_KEY": "test", "GITHUB_EVENT_NAME": "schedule",
                                    "GITHUB_RUN_ATTEMPT": "1"}, clear=True), patch.object(f.urllib.request, "build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.headers = {"X-Daily-Limit": "1000", "X-Daily-Used": "2", "X-Daily-Remaining": "998"}
            response.read.return_value = json.dumps({"players": [row()]}).encode()
            self.assertEqual(len(f.fetch_college_football()), 1)
            self.assertEqual(len(f.fetch_college_football()), 1)
            opener.return_value.open.assert_called_once()
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url, f.PROPLINE_URL)
            self.assertNotIn("test", req.full_url)

    def test_reserve_and_rate_limit_fail_closed(self):
        with patch.dict(os.environ, {"PROPLINE_API_KEY": "test", "GITHUB_EVENT_NAME": "schedule",
                                    "GITHUB_RUN_ATTEMPT": "1"}, clear=True), patch.object(f.urllib.request, "build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.headers = {"X-Daily-Limit": "1000", "X-Daily-Used": "950", "X-Daily-Remaining": "50"}
            self.assertEqual(f.fetch_college_football(), ())
            self.assertEqual(f.fetch_college_football(), ())
            response.read.assert_not_called()
            opener.return_value.open.assert_called_once()
        f.fetch_college_football.cache_clear()
        with patch.dict(os.environ, {"PROPLINE_API_KEY": "test", "GITHUB_EVENT_NAME": "schedule",
                                    "GITHUB_RUN_ATTEMPT": "1"}, clear=True), patch.object(f.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError(f.PROPLINE_URL, 429, "limited", {}, io.BytesIO())
            self.assertEqual(f.fetch_college_football(), ())
            self.assertEqual(f.fetch_college_football(), ())
            opener.return_value.open.assert_called_once()

    def test_espn_summary_missing_team_only_and_cache(self):
        source = Path(__file__).with_name("matchup_research.py").read_text()
        nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in
                 {"supplement_event_injuries", "injury_reports_for_event", "norm"}]
        fake_get = MagicMock(return_value={"header": {"id": "123", "competitions": EVENT["competitions"]}})
        parse = MagicMock(return_value={"id:194": [{"player": "Away player"}], "id:213": [{"player": "Not replacement"}]})
        env = {"re": __import__("re"), "logging": logging, "requests": MagicMock(),
               "get_json": fake_get, "parse_espn_injuries": parse, "ESPN_INJURY_LEAGUES": {"NFL": ("football", "nfl")}}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "matchup_research.py", "exec"), env)
        primary = {"id:213": [{"player": "Primary"}]}
        cache = {}
        run = env["supplement_event_injuries"]
        result = run(EVENT, "NFL", primary, HOME, AWAY, cache)
        self.assertEqual(result[HOME], primary["id:213"])
        self.assertEqual(result[AWAY], [{"player": "Away player"}])
        run(EVENT, "NFL", primary, HOME, AWAY, cache)
        fake_get.assert_called_once()
        run(EVENT, "College Basketball", {}, HOME, AWAY, {})
        fake_get.assert_called_once()
        run(EVENT, "NFL", {}, HOME, AWAY, {("NFL", str(i)): None for i in range(5)})
        fake_get.assert_called_once()
        fake_get.return_value = {"header": {"id": "999", "competitions": EVENT["competitions"]}}
        self.assertEqual(run(EVENT, "NFL", {}, HOME, AWAY, {})[AWAY], [])
