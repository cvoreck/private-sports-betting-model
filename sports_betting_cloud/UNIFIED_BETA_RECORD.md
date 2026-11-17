# Unified beta Top 5: one W-L-P record

This proposed change uses **one shared testing-period W-L-P record** for all
published, hypothetical daily picks. There is no second research/watchlist
ledger. Do not reset, delete, or rewrite the historical record.

- **Tier 1 (strict):** trained moneyline with positive conservatively adjusted EV.
- **Tier 2 (provisional beta):** trained moneyline with positive raw modeled EV
  but non-positive conservative adjusted EV. This is NOT demonstrated betting
  value; it is a research selection and marked `PROVISIONAL BETA` on the card.
- All selections require verified upcoming regular-season games, official
  AP Top 25 verification for college sports, real cached odds, valid same-book
  two-way no-vig prices, promoted trained models, a capped price from -200 to
  +200, and positive raw modeled EV.
- Each distinct game can appear only once. Strict picks rank before provisional.
  Published stake is a flat hypothetical 1 unit. At most five are chosen; never
  manufacture missing selections.
- **The published probability is always the conservative market-aware adjusted
  estimate**. Provisional entries can show a negative adjusted edge on the
  dashboard even though they passed the raw-model screen. Raw probability and
  both EV estimates are documented in the pick notes.
- Both tiers save to `picks` with `kind='Hypothetical'`,
  `source='Auto: ...'`, `rank` and `stake`. Existing settlement and
  analytics count both tiers in the same record, including Wins/Losses/Pushes.
  No counterfactual wins or fake picks are inserted.
- No spreads/totals promotion. Phase 6B remains independent. No additional
  paid Odds API requests, no change in the database schema.
- A future record restart must be deliberate, with a snapshot or an archived
  copy of the beta record before any change; this PR **does not** reset anything.

Tests run offline in GitHub Actions before review. This is for research and
tracking, not a validated wagering system.
