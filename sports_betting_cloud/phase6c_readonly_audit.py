#!/usr/bin/env python3
"""Phase 6C — deterministic, read-only audit of the immutable Phase 6B ledger.

Reads a local JSON copy of phase6b_shadow_ledger.json. Standard library only:
no DB, network access, odds vendor, writes to the input or source branch,
production bets, ROI projection, or model promotion.
"""
import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import random
from pathlib import Path

VERSION = "phase6c-readonly-v1"
FORECAST_NAMES = ("market", "baseline", "trained", "anchored")
FORECAST_FIELDS = {
    "market": "market_reference",
    "baseline": "baseline_forecast",
    "trained": "trained_forecast",
    "anchored": "anchored_forecast",
}
SUPPORTED = {"spreads", "totals"}
SEED = 20261118
BOOTSTRAP_REPS = 3000


def sha256_json(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing datetime string")
    t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("Naive datetime not allowed")
    return t.astimezone(dt.timezone.utc)


def mean(values):
    return sum(values) / len(values) if values else None


def num(value):
    return round(value, 6) if value is not None else None


def side_outcome(forecast, grade, side):
    actual = grade["actual_value"]
    point = forecast["selections"][side]["point"]
    if forecast["market"] == "spreads":
        margin = actual + point if side == "home" else -actual + point
    else:
        margin = actual - point if side == "over" else point - actual
    return "P" if abs(margin) < 1e-7 else "W" if margin > 0 else "L"


def validate_ledger(ledger):
    """Fail closed on broken input, grade hashes, and cross-market final conflicts."""
    if ledger.get("schema_version") != 1 or not isinstance(ledger.get("forecasts"), list):
        raise ValueError("Unsupported Phase 6B ledger schema")
    timestamp(ledger["updated_at"])
    seen, game_results, rows = set(), {}, []
    for rec in ledger["forecasts"]:
        fc = rec["forecast"]
        key = "|".join(str(fc[x]) for x in
                       ("sport", "odds_event_id", "market", "model_version"))
        if rec.get("key") != key or key in seen:
            raise ValueError("Invalid or duplicate immutable forecast key")
        seen.add(key)
        if sha256_json(fc) != rec.get("forecast_sha256"):
            raise ValueError("Forecast hash mismatch")
        if fc["market"] not in SUPPORTED or fc["status"] not in ("SHADOW_ONLY", "ABSTAIN"):
            raise ValueError("Unsupported market or status")
        started = timestamp(fc["event_start"])
        captured = timestamp(rec["first_recorded_at"])
        quote = timestamp(fc["source_quote_captured_at"])
        if not quote <= captured < started:
            raise ValueError("Nonpregame frozen forecast timing")
        if not all(finite(fc[n]) for n in FORECAST_FIELDS.values()):
            raise ValueError("Invalid numeric prediction")

        grade = rec.get("grade")
        if grade is None:
            if rec.get("grade_sha256") is not None:
                raise ValueError("Ungraded entry has grade hash")
            rows.append({"record": rec, "game": (fc["sport"], fc["odds_event_id"]),
                         "graded": False, "evaluated": False})
            continue
        if sha256_json(grade) != rec.get("grade_sha256"):
            raise ValueError("Grade hash mismatch")
        if timestamp(grade["graded_at"]) < started:
            raise ValueError("Final was graded before event")
        home, away = grade["home_score"], grade["away_score"]
        if any(not finite(x) or int(x) != x or x < 0 for x in (home, away)):
            raise ValueError("Invalid final score")
        actual = home - away if fc["market"] == "spreads" else home + away
        if not math.isclose(actual, grade["actual_value"], abs_tol=1e-6):
            raise ValueError("Final score/result mismatch")
        for model, field in FORECAST_FIELDS.items():
            expected = abs(actual - fc[field])
            if not math.isclose(expected, grade["absolute_errors"][model],
                                rel_tol=0, abs_tol=1e-4):
                raise ValueError("Stored absolute error differs from frozen forecast")

        game = (fc["sport"], fc["odds_event_id"])
        identity = (grade["final_event_id"], home, away, fc["home_team"],
                    fc["away_team"], fc["event_start"])
        if game in game_results and game_results[game] != identity:
            raise ValueError("Conflicting frozen finals across markets for same game")
        game_results[game] = identity

        if fc["status"] == "SHADOW_ONLY":
            for side in ("home", "away") if fc["market"] == "spreads" else ("over", "under"):
                expected = side_outcome(fc, grade, side)
                if grade["side_results"][side] != expected:
                    raise ValueError("Frozen side-result inconsistency")
                data = fc["selections"][side]
                if not all(finite(data[k]) and 0 < data[k] < 1
                           for k in ("market_no_vig_conditional",
                                     "shadow_conditional_nonpush")):
                    raise ValueError("Missing conditional probabilities")
        rows.append({"record": rec, "game": game, "graded": True,
                     "evaluated": fc["status"] == "SHADOW_ONLY"})
    return rows


def paired_bootstrap(differences, seed, repetitions=BOOTSTRAP_REPS):
    """Paired resampling of whole games within one sport/market/model version."""
    if len(differences) < 2:
        return None
    rng = random.Random(seed)
    n = len(differences)
    samples = sorted(
        sum(differences[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(repetitions)
    )
    return [num(samples[int((repetitions - 1) * .025)]),
            num(samples[int((repetitions - 1) * .975)])]


def reliability_buckets(scored):
    """Descriptive bins, never an assertion of calibrated betting probabilities."""
    groups = collections.defaultdict(list)
    for row in scored:
        group = min(4, int(row["shadow_probability"] * 5))
        groups[group].append(row)
    return [{"lower": i / 5, "upper": (i + 1) / 5, "n": len(vals),
             "mean_shadow_probability": num(mean([v["shadow_probability"] for v in vals])),
             "observed_win_rate": num(mean([v["y"] for v in vals]))}
            for i, vals in sorted(groups.items())]


def aggregate_group(rows, seed):
    evaluated = [r for r in rows if r["evaluated"]]
    pending = sum(not r["graded"] for r in rows)
    abstained = sum(r["record"]["forecast"]["status"] == "ABSTAIN" for r in rows)
    if not evaluated:
        return {"captured": len(rows), "graded": sum(r["graded"] for r in rows),
                "pending": pending, "abstained": abstained, "graded_evaluated_games": 0,
                "mae": {}, "improvement": {}, "probabilities": {},
                "observed_later_pregame_quote_count": 0}
    games = [r["game"] for r in evaluated]
    if len(set(games)) != len(games):
        raise ValueError("Duplicate game/market/model evaluated; no independent samples")
    values = {name: [] for name in FORECAST_NAMES}
    scored = []
    observed_later = 0
    source_lag_hours = []
    for row in evaluated:
        rec = row["record"]
        fc, grade = rec["forecast"], rec["grade"]
        for name in FORECAST_NAMES:
            values[name].append(abs(grade["actual_value"] - fc[FORECAST_FIELDS[name]]))
        source_lag_hours.append(
            (timestamp(rec["first_recorded_at"]) -
             timestamp(fc["source_quote_captured_at"])).total_seconds() / 3600)
        later = rec.get("latest_observed_pregame_quote")
        if later:
            # A later quote is NOT necessarily the closing price.
            if timestamp(fc["source_quote_captured_at"]) < timestamp(later["captured_at"]) < timestamp(fc["event_start"]):
                observed_later += 1
        primary = "home" if fc["market"] == "spreads" else "over"
        result = grade["side_results"][primary]
        if result == "P":
            continue  # no push probability modeled; exclude from Brier denominators
        y = int(result == "W")
        probs = fc["selections"][primary]
        shadow, market = probs["shadow_conditional_nonpush"], probs["market_no_vig_conditional"]
        scored.append({"shadow_probability": shadow, "market_probability": market,
                       "y": y, "shadow_brier": (shadow - y) ** 2,
                       "market_brier": (market - y) ** 2})
    differences = [m - a for m, a in zip(values["market"], values["anchored"])]
    trained_differences = [m - t for m, t in zip(values["market"], values["trained"])]
    brier_diff = [r["market_brier"] - r["shadow_brier"] for r in scored]
    return {
        "captured": len(rows), "graded": sum(r["graded"] for r in rows),
        "pending": pending, "abstained": abstained,
        "graded_evaluated_games": len(evaluated),
        "mae": {name: num(mean(values[name])) for name in FORECAST_NAMES},
        "improvement": {
            "anchored_vs_market_mean_market_minus_anchored": num(mean(differences)),
            "anchored_vs_market_95pct_paired_game_bootstrap": paired_bootstrap(differences, seed),
            "trained_vs_market_mean_market_minus_trained": num(mean(trained_differences)),
            "trained_vs_market_95pct_paired_game_bootstrap": paired_bootstrap(trained_differences, seed + 1),
            "anchored_strictly_lower_error_games": sum(x > 1e-8 for x in differences),
            "anchored_higher_error_games": sum(x < -1e-8 for x in differences),
            "anchored_tied_error_games": sum(abs(x) <= 1e-8 for x in differences),
        },
        "probabilities": {
            "nonpush_games": len(scored),
            "pushes_excluded": len(evaluated) - len(scored),
            "brier_market_no_vig": num(mean([r["market_brier"] for r in scored])),
            "brier_shadow_uncalibrated": num(mean([r["shadow_brier"] for r in scored])),
            "mean_brier_improvement_market_minus_shadow": num(mean(brier_diff)),
            "brier_improvement_95pct_paired_game_bootstrap":
                paired_bootstrap(brier_diff, seed + 2),
            "descriptive_reliability_bins": reliability_buckets(scored),
        },
        "mean_quote_age_hours_at_freeze": num(mean(source_lag_hours)),
        "observed_later_pregame_quote_count": observed_later,
    }


def analyze(ledger, ledger_commit="unidentified", reps=BOOTSTRAP_REPS):
    rows = validate_ledger(ledger)
    sport_games = collections.defaultdict(set)
    graded_games = set()
    paired_markets = collections.defaultdict(set)
    groups = collections.defaultdict(list)
    for row in rows:
        fc = row["record"]["forecast"]
        sport_games[fc["sport"]].add(row["game"])
        if row["evaluated"]:
            graded_games.add(row["game"])
            paired_markets[row["game"]].add(fc["market"])
        groups[(fc["sport"], fc["market"], fc["model_version"])].append(row)
    report = {
        "audit_version": VERSION,
        "source": "Phase 6B immutable shadow ledger; frozen records only",
        "source_branch": "phase6b-shadow-ledger",
        "source_commit": ledger_commit,
        "source_ledger_updated_at": ledger["updated_at"],
        "forecasts_total": len(rows),
        "graded_total": sum(r["graded"] for r in rows),
        "graded_evaluated_forecasts": sum(r["evaluated"] for r in rows),
        "pending_forecasts": sum(not r["graded"] for r in rows),
        "unique_graded_games_total": len(graded_games),
        "games_with_both_graded_spread_and_total": sum(
            x == {"spreads", "totals"} for x in paired_markets.values()),
        "unique_graded_games_by_sport": dict(sorted(
            (sport, sum(g[0] == sport for g in graded_games))
            for sport in sport_games)),
        "markets": {},
        "promotion_recommendation": "DO_NOT_PROMOTE_UNVALIDATED_MODELS",
        "limitations": [
            "MAE is absolute final-score forecast error in units specific to each sport. Never pool NFL, NBA and NHL MAEs.",
            "Paired bootstrap intervals resample games within each sport/market/version. Two markets from the same game are never treated as independent across a pooled result.",
            "Brier scores use the primary side (home or over) and are CONDITIONAL on nonpush. Pushes are excluded; no push probability is modeled.",
            "Phase 6B shadow probabilities are UNCALIBRATED heuristics. Reliability bins are descriptive and can be unstable for small samples.",
            "Books' no-vig estimates and initial market lines are baselines, not a demonstration of actionable value.",
            "A later OBSERVED same-book quote is not a verified closing line or CLV.",
            "No actual realized wagers or tradable odds-strategy returns were estimated; no ROI, units or profit claims.",
            "Grading and quote source data are limited to what the independent Phase 6B ledger froze.",
            "A favorable point estimate is insufficient to establish an edge or merit automatic model promotion.",
        ],
        "resources": {"database_queries": 0, "database_writes": 0,
                      "paid_odds_api_requests": 0, "phase6b_branch_writes": 0,
                      "production_changes": 0, "live_record_changes": 0},
    }
    for i, ((sport, market, version), records) in enumerate(sorted(groups.items())):
        result = aggregate_group(records, SEED + 100 * i)
        report["markets"].setdefault(sport, {}).setdefault(market, {})[version] = result
    return report


def pretty(v, decimals=3):
    return "—" if v is None else f"{v:+.{decimals}f}"


def markdown_report(report):
    out = [
        "# Phase 6C | Read-only prospective performance validation",
        "",
        f"**Shadow ledger commit:** `{report['source_commit']}`  ",
        f"**Ledger updated:** {report['source_ledger_updated_at']}  ",
        f"**Frozen:** {report['forecasts_total']} · **Graded:** {report['graded_total']} · "
        f"**Independent graded games:** {report['unique_graded_games_total']} · "
        f"**Pending:** {report['pending_forecasts']}",
        "",
        "> **No model promotion recommended.** Point estimates and heuristic probabilities "
        "do not establish a betting edge.",
        "",
        "## Paired forecast errors by sport and market",
        "",
        "| Sport | Market | Games | MAE market | MAE anchored | MAE trained | "
        "Market − anchored (95% paired bootstrap CI) |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for sport, markets in sorted(report["markets"].items()):
        for market, models in sorted(markets.items()):
            for version, stat in sorted(models.items()):
                m = stat["mae"]
                diff = stat["improvement"].get("anchored_vs_market_mean_market_minus_anchored")
                ci = stat["improvement"].get("anchored_vs_market_95pct_paired_game_bootstrap")
                ci_str = f"[{pretty(ci[0])}, {pretty(ci[1])}]" if ci else "not estimable"
                n = stat["graded_evaluated_games"]
                out.append(f"| {sport} | {market} | {n} | {m.get('market', '—')} | "
                           f"{m.get('anchored', '—')} | {m.get('trained', '—')} | "
                           f"{pretty(diff)} {ci_str} |")
    out.extend([
        "",
        "**Sign convention:** market MAE minus model MAE; positive means the "
        "model's MAE was *smaller*. Units vary by sport; do not aggregate.",
        "",
        "## Conditional probability checks (nonpush only)",
        "",
        "| Sport | Market | Nonpush | Pushes excluded | Market no-vig Brier | "
        "Shadow Brier | Market − shadow Brier (95% CI) |",
        "|---|---|---:|---:|---:|---:|---|",
    ])
    for sport, markets in sorted(report["markets"].items()):
        for market, models in sorted(markets.items()):
            for version, stat in sorted(models.items()):
                p = stat["probabilities"]
                x = p.get("mean_brier_improvement_market_minus_shadow")
                ci = p.get("brier_improvement_95pct_paired_game_bootstrap")
                ci_str = f"[{pretty(ci[0], 5)}, {pretty(ci[1], 5)}]" if ci else "not estimable"
                out.append(f"| {sport} | {market} | {p.get('nonpush_games', 0)} | "
                           f"{p.get('pushes_excluded', 0)} | {p.get('brier_market_no_vig', '—')} | "
                           f"{p.get('brier_shadow_uncalibrated', '—')} | "
                           f"{pretty(x, 5)} {ci_str} |")
    out.extend([
        "",
        "**Brier convention:** market minus shadow Brier; positive means the "
        "*uncalibrated* shadow heuristic had the smaller error in this sample.",
        "",
        "## Independence, coverage and readiness",
        "",
        f"- Distinct graded games: **{report['unique_graded_games_total']}**; "
        f"games with both graded spread and total: "
        f"**{report['games_with_both_graded_spread_and_total']}**.",
        "- Initial review milestone: roughly 100–200 independently graded games **per sport** "
        "and more prospective stability; this is not a sufficient promotion rule on its own.",
        "- Keep all production models, odds, publishing and official W–L unchanged.",
        "- **No paid Odds API calls, database queries or database writes.** "
        "This report is generated from one frozen GitHub ledger snapshot.",
        "",
        "## Method limitations",
        "",
    ])
    out.extend("- " + x for x in report["limitations"])
    out.append("")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--report-json", type=Path, required=True)
    parser.add_argument("--report-md", type=Path, required=True)
    parser.add_argument("--ledger-commit", default="unidentified")
    args = parser.parse_args()
    ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    report = analyze(ledger, ledger_commit=args.ledger_commit)
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps(report, indent=2, sort_keys=True,
                                           allow_nan=False) + "\n", encoding="utf-8")
    args.report_md.write_text(markdown_report(report), encoding="utf-8")
    print("Phase 6C read-only audit: total=%s graded=%s independent_games=%s "
          "database_queries=0 paid_odds_api_calls=0" %
          (report["forecasts_total"], report["graded_total"],
           report["unique_graded_games_total"]))


if __name__ == "__main__":
    main()
