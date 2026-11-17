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


class TestCompactResearchPresentation(unittest.TestCase):
    def test_only_one_team_comparison_not_three_duplicate_panels(self):
        app = Path(__file__).with_name("app.py").read_text()
        source = app[app.index("def full_pick_card("):app.index("# Render the dashboard once", app.index("def full_pick_card("))]
        self.assertIn("_se_unified_comparison_card(", source)
        self.assertIn("Team Comparison", app)
        self.assertNotIn("{recent_card}{season_card}{matchup_card}", source)
        self.assertNotIn("Key Takeaways", source)
        self.assertNotIn("Bottom Line</h3>", source)
        self.assertIn("market_strip = market_strip_html(", source)
        self.assertIn("Historical research snapshot", source)

    def test_comparison_merges_only_saved_statistics(self):
        app = Path(__file__).with_name("app.py").read_text()
        import re
        tree = ast.parse(app)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "_se_unified_comparison_card")
        env = {"re": re, "_se_comparison_card": lambda title, teams, rows, note, symbol:
               (title, rows, note), "_insight_card": lambda *args: "unavailable"}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), env)
        sections = {"season": ["Portland Trail Blazers 9-4; Minnesota Timberwolves 10-4"],
                    "home": ["Portland Trail Blazers on road 4-2; Minnesota Timberwolves at home 5-2"],
                    "recent": []}
        stats = {"Portland Trail Blazers":{"record":"6–4","games":10,"gf":"114.5","ga":"111.6"},
                 "Minnesota Timberwolves":{"record":"7–3","games":10,"gf":"115.6","ga":"106.6"}}
        title, rows, note = env["_se_unified_comparison_card"](
            sections, list(stats), stats, "NBA")
        self.assertEqual(title, "Team Comparison")
        self.assertEqual(len(rows), 5)
        self.assertEqual(sum("For / Game" in label for label, _ in rows), 0)
        self.assertEqual(rows[0][0], "Season record")
        self.assertEqual(rows[1][0], "Last 10 record")
        self.assertIn("10 recent games", note)

    def test_injury_list_is_disclosed_not_discarded(self):
        app = Path(__file__).with_name("app.py").read_text()
        import re
        from html import escape
        tree = ast.parse(app)
        relevant = [n for n in tree.body if
                    (isinstance(n, ast.FunctionDef) and n.name in
                     {"_se_injury_table", "_se_injury_priority", "_se_key_absences"}) or
                    (isinstance(n, ast.Assign) and any(
                     isinstance(t, ast.Name) and t.id == "_RECORDED_KEY_PLAYERS" for t in n.targets))]
        env = {"re": re, "esc": escape,
               "_team_key": lambda x: re.sub(r'[^a-z0-9]', '', str(x).casefold())}
        exec(compile(ast.Module(body=relevant, type_ignores=[]), "app.py", "exec"), env)
        injuries = {"Team A":[f"Player {i} — Out (Shoulder)" for i in range(5)]}
        html = env["_se_injury_table"](["Team A","Team B"], injuries)
        self.assertIn("Show 2 more reported injuries", html)
        self.assertIn("Player 4", html)
        self.assertIn("Player 0", html)
        self.assertIn("se-v4-extra", html)


    def test_saved_injury_details_do_not_truncate_before_cell_width(self):
        app = Path(__file__).with_name("app.py").read_text()
        import re
        from html import escape
        tree = ast.parse(app)
        relevant = [n for n in tree.body if
                    (isinstance(n, ast.FunctionDef) and n.name in
                     {"_se_injury_table", "_se_injury_priority", "_se_key_absences"}) or
                    (isinstance(n, ast.Assign) and any(
                     isinstance(t, ast.Name) and t.id == "_RECORDED_KEY_PLAYERS" for t in n.targets))]
        env = {"re": re, "esc": escape,
               "_team_key": lambda x: re.sub(r'[^a-z0-9]', '', str(x).casefold())}
        exec(compile(ast.Module(body=relevant, type_ignores=[]), "app.py", "exec"), env)
        detail = ("Anthony Edwards underwent a successful arthroscopic procedure "
                  "and is being evaluated by the medical team for a return to play.")
        additional = ("Jonathan Kuminga underwent evaluation, and his return "
                      "will depend on clinical reassessment rather than a fixed date.")
        injuries = {"Minnesota Timberwolves": [
            "Anthony Edwards — Out (" + detail + ")",
            "Zyon Pullin — Out (Short note)",
            "Jaylen Clark — Out (Another short note)",
            "Jonathan Kuminga — Questionable (" + additional + ")"]}
        html = env["_se_injury_table"](["Minnesota Timberwolves"], injuries, "NBA")
        self.assertIn(detail, html)
        self.assertIn(additional, html)
        self.assertNotIn(detail[:59] + "…", html)
        self.assertIn("Show 1 more reported injuries", html)
        self.assertLess(html.index("Anthony Edwards"), html.index("Zyon Pullin"))

    def test_saved_injury_details_remain_html_escaped(self):
        app = Path(__file__).with_name("app.py").read_text()
        import re
        from html import escape
        tree = ast.parse(app)
        relevant = [n for n in tree.body if
                    (isinstance(n, ast.FunctionDef) and n.name in
                     {"_se_injury_table", "_se_injury_priority", "_se_key_absences"}) or
                    (isinstance(n, ast.Assign) and any(
                     isinstance(t, ast.Name) and t.id == "_RECORDED_KEY_PLAYERS" for t in n.targets))]
        env = {"re": re, "esc": escape,
               "_team_key": lambda x: re.sub(r'[^a-z0-9]', '', str(x).casefold())}
        exec(compile(ast.Module(body=relevant, type_ignores=[]), "app.py", "exec"), env)
        html = env["_se_injury_table"](
            ["Team A"], {"Team A": ["Player — Out (Recovery & <testing> continues)"]})
        self.assertIn("Recovery &amp; &lt;testing&gt; continues", html)
        self.assertNotIn("<testing>", html)


class TestMarketsAndImpactOrdering(unittest.TestCase):
    def test_impact_overrides_sort_edwards_above_depth_players(self):
        app = Path(__file__).with_name("app.py").read_text()
        import re
        from html import escape
        tree = ast.parse(app)
        relevant = [n for n in tree.body if
                    (isinstance(n, ast.FunctionDef) and n.name in
                     {"_se_injury_table", "_se_injury_priority", "_se_key_absences"}) or
                    (isinstance(n, ast.Assign) and any(
                     isinstance(t, ast.Name) and t.id == "_RECORDED_KEY_PLAYERS" for t in n.targets))]
        env = {"re": re, "esc": escape,
               "_team_key": lambda x: re.sub(r'[^a-z0-9]', '', str(x).casefold())}
        exec(compile(ast.Module(body=relevant, type_ignores=[]), "app.py", "exec"), env)
        team = "Minnesota Timberwolves"
        notes = ["Zyon Pullin — Out (Quadriceps)", "Jaylen Clark — Out (Hamstring)",
                 "Anthony Edwards — Out (Knee)", "Jonathan Kuminga — Out (Shoulder)"]
        html = env["_se_injury_table"]([team], {team: notes}, "NBA")
        self.assertLess(html.index("Anthony Edwards"), html.index("Zyon Pullin"))
        self.assertIn("Show 1 more reported injuries", html)
        self.assertIn("Anthony Edwards", env["_se_key_absences"]([team], {team: notes}, "NBA"))
        self.assertEqual(env["_se_key_absences"]([team], {team: notes}, "NFL"), "")

    def test_no_unverified_key_absence_claims(self):
        app = Path(__file__).with_name("app.py").read_text()
        import re
        from html import escape
        tree = ast.parse(app)
        relevant = [n for n in tree.body if
                    (isinstance(n, ast.FunctionDef) and n.name in
                     {"_se_injury_table", "_se_injury_priority", "_se_key_absences"}) or
                    (isinstance(n, ast.Assign) and any(
                     isinstance(t, ast.Name) and t.id == "_RECORDED_KEY_PLAYERS" for t in n.targets))]
        env = {"re": re, "esc": escape,
               "_team_key": lambda x: re.sub(r'[^a-z0-9]', '', str(x).casefold())}
        exec(compile(ast.Module(body=relevant, type_ignores=[]), "app.py", "exec"), env)
        self.assertEqual(env["_se_key_absences"](
            ["Minnesota Timberwolves"], {"Minnesota Timberwolves":
            ["Anthony Edwards — Questionable (Knee)"]}, "NBA"), "")

    def test_market_strip_visible_with_recorded_moneyline_and_archived_lines(self):
        app = Path(__file__).with_name("app.py").read_text()
        import re
        from html import escape
        tree = ast.parse(app)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "market_strip_html")
        env = {"re":re, "esc":escape, "TEAM_LOGOS":{}, "PT": None,
               "ODDS_SPORT_KEYS":{"NBA":"basketball_nba"},
               "BOOK_KEYS":{}, "number":lambda x,fallback=None: x,
               "clean_text": lambda x: str(x or ''),
               "matchup_teams":lambda r:["Portland Trail Blazers","Minnesota Timberwolves"],
               "_team_key":lambda x: re.sub(r'[^a-z0-9]', '', str(x).casefold()),
               "saved_market_lines":lambda r: {"spread":"Minnesota Timberwolves -2.5 (-104)",
                                               "total":"Over 227.5 (-110)"},
               "morning_market_snapshots":lambda day:{},
               "recorded_bookmaker":lambda source:"draftkings",
               "saved_model_label":lambda r:"rolling-homewin-v2 (trained)"}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), env)
        row={"date":"2026-11-17","sport":"NBA","selection":"Minnesota Timberwolves",
             "market":"h2h", "matchup":"Portland Trail Blazers @ Minnesota Timberwolves",
             "odds":-130, "source":"Auto: The Odds API / draftkings"}
        html=env["market_strip_html"](row)
        self.assertIn("Moneyline (pick)", html)
        self.assertIn("Minnesota Timberwolves -130", html)
        self.assertIn("Spread (archived)", html)
        self.assertIn("-2.5 (-104)", html)
        self.assertIn("Total O/U (archived)", html)
        self.assertIn("227.5 (-110)", html)
        self.assertIn("Quote timestamps &amp; model source", html)
        self.assertNotIn("rolling-homewin-v2 (trained)</span></div>", html)

    def test_comparison_and_context_equal_height_grid(self):
        app=Path(__file__).with_name("app.py").read_text()
        self.assertIn(".se-v2-expanded .se-v4-main{align-items:stretch}", app)
        self.assertIn(".se-v2-expanded .se-v4-main>.se-insight-card{height:100%", app)
        self.assertNotIn('<details class="se-v4-market-details">', app)
        self.assertIn('{market_strip}', app)


if __name__ == "__main__":
    unittest.main()
