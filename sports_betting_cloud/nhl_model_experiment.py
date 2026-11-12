"""Read-only NHL feature experiment. Never trains/promotes a production model.

Uses the same ESPN historical game source, eligibility and seven pregame features as
weekly_trainer. Elo is computed chronologically from previously completed games only.
Three expanding-window tests cover 60-90% of eligible examples; final 10% untouched.
No sportsbook API requests, no SQL writes. Exploratory, not a profitability test.
"""
import json
import logging
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from weekly_trainer import (dataset, fit, load_training_rows, logloss,
                            parse_game, predict, train_candidate)

SPORT = 'icehockey_nhl'
OUTPUT = Path('nhl_model_experiment_report.json')
ELO_K = 20.0
ELO_HOME_ADV = 45.0
ELO_SCALE = 400.0
# Fixed a priori: these settings are not selected using the test windows.
VARIANTS = {
    'core_three': [0, 1, 2],
    'seven_feature_no_rest': [0, 1, 2, 3, 4, 5],
    'seven_feature_no_venue': [0, 1, 2, 4, 5, 6],
    'elo_only': [0, 7],
    'core_three_plus_elo': [0, 1, 2, 7],
    'seven_feature_plus_elo': list(range(8)),
}


def elo_features(rows):
    """Map ESPN event id -> pregame Elo difference; deduplicate like dataset()."""
    games = {}
    for row in rows:
        payload = row['payload']
        if isinstance(payload, dict):
            game = parse_game(payload, SPORT)
            if game and game[1]:
                games[game[1]] = game
    ratings = defaultdict(lambda: 1500.0)
    result = {}
    for dt, eid, home, away, hs, aw in sorted(games.values(), key=lambda g: (g[0], g[1])):
        rh, ra = ratings[home], ratings[away]
        result[eid] = (rh - ra) / ELO_SCALE
        p = 1.0 / (1.0 + 10 ** (-(rh + ELO_HOME_ADV - ra) / ELO_SCALE))
        change = ELO_K * (float(hs > aw) - p)
        ratings[home] = rh + change
        ratings[away] = ra - change
    return result


def brier(ys, ps):
    return sum((y - p) ** 2 for y, p in zip(ys, ps)) / len(ys)


def metrics(ys, ps):
    return {'logloss': round(logloss(ys, ps), 6),
            'brier': round(brier(ys, ps), 6),
            'mean_probability': round(sum(ps) / len(ps), 5)}


def project(rows, indices):
    return [(dt, eid, [x[i] for i in indices], y) for dt, eid, x, y in rows]


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if not os.getenv('DATABASE_URL'):
        raise RuntimeError('DATABASE_URL secret is missing')
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        rows = load_training_rows(conn, SPORT)
        base, unique_games = dataset(rows, SPORT)
        pregame_elo = elo_features(rows)
        conn.rollback()

    missing = [eid for _, eid, _, _ in base if eid not in pregame_elo]
    if missing:
        raise RuntimeError(f'Could not align Elo and training examples: {len(missing)} missing event ids')
    data = [(dt, eid, x + [pregame_elo[eid]], y) for dt, eid, x, y in base]
    n = len(data)
    if n < 500:
        raise RuntimeError(f'Insufficient eligible NHL games: {n}')

    windows = []
    for lower, upper in [(0.6, 0.7), (0.7, 0.8), (0.8, 0.9)]:
        start, end = int(n * lower), int(n * upper)
        train, test = data[:start], data[start:end]
        ys = [y for _, _, _, y in test]
        home_rate = (sum(y for _, _, _, y in train) + 1) / (len(train) + 2)
        results = {'historical_home_rate': metrics(ys, [home_rate] * len(test)),
                   'coin_flip': metrics(ys, [0.5] * len(test))}
        # Reproduce current NHL model including its inner chronological tuning.
        original_train = project(train, list(range(7)))
        original_test = project(test, list(range(7)))
        tuning = {}
        original_weights = train_candidate(original_train, SPORT, tuning)
        results['current_seven_feature'] = metrics(ys, predict(original_weights, original_test))
        for name, indices in VARIANTS.items():
            # Fixed regularization/epochs for fair exploratory comparisons.
            weights = fit(project(train, indices), learning_rate=0.35, penalty=0.025, epochs=600)
            results[name] = metrics(ys, predict(weights, project(test, indices)))
        home_ll = results['historical_home_rate']['logloss']
        for name, result in results.items():
            result['logloss_improvement_vs_home_rate'] = round(home_ll - result['logloss'], 6)
        window = {'window': f'{int(lower*100)}-{int(upper*100)}%',
                  'train_examples': len(train), 'test_examples': len(test),
                  'test_start': test[0][0].isoformat(), 'test_end': test[-1][0].isoformat(),
                  'observed_home_win_rate': round(sum(ys)/len(ys), 5),
                  'training_home_win_rate': round(home_rate, 5),
                  'current_model_tuning': tuning.get('nhl_tuning'),
                  'metrics': results}
        windows.append(window)
        logging.info('%s n=%d baseline=%.6f current=%.6f full+elo=%.6f',
                     window['window'], len(test), home_ll,
                     results['current_seven_feature']['logloss'],
                     results['seven_feature_plus_elo']['logloss'])

    names = list(windows[0]['metrics'])
    summary = {}
    for name in names:
        scores = [w['metrics'][name]['logloss'] for w in windows]
        improvements = [w['metrics'][name]['logloss_improvement_vs_home_rate'] for w in windows]
        summary[name] = {'mean_window_logloss': round(sum(scores)/len(scores), 6),
                         'windows_beating_home_rate': sum(v > 0 for v in improvements),
                         'mean_improvement_vs_home_rate': round(sum(improvements)/len(improvements), 6)}
    report = {'generated_at': datetime.now(timezone.utc).isoformat(),
              'sport': SPORT, 'read_only': True, 'promotes_models': False,
              'odds_api_calls': False, 'unique_completed_games': unique_games,
              'eligible_examples': n, 'source_mode': 'canonical' if os.getenv('CANONICAL_HISTORY', '0') == '1' else 'historical_snapshots',
              'method': '3 expanding-window chronological tests (60-70%, 70-80%, 80-90%); final 10% untouched',
              'elo_settings': {'initial_rating': 1500, 'k': ELO_K,
                               'home_advantage_points': ELO_HOME_ADV,
                               'rating_scale': ELO_SCALE,
                               'season_reset': False,
                               'note': 'Elo inputs include only prior completed games. Fixed parameters are exploratory, not tuned.'},
              'variant_feature_indices': VARIANTS,
              'note': 'Research-only feature ablation. No market odds or closing lines; no betting profitability conclusions. Reusing these windows for repeated selection risks overfitting.',
              'summary': summary, 'windows': windows}
    OUTPUT.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    logging.info('NHL experiment report saved: %s', OUTPUT)


if __name__ == '__main__':
    main()
