"""Read-only audit comparing market-aware moneyline adjustment variants.

Historical published picks are selection-biased; rejected candidate outcomes cannot
be reconstructed from overwritten model_predictions. NEVER promote thresholds using
this audit alone. Zero paid odds requests, no table creation, no database writes.
"""
import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PATTERNS = {
    "raw_model": re.compile(r"Raw trained-model probability\\s+([0-9]*\\.?[0-9]+)", re.I),
    "fair_market": re.compile(r"same-bookmaker no-vig probability\\s+([0-9]*\\.?[0-9]+)", re.I),
}
VARIANTS = (
    ("current_35pct_1pp", 0.35, 0.01),
    ("35pct_no_haircut", 0.35, 0.00),
    ("50pct_1pp", 0.50, 0.01),
    ("65pct_1pp", 0.65, 0.01),
    ("80pct_1pp", 0.80, 0.01),
    ("raw_model_no_haircut", 1.00, 0.00),
)


def break_even(odds):
    if isinstance(odds, bool) or not isinstance(odds, (int, float)) or not math.isfinite(odds):
        raise ValueError("invalid odds")
    if odds >= 100:
        return 100.0 / (odds + 100.0)
    if odds <= -100:
        return -odds / (100.0 - odds)
    raise ValueError("invalid American odds")


def adjust_probability(raw, fair, weight, haircut):
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (raw, fair, weight, haircut)):
        raise ValueError("non-finite probability input")
    if not (0 < raw < 1 and 0 < fair < 1 and 0 <= weight <= 1 and 0 <= haircut < 1):
        raise ValueError("out-of-range probability input")
    return min(0.999, max(0.001, fair + weight * (raw - fair) - haircut))


def extract_inputs(notes):
    text = str(notes or "")
    out = {}
    for key, pattern in PATTERNS.items():
        match = pattern.search(text)
        if match:
            p = float(match.group(1))
            if 0 < p < 1 and math.isfinite(p):
                out[key] = p
    return out if len(out) == 2 else None


def summarize(data):
    rows = [r for r in data if r["result"] in ("Win", "Loss")]
    if not rows:
        return {"graded": 0, "wins": 0, "roi_on_selected": None, "brier": None, "log_loss": None}
    n = len(rows)
    risked = sum(r["stake"] for r in rows)
    units = sum(r["stake"] * (
        (r["odds"] / 100 if r["odds"] > 0 else 100 / abs(r["odds"]))
        if r["result"] == "Win" else -1
    ) for r in rows)
    brier = sum((r["prob"] - int(r["result"] == "Win")) ** 2 for r in rows) / n
    loss = sum(-math.log(min(1 - 1e-9, max(1e-9, r["prob"] if r["result"] == "Win" else 1 - r["prob"]))) for r in rows) / n
    return {"graded": n, "wins": sum(r["result"] == "Win" for r in rows),
            "roi_on_selected": round(units / risked, 5) if risked else None,
            "brier": round(brier, 6), "log_loss": round(loss, 6)}


def evaluate(rows):
    """Only previously published selections; no hypothetical rejected-game results."""
    usable = []
    for row in rows:
        try:
            if row.get("market") != "h2h" or row.get("result") not in ("Win", "Loss"):
                continue
            extracted = extract_inputs(row.get("notes"))
            if not extracted:
                continue
            odds = int(row["odds"])
            implied = break_even(odds)
            stake = float(row["stake"])
            if not math.isfinite(stake) or stake <= 0:
                continue
            usable.append(dict(row, **extracted, implied=implied, odds=odds, stake=stake))
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    by_variant = []
    for name, weight, haircut in VARIANTS:
        all_rows = []
        selected = []
        for row in usable:
            p = adjust_probability(row["raw_model"], row["fair_market"], weight, haircut)
            record = dict(result=row["result"], odds=row["odds"], stake=row["stake"], prob=p)
            all_rows.append(record)
            if p > row["implied"]:
                selected.append(record)
        by_variant.append({"name": name, "weight": weight, "haircut_pp": haircut * 100,
                           "would_pass_on_historically_published_picks": len(selected),
                           "all_observed": summarize(all_rows),
                           "passing_observed_only": summarize(selected)})
    return {
        "status": "descriptive_only" if usable else "insufficient_annotated_picks",
        "graded_annotated_moneyline_picks": len(usable),
        "total_picks_examined": len(rows),
        "variants": by_variant,
        "limitations": [
            "Selected historical picks only; outcomes of rejected candidates are unavailable.",
            "Comparisons on previously published selections are selection-biased and cannot estimate all-bet strategy ROI.",
            "Repeated same-event sportsbook quotes are not independent betting opportunities.",
            "Market-fair probability and raw-model probability are extracted from immutable published notes when available.",
            "This is not chronological out-of-sample validation of a newly selected adjustment.",
            "No adjustment is promoted to production by this audit.",
        ],
    }


def main():
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row,
                         connect_timeout=15, options="-c statement_timeout=30000") as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            rows = conn.execute("""
                SELECT date, sport, matchup, market, selection, odds, stake, result, notes
                FROM picks WHERE kind='Hypothetical' AND source LIKE 'Auto:%%'
                ORDER BY date, id
            """).fetchall()
    report = evaluate([dict(r) for r in rows])
    report.update({"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                   "read_only": True, "database_writes": 0,
                   "paid_api_requests": 0, "model_promotions": 0})
    dest = Path(os.getenv("MONEYLINE_AUDIT_OUTPUT", "reports/moneyline_adjustment_audit.json"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"status": report["status"],
                      "graded_annotated_moneyline_picks": report["graded_annotated_moneyline_picks"],
                      "variants": [{"name": v["name"], "would_pass": v["would_pass_on_historically_published_picks"]}
                                   for v in report["variants"]]}, indent=2))
    print("Saved read-only audit:", dest)


if __name__ == "__main__":
    main()
