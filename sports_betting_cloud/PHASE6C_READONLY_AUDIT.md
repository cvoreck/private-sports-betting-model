# Phase 6C — read-only performance validation (no model changes)

Purpose: evaluate the frozen, prospective spread and total forecasts already
captured and graded by the independent Phase 6B GitHub ledger, without changing
its data, production betting models, pick publisher, existing official W–L
record, database, Odds API quota, or scheduling.

## Execution
- Python standard library only.
- GitHub Actions reads one specific commit of branch phase6b-shadow-ledger
  using Git fetch with read permission and Git show. It never checks out
  that branch for writing or pushes any changes.
- The only outputs are aggregate reports/phase6c_audit.json and
  reports/phase6c_audit.md uploaded as a short-lived GitHub artifact.
- It does not read Supabase, execute SQL, call ESPN, or call The Odds API.
  It has no environment secrets configured.
- It checks immutable forecast SHA-256 and grade SHA-256, frozen keys,
  pregame timestamps, result consistency, side-result settlement and
  within-game cross-market result consistency. Invalid input fails closed.
- Default audit runs on the pull request; no merge is necessary to review it.
  The user must separately approve a future merge, workflow edits, model
  promotion or any production change.

## Interpretation
- Scores: mean absolute error (MAE) per sport/market/model version for
  original sportsbook reference, historical baseline, trained baseline, and
  Phase 6A's market-anchored shadow forecast. These are forecast-error units,
  not bet ROI, and NBA points are not comparable with NHL goals.
- Paired game-level improvement = market MAE minus experimental MAE.
  Positive is favorable to the experimental forecast; negative is favorable
  to the market. A 95% percentile bootstrap interval resamples the exact
  same independent games within a sport/market/version.
- Market-implied no-vig conditional probabilities are compared against
  the uncalibrated shadow heuristic with Brier scores, on the same
  first-side outcomes. Pushes are excluded from probability denominators.
  No push probability is estimated.
- No pooled cross-market edge claims: a game may provide both a spread and
  total; the report states the number of unique graded games and pairs.
  It does not treat both as two independent games.
- Reliability bins are descriptive only and especially unstable with samples
  of 10–30 games. Even a favorable point estimate or narrow interval does not
  prove a profitable betting strategy.
- Captures of later same-book pregame quotes are explicitly NOT guaranteed
  closing lines; the audit does not claim CLV.
- No returns, profits, units, theoretical picks, win-rate-based strategies or
  automatic promotion are reported.

## Review gates
1. Confirm aggregate report and hash checks pass without vendor or database.
2. Review unique graded-game counts and paired market-vs-model deltas
   separately for NFL/NBA/NHL spreads and totals.
3. Continue prospective observations to around 100–200 unique graded games
   per sport as an initial statistical checkpoint, then examine uncertainty,
   time stability and market selection/odds validation. This checkpoint alone
   is not permission to promote.
4. Any future change to production, model parameters, W–L record, or
   Phase 6B workflow requires explicit user approval every time.

## Local replay
From repository root, with a locally saved unchanged shadow ledger:

    python sports_betting_cloud/phase6c_readonly_audit.py \
      --ledger /path/to/phase6b_shadow_ledger.json \
      --ledger-commit a_read_only_ledger_revision \
      --report-json reports/phase6c_audit.json \
      --report-md reports/phase6c_audit.md

Only generated files under reports/ are written locally. The input ledger
is never rewritten. Raw ledger data is not in the GitHub artifact.
