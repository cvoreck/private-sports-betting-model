# Phase 6B: read-only ESPN final fallback

The scheduled Phase 6B ledger has been successfully capturing immutable market
forecasts, but as of Nov 17, 2026, **168 forecasts are pending**, with zero
canonical game rows in grading windows. The completed-game canonical table is
populated by a **manual** compact-history migration; the daily historical
collector separately archives ESPN `game` snapshots.

This change keeps the canonical table as the first source. If any recorded
forecast is due and cannot be uniquely matched, it reads the already-stored
ESPN snapshots for tightly bounded scoreboarding dates and uses only:
- ESPN explicitly completed regular-season games (season type 2)
- complete home and away names and team IDs, nonmissing numeric scores
- game times within the original ±18-hour grading windows
- games older than the existing 24-hour results lag
- one latest completed observation per ESPN event ID
- strictly unique ESPN event matches by ordered team names and game start

Snapshots never change or overwrite frozen forecasts. Locked grades retain
their SHA256 hashes and are checked on every future run. Score conflicts with
existing canonical rows fail closed. The script performs no paid vendor
requests, ESPN network requests, database writes, model promotions or changes
to production moneylines / totals / spreads.

Audit the **next scheduled** Phase 6B run's `phase6b_shadow_summary.json`:
`grading_database_read_counts`, `records_graded`, and any `FINAL_NOT_FOUND`
reasons. A first nonzero grade means the pipeline is operating, not that the
market-anchored model is better than sportsbooks. Compare independent games,
forecast MAE/Brier, and market baselines only after sufficient graded games.

Do not manually recreate or overwrite prior grades. Do not merge this patch
until the offline regression tests and code review pass.
