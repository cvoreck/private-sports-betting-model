"""Train six sport-specific home-win models on archived ESPN regular-season games.

No Odds API calls. Chronological holdout, pregame-only rolling features, and
promotion only when candidate beats a 50/50 baseline and incumbent on the
same holdout. The daily prediction engine must load promoted models separately.
"""
import json
import logging
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

SPORTS = (
    'basketball_nba',
    'icehockey_nhl',
    'americanfootball_nfl',
    'baseball_mlb',
    'americanfootball_ncaaf',
    'basketball_ncaab',
)
MIN_TRAIN, MIN_TEST, MIN_PRIOR = 80, 25, 5
# Margin normalization reflects typical scoring differences by sport.
MARGIN_SCALE = {
    'basketball_nba': 15.0,
    'icehockey_nhl': 2.5,
    'americanfootball_nfl': 14.0,
    'baseball_mlb': 3.0,
    'americanfootball_ncaaf': 17.0,
    'basketball_ncaab': 12.0,
}
NHL_FEATURES = [
    'intercept', 'win_rate_diff_last10', 'margin_diff_last10_scaled',
    'home_away_win_rate_diff_last10', 'goals_for_diff_last10_scaled',
    'goals_against_diff_last10_scaled', 'rest_days_diff_scaled',
]
LEGACY_FEATURES = ['intercept', 'win_rate_diff_last10', 'margin_diff_last10_scaled']


def append_history(hist, dt, home, away, home_score, away_score, sport):
    """Store only already-final games, with enough context for NHL features."""
    if sport == 'icehockey_nhl':
        hist[home].append((int(home_score > away_score), home_score-away_score,
                           home_score, away_score, True, dt))
        hist[away].append((int(away_score > home_score), away_score-home_score,
                           away_score, home_score, False, dt))
    else:
        hist[home].append((int(home_score > away_score), home_score-away_score))
        hist[away].append((int(away_score > home_score), away_score-home_score))


def nhl_features(home, away, hist, asof):
    """Seven strictly pregame features. All data comes from completed games.

    Home/away splits shrink toward overall win rate to avoid sparse samples.
    Rest uses most recent completed game; cap at 7 days. No future results.
    """
    h, a = hist.get(home, [])[-10:], hist.get(away, [])[-10:]
    if len(h) < MIN_PRIOR or len(a) < MIN_PRIOR:
        return None
    if any(len(r) < 6 for r in h + a):
        return None
    hw, aw = sum(r[0] for r in h)/len(h), sum(r[0] for r in a)/len(a)
    hm, am = sum(r[1] for r in h)/len(h), sum(r[1] for r in a)/len(a)
    def venue_win(rows, is_home, overall):
        subset = [r[0] for r in rows if r[4] == is_home]
        return (sum(subset) + 3 * overall)/(len(subset) + 3)
    h_venue = venue_win(h, True, hw)
    a_venue = venue_win(a, False, aw)
    goals_for = (sum(r[2] for r in h)/len(h) - sum(r[2] for r in a)/len(a))/2.5
    goals_against = (sum(r[3] for r in h)/len(h) - sum(r[3] for r in a)/len(a))/2.5
    def rest(rows):
        last = max(r[5] for r in rows)
        return max(0.0, min(7.0, (asof-last).total_seconds()/86400.0))
    return [1.0, hw-aw, max(-3., min(3., (hm-am)/2.5)),
            h_venue-a_venue, max(-3., min(3., goals_for)),
            max(-3., min(3., goals_against)), (rest(h)-rest(a))/7.0]


SCHEMA = '''
CREATE TABLE IF NOT EXISTS sports_edge_training_runs (
 id BIGSERIAL PRIMARY KEY, trained_at TIMESTAMPTZ NOT NULL DEFAULT now(), sport TEXT NOT NULL,
 status TEXT NOT NULL, train_games INT NOT NULL, test_games INT NOT NULL,
 candidate_logloss DOUBLE PRECISION, baseline_logloss DOUBLE PRECISION,
 incumbent_logloss DOUBLE PRECISION, details JSONB NOT NULL DEFAULT '{}'::jsonb);
CREATE TABLE IF NOT EXISTS sports_edge_trained_models (
 sport TEXT PRIMARY KEY, version TEXT NOT NULL, trained_at TIMESTAMPTZ NOT NULL,
 coefficients JSONB NOT NULL, test_logloss DOUBLE PRECISION NOT NULL,
 train_games INT NOT NULL, test_games INT NOT NULL);
'''


def norm(s):
    return re.sub('[^a-z0-9]+', '', str(s or '').lower())


def sigmoid(x):
    return 1 / (1 + math.exp(-max(-30, min(30, x))))


def clip(p):
    return min(.999, max(.001, p))


def logloss(ys, ps):
    if not ys:
        raise ValueError('Cannot evaluate an empty test set')
    return sum(-y * math.log(clip(p)) - (1-y) * math.log(clip(1-p))
               for y, p in zip(ys, ps)) / len(ys)


def features(home, away, hist, sport):
    h, a = hist[home][-10:], hist[away][-10:]
    if len(h) < MIN_PRIOR or len(a) < MIN_PRIOR:
        return None
    wr = lambda rows: sum(x[0] for x in rows) / len(rows)
    margin = lambda rows: sum(x[1] for x in rows) / len(rows)
    scale = MARGIN_SCALE[sport]
    return [1.0, wr(h) - wr(a), max(-3.0, min(3.0, (margin(h)-margin(a))/scale))]


def parse_game(e, sport):
    try:
        season_type = (e.get('season') or {}).get('type')
        if str(season_type) != '2':
            return None  # Regular season only; exclude preseason and playoffs.
        if not ((e.get('status') or {}).get('type') or {}).get('completed'):
            return None
        cs = (e.get('competitions') or [{}])[0].get('competitors') or []
        h = next(x for x in cs if x.get('homeAway') == 'home')
        a = next(x for x in cs if x.get('homeAway') == 'away')
        hs, aa = float(h['score']), float(a['score'])
        if not math.isfinite(hs) or not math.isfinite(aa) or hs == aa:
            return None  # No draw-class handling in this binary model.
        ht, at = h.get('team') or {}, a.get('team') or {}
        # ESPN team IDs are more stable than display names across seasons.
        hn = str(ht.get('id') or norm(ht.get('displayName')))
        an = str(at.get('id') or norm(at.get('displayName')))
        dt = datetime.fromisoformat(e['date'].replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if not hn or not an or hn == an or dt > datetime.now(timezone.utc):
            return None
        return dt, str(e.get('id') or ''), hn, an, hs, aa
    except (ValueError, TypeError, KeyError, StopIteration, IndexError, AttributeError):
        return None


def dataset(rows, sport):
    games = {}
    for r in rows:
        e = r['payload']
        if not isinstance(e, dict):
            continue
        g = parse_game(e, sport)
        if g and g[1]:
            games[g[1]] = g
    hist, examples = defaultdict(list), []
    for dt, eid, h, a, hs, aa in sorted(games.values(), key=lambda g: (g[0], g[1])):
        x = nhl_features(h, a, hist, dt) if sport == 'icehockey_nhl' else features(h, a, hist, sport)
        if x is not None:
            examples.append((dt, eid, x, int(hs > aa)))
        # Only update history after constructing the pregame feature row.
        append_history(hist, dt, h, a, hs, aa, sport)
    return examples, len(games)


def fit(data, learning_rate=.35, penalty=.025, epochs=900):
    w = [0.0] * len(data[0][2])
    for _ in range(epochs):
        grad = [0.0] * len(w)
        for _, _, x, y in data:
            err = sigmoid(sum(v*c for v, c in zip(w, x))) - y
            for j in range(len(w)):
                grad[j] += err*x[j]
        for j in range(len(w)):
            w[j] -= learning_rate * (grad[j]/len(data) + (0 if j == 0 else penalty*w[j]))
    return w


def predict(w, data):
    return [sigmoid(sum(v*c for v, c in zip(w, x))) for _, _, x, _ in data]


def evaluate(w, data):
    return logloss([z[3] for z in data], predict(w, data))



def train_candidate(train, sport, details):
    """Tune NHL on an inner chronological validation window, never the final test.

    NHL uses seven features shared with prediction_engine.py.
    All other sports retain their original three-feature algorithm.
    """
    if sport != 'icehockey_nhl':
        return fit(train)
    split = int(len(train) * .8)
    inner_train, validation = train[:split], train[split:]
    if len(inner_train) < MIN_TRAIN or len(validation) < MIN_TEST:
        details['nhl_tuning'] = 'insufficient_inner_validation; default_fit'
        return fit(train)
    configurations = [
        (.35, .025, 900),  # Original model, included for fair selection
        (.35, .005, 900),
        (.35, .075, 900),
        (.25, .001, 900),
        (.25, .150, 900),
        (.20, .025, 450),
    ]
    results = []
    for lr, penalty, epochs in configurations:
        w = fit(inner_train, learning_rate=lr, penalty=penalty, epochs=epochs)
        score = evaluate(w, validation)
        results.append((score, lr, penalty, epochs))
    # Stable tie-breaking favors the original configuration when scores match.
    best = min(enumerate(results), key=lambda pair: (pair[1][0], pair[0]))[1]
    score, lr, penalty, epochs = best
    details['nhl_tuning'] = {
        'validation_games': len(validation),
        'validation_first_game': validation[0][0].isoformat(),
        'validation_last_game': validation[-1][0].isoformat(),
        'best_validation_logloss': score,
        'chosen_learning_rate': lr,
        'chosen_penalty': penalty,
        'chosen_epochs': epochs,
        'configurations': [
            {'validation_logloss': v, 'learning_rate': a, 'penalty': b, 'epochs': c}
            for v, a, b, c in results
        ],
    }
    logging.info('NHL validation tuning: best logloss=%.6f lr=%s penalty=%s epochs=%d',
                 score, lr, penalty, epochs)
    return fit(train, learning_rate=lr, penalty=penalty, epochs=epochs)


def run(conn, sport):
    rows = conn.execute('''SELECT event_id,payload FROM historical_snapshots
      WHERE source='espn' AND sport=%s AND category='game'
      ORDER BY collected_at DESC LIMIT 30000''', (sport,)).fetchall()
    data, game_count = dataset(rows, sport)
    n = len(data)
    cut = int(n*.8)
    train, test = data[:cut], data[cut:]
    details = {
        'archived_unique_final_games': game_count,
        'eligible_pregame_examples': n,
        'first_test_game': test[0][0].isoformat() if test else None,
        'last_test_game': test[-1][0].isoformat() if test else None,
        'margin_scale': MARGIN_SCALE[sport],
        'note': 'Team-level home-win model. NHL seven-feature candidate uses pregame context and inner chronological validation; other sports unchanged.',
    }
    status, cand, base, inc = 'insufficient_data', None, None, None
    if len(train) >= MIN_TRAIN and len(test) >= MIN_TEST:
        weights = train_candidate(train, sport, details)
        cand = evaluate(weights, test)
        ys = [z[3] for z in test]
        base = logloss(ys, [.5]*len(ys))
        incumbent = conn.execute(
            'SELECT coefficients FROM sports_edge_trained_models WHERE sport=%s',
            (sport,)
        ).fetchone()
        if incumbent:
            old = (incumbent['coefficients'] or {}).get('weights')
            # An incumbent built using a different margin scale is not comparable.
            # Preserve it until it can be evaluated on matching features.
            metadata = incumbent['coefficients'] or {}
            expected_features = NHL_FEATURES if sport == 'icehockey_nhl' else LEGACY_FEATURES
            compatible = (metadata.get('margin_scale') == MARGIN_SCALE[sport]
                          and metadata.get('features') == expected_features
                          and isinstance(old, list) and len(old) == len(expected_features)
                          and all(isinstance(v, (int, float)) and math.isfinite(v) for v in old))
            if compatible:
                inc = evaluate(old, test)
            else:
                details['incumbent_incompatible'] = True
                logging.warning('%s: existing model has incompatible or missing feature metadata; '
                                'will not compare its log loss on different features', sport)
        details['required_logloss_improvement'] = .005
        details['baseline_improvement'] = base - cand
        details['incumbent_improvement'] = inc - cand if inc is not None else None
        if cand < base - .005 and (inc is None or cand < inc - .005):
            version = ('nhl-context-v3-' if sport == 'icehockey_nhl' else 'rolling-homewin-v2-') + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            conn.execute('''INSERT INTO sports_edge_trained_models
             (sport,version,trained_at,coefficients,test_logloss,train_games,test_games)
             VALUES (%s,%s,now(),%s,%s,%s,%s)
             ON CONFLICT(sport) DO UPDATE SET version=EXCLUDED.version,
             trained_at=EXCLUDED.trained_at,coefficients=EXCLUDED.coefficients,
             test_logloss=EXCLUDED.test_logloss,train_games=EXCLUDED.train_games,
             test_games=EXCLUDED.test_games''',
             (sport, version, json.dumps({
                 'weights': weights,
                 'features': NHL_FEATURES if sport == 'icehockey_nhl' else LEGACY_FEATURES,
                 'margin_scale': MARGIN_SCALE[sport],
             }), cand, len(train), len(test)))
            status = 'promoted'
        else:
            status = ('rejected_below_baseline_threshold' if cand >= base - .005
                      else 'rejected_below_incumbent_threshold')
            logging.info('%s: promotion rejected; candidate improvement over baseline=%.6f, required>0.005; incumbent=%s',
                         sport, base-cand, inc)
        details['candidate_weights'] = weights
    conn.execute('''INSERT INTO sports_edge_training_runs
      (sport,status,train_games,test_games,candidate_logloss,baseline_logloss,incumbent_logloss,details)
      VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
      (sport, status, len(train), len(test), cand, base, inc, json.dumps(details)))
    logging.info('%s: %s | final games=%d examples=%d train=%d test=%d candidate=%s baseline=%s incumbent=%s',
                 sport, status, game_count, n, len(train), len(test), cand, base, inc)


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if not os.environ.get('DATABASE_URL'):
        raise RuntimeError('DATABASE_URL secret is missing')
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row) as conn:
        conn.execute(SCHEMA)
        for sport in SPORTS:
            run(conn, sport)


if __name__ == '__main__':
    main()
