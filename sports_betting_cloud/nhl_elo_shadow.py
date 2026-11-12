"""Prospective NHL Elo shadow predictions. Not consumed by the publisher.

Uses the same fixed Elo settings and logistic fit as the historical experiment.
No Odds API requests. Saves immutable pregame model and cached bookmaker quotes.
"""
import logging
import math
from collections import defaultdict
from datetime import timedelta

from weekly_trainer import dataset, fit, load_training_rows, parse_game, sigmoid

SPORT = 'icehockey_nhl'
VERSION = 'nhl-elo-shadow-v1-k20-home45-noreset'
K, HOME_ADV, SCALE = 20.0, 45.0, 400.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS nhl_elo_shadow_predictions (
 event_id TEXT PRIMARY KEY,
 espn_event_id TEXT NOT NULL,
 home_team TEXT NOT NULL,
 away_team TEXT NOT NULL,
 commence_time TIMESTAMPTZ NOT NULL,
 generated_at TIMESTAMPTZ NOT NULL,
 model_version TEXT NOT NULL,
 elo_home_probability DOUBLE PRECISION NOT NULL CHECK (elo_home_probability > 0 AND elo_home_probability < 1),
 current_home_probability DOUBLE PRECISION,
 market_home_probability DOUBLE PRECISION,
 bookmaker_key TEXT,
 home_american_odds INTEGER,
 away_american_odds INTEGER,
 training_games INTEGER NOT NULL,
 historical_cutoff TIMESTAMPTZ NOT NULL
);
"""


def _implied(odds):
    if not isinstance(odds, (int, float)) or isinstance(odds, bool) or not math.isfinite(odds) or odds == 0:
        return None
    return 100 / (100 + odds) if odds > 0 else -odds / (100 - odds)


def _reference_quote(event):
    """Deterministic same-bookmaker two-way home/away moneyline, if present."""
    home, away = event.get('home_team'), event.get('away_team')
    for book in sorted(event.get('bookmakers') or [], key=lambda b: b.get('key') or ''):
        for market in book.get('markets') or []:
            if market.get('key') != 'h2h':
                continue
            outcomes = market.get('outcomes') or []
            if len(outcomes) != 2:
                continue
            by_name = {o.get('name'): o.get('price') for o in outcomes}
            if set(by_name) != {home, away}:
                continue
            h, a = by_name[home], by_name[away]
            ih, ia = _implied(h), _implied(a)
            if ih is None or ia is None or ih + ia <= 0:
                continue
            return (book.get('key'), int(h), int(a), ih / (ih + ia))
    return (None, None, None, None)


def train_shadow(conn, now):
    """Rebuild pregame-only Elo features from completed archived games.

    The 6-hour cutoff matches prediction_engine's existing conservative
    archived_training_history eligibility rule. Do not include live results.
    """
    rows = load_training_rows(conn, SPORT)
    cutoff = now - timedelta(hours=6)
    filtered = []
    for row in rows:
        payload = row.get('payload')
        if not isinstance(payload, dict):
            continue
        game = parse_game(payload, SPORT)
        if game and game[0] < cutoff:
            filtered.append(row)
    base, _ = dataset(filtered, SPORT)
    if len(base) < 500:
        logging.warning('NHL Elo shadow disabled: only %d eligible historical examples', len(base))
        return None
    games = {}
    for row in filtered:
        game = parse_game(row['payload'], SPORT)
        if game and game[1]:
            games[game[1]] = game
    ratings = defaultdict(lambda: 1500.0)
    pregame = {}
    for dt, eid, home, away, hs, aw in sorted(games.values(), key=lambda g: (g[0], g[1])):
        rh, ra = ratings[home], ratings[away]
        pregame[eid] = (rh - ra) / SCALE
        expected = sigmoid(math.log(10) * (rh + HOME_ADV - ra) / SCALE)
        delta = K * (float(hs > aw) - expected)
        ratings[home] += delta
        ratings[away] -= delta
    data = [(dt, eid, [1.0, pregame[eid]], y) for dt, eid, _, y in base]
    weights = fit(data, learning_rate=.35, penalty=.025, epochs=600)
    return weights, ratings, len(data), cutoff


def save_shadow(conn, model, odds_event, espn_event, current_home_probability, now):
    if model is None:
        return False
    weights, ratings, count, cutoff = model
    cs = ((espn_event.get('competitions') or [{}])[0]).get('competitors') or []
    home = next((x for x in cs if x.get('homeAway') == 'home'), None)
    away = next((x for x in cs if x.get('homeAway') == 'away'), None)
    if not home or not away:
        return False
    ht, at = home.get('team') or {}, away.get('team') or {}
    # Require ESPN IDs, as used in the historical trainer, to avoid silent mismatch.
    if not ht.get('id') or not at.get('id'):
        return False
    home_id, away_id = str(ht['id']), str(at['id'])
    p = sigmoid(weights[0] + weights[1] * (ratings[home_id] - ratings[away_id]) / SCALE)
    quote = _reference_quote(odds_event)
    commence = odds_event['commence_time']
    conn.execute("""INSERT INTO nhl_elo_shadow_predictions
      (event_id, espn_event_id, home_team, away_team, commence_time,
       generated_at, model_version, elo_home_probability, current_home_probability,
       market_home_probability, bookmaker_key, home_american_odds, away_american_odds,
       training_games, historical_cutoff)
      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
      ON CONFLICT (event_id) DO NOTHING""",
      (str(odds_event['id']), str(espn_event['id']), odds_event['home_team'],
       odds_event['away_team'], commence, now, VERSION, p, current_home_probability,
       quote[3], quote[0], quote[1], quote[2], count, cutoff))
    return True
