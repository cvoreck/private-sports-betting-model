# Moneyline audit + ESPN college event matching coverage

## Safety
- This branch is **not** merged into `main`. It does not change model promotion,
  prediction weights, odds limits, or the production moneyline adjustment.
- College basketball scoreboard fetching specifies ESPN Division I men's group 50
  with limit 500. ESPN matching still requires an exact normalized home/away
  team pair, a unique event and start-time agreement. AP Top 25 eligibility is
  verified separately using a dated official AP poll and ESPN team IDs.
- The workflow is manual-only, with no paid Odds API calls.
- The moneyline audit uses **published and settled** moneylines with both raw
  model and same-bookmaker no-vig probabilities saved in their notes.
  Earlier published picks may lack those annotations.
- Rejected past candidate outcomes are **not** available in this sample. Its
  comparison is **not** an unbiased strategy backtest or promotion evidence.
  Do not alter production betting thresholds based only on this report.

## Review and test
1. Review PR changes, especially `espn_event_match.py`,
   `ap_rankings_verifier.py`, and `prediction_engine.py`.
2. Navigate to GitHub Actions and manually run **Moneyline Adjustment and
   College Matching Audit (Manual)** on this branch.
3. Download `moneyline-adjustment-observational-audit` and inspect sample
   size, model calibration and explicit limitations.
4. Before merging, compare the existing daily run's college ESPN match count
   with a non-production dry-run or targeted tests; avoid paid odds refresh.
5. Keep Phase 6B shadow ledger completely separate and unchanged.

## Why not automatically loosen the conservative filter?
The November 17 production log recorded 11 positive raw-EV quotes from just
two trained NBA moneyline events, and all 11 failed the adjusted-EV filter.
The current 35% model/65% market blend with one-percentage-point haircut may
be too strict, but prior published bets do not establish the profitability of
the excluded alternatives.
