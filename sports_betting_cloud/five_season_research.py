"""Read-only five-season coverage and recency-weight research on canonical games.

No network API calls, DDL, inserts, or model promotion. Requires DATABASE_URL.
Run: python sports_betting_cloud/five_season_research.py
"""
import json
import logging
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from weekly_trainer import SPORTS, dataset, fit, load_training_rows, logloss, predict, sigmoid

# Season boundaries: NFL/CFB/MLB start in calendar year; NBA/NHL/CBB
# begin in autumn and finish in the following calendar year.
SEASON_START = 2022
SEASON_END = 2026
SPORT_SEASON_MONTH = {
    'basketball_nba': 10, 'icehockey_nhl': 10,
    'americanfootball_nfl': 8, 'baseball_mlb': 3,
    'americanfootball_ncaaf': 8, 'basketball_ncaab': 10,
}


def season_year(sport, dt):
    return dt.year if dt.month >= SPORT_SEASON_MONTH[sport] else dt.year - 1


def weighted_fit(train, half_life_days, learning_rate=.35, penalty=.025, epochs=900):
    """Weighted logistic regression; weights computed from training data only."""
    if not train:
        raise ValueError('Empty training window')
    end = train[-1][0]
    weights = [2 ** (-max(0, (end - r[0]).total_seconds() / 86400) / half_life_days)
               for r in train]
    total = sum(weights)
    w = [0.0] * len(train[0][2])
    for _ in range(epochs):
        grad = [0.0] * len(w)
        for (_, _, x, y), sample_weight in zip(train, weights):
            err = (sigmoid(sum(a * b for a, b in zip(w, x))) - y) * sample_weight
            for j, xj in enumerate(x):
                grad[j] += err * xj
        for j in range(len(w)):
            w[j] -= learning_rate * (grad[j] / total + (0 if j == 0 else penalty * w[j]))
    return w


def brier(ys, ps):
    return sum((y - p) ** 2 for y, p in zip(ys, ps)) / len(ys)


def evaluate(data, weights):
    ys = [r[3] for r in data]
    ps = predict(weights, data)
    return {'log_loss': round(logloss(ys, ps), 6), 'brier': round(brier(ys, ps), 6)}


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if not os.getenv('DATABASE_URL'):
        raise RuntimeError('DATABASE_URL is required')
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'read_only': True, 'odds_api_calls': 0, 'model_promotions': 0,
        'retention_window': '2022-2026 season starts, inclusive',
        'note': 'Research only; no production changes. Chronological expanding windows.',
        'sports': {},
    }
    # Explicit read-only transaction prevents accidental database writes.
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row, connect_timeout=20) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        for sport in SPORTS:
            logging.info('Evaluating %s using canonical history', sport)
            # Force the already-supported compact reader, independent of workflow defaults.
            old = os.environ.get('CANONICAL_HISTORY')
            os.environ['CANONICAL_HISTORY'] = '1'
            try:
                rows = load_training_rows(conn, sport)
            finally:
                if old is None:
                    os.environ.pop('CANONICAL_HISTORY', None)
                else:
                    os.environ['CANONICAL_HISTORY'] = old
            all_data, unique = dataset(rows, sport)
            # All games in canonical reader are finals; derive the season from game date.
            # We filter examples only after constructing historical features, preserving
            # pregame context from preceding seasons when applicable.
            data = [r for r in all_data if SEASON_START <= season_year(sport, r[0]) <= SEASON_END]
            coverage = conn.execute('''SELECT COUNT(*) AS games, MIN(played_at) AS first_game,
                 MAX(played_at) AS last_game FROM canonical_completed_games
                 WHERE sport=%s AND season_type=2''', (sport,)).fetchone()
            season_counts = Counter(season_year(sport, r[0]) for r in data)
            item = {
                'canonical_regular_games_all_years': coverage['games'],
                'first_canonical_game': coverage['first_game'].isoformat() if coverage['first_game'] else None,
                'last_canonical_game': coverage['last_game'].isoformat() if coverage['last_game'] else None,
                'unique_final_games_parsed': unique,
                'eligible_examples_five_seasons': len(data),
                'eligible_examples_by_season': {str(y): season_counts[y] for y in range(SEASON_START, SEASON_END + 1)},
                'windows': [],
            }
            n = len(data)
            if n < 200:
                item['warning'] = 'Insufficient eligible games for three research windows'
                report['sports'][sport] = item
                continue
            for start in (.6, .7, .8):
                cut = int(n * start)
                end = int(n * (start + .1))
                train, test = data[:cut], data[cut:end]
                if not train or not test:
                    continue
                variants = {
                    'unweighted': fit(train),
                    'half_life_365_days': weighted_fit(train, 365),
                    'half_life_730_days': weighted_fit(train, 730),
                }
                item['windows'].append({
                    'train_games': len(train), 'test_games': len(test),
                    'test_start': test[0][0].isoformat(),
                    'test_end': test[-1][0].isoformat(),
                    'results': {name: evaluate(test, w) for name, w in variants.items()},
                })
            if item['windows']:
                item['mean_log_loss'] = {
                    name: round(sum(w['results'][name]['log_loss'] for w in item['windows']) / len(item['windows']), 6)
                    for name in ('unweighted', 'half_life_365_days', 'half_life_730_days')
                }
            report['sports'][sport] = item
        conn.rollback()
    path = Path('five_season_research_report.json')
    path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    logging.info('Saved %s; database unchanged', path)


if __name__ == '__main__':
    main()
