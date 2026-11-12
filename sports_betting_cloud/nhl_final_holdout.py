"""One-time, read-only NHL final holdout evaluation (last 10% of eligible games).

The candidate was chosen from the earlier 60-90% exploration. Do not use
this holdout repeatedly to tune or select models. No writes or odds API calls.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from weekly_trainer import dataset, fit, load_training_rows, predict, train_candidate
from nhl_model_experiment import SPORT, ELO_K, ELO_HOME_ADV, ELO_SCALE, elo_features, metrics, project

OUTPUT = Path('nhl_final_holdout_report.json')


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    url = os.getenv('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL secret is missing')
    with psycopg.connect(url, row_factory=dict_row) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        rows = load_training_rows(conn, SPORT)
        base, unique_games = dataset(rows, SPORT)
        elo = elo_features(rows)
        conn.rollback()
    missing = [eid for _, eid, _, _ in base if eid not in elo]
    if missing:
        raise RuntimeError(f'Missing pregame Elo for {len(missing)} eligible games')
    data = [(dt, eid, x + [elo[eid]], y) for dt, eid, x, y in base]
    n = len(data)
    if n < 500:
        raise RuntimeError(f'Insufficient eligible NHL games: {n}')
    cut = int(n * .9)
    # Avoid splitting games with identical start timestamps across train/test.
    while cut > 0 and data[cut-1][0] == data[cut][0]:
        cut -= 1
    train, test = data[:cut], data[cut:]
    if len(train) < 400 or len(test) < 30:
        raise RuntimeError('Insufficient training or holdout examples')
    ys = [y for _, _, _, y in test]
    home_rate = (sum(y for _, _, _, y in train) + 1) / (len(train) + 2)
    tuning = {}
    current = train_candidate(project(train, list(range(7))), SPORT, tuning)
    # Freeze the Elo-only configuration from the earlier exploratory experiment.
    elo_weights = fit(project(train, [0, 7]), learning_rate=.35, penalty=.025, epochs=600)
    result = {
        'historical_home_rate': metrics(ys, [home_rate] * len(test)),
        'current_seven_feature': metrics(ys, predict(current, project(test, list(range(7))))),
        'elo_only': metrics(ys, predict(elo_weights, project(test, [0, 7]))),
    }
    baseline = result['historical_home_rate']['logloss']
    for item in result.values():
        item['logloss_improvement_vs_home_rate'] = round(baseline - item['logloss'], 6)
    winner = min(result, key=lambda name: result[name]['logloss'])
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'sport': SPORT,
        'method': 'Single chronological final 10% holdout, models frozen before evaluation',
        'read_only': True, 'promotes_models': False, 'odds_api_calls': False,
        'source_mode': 'canonical' if os.getenv('CANONICAL_HISTORY', '0') == '1' else 'historical_snapshots',
        'unique_completed_games': unique_games, 'eligible_examples': n,
        'training_examples': len(train), 'holdout_examples': len(test),
        'train_end': train[-1][0].isoformat(),
        'holdout_start': test[0][0].isoformat(),
        'holdout_end': test[-1][0].isoformat(),
        'holdout_observed_home_win_rate': round(sum(ys) / len(ys), 5),
        'training_home_win_rate': round(home_rate, 5),
        'current_model_tuning': tuning.get('nhl_tuning'),
        'elo_settings': {'initial_rating': 1500, 'k': ELO_K, 'home_advantage_points': ELO_HOME_ADV,
                         'rating_scale': ELO_SCALE, 'season_reset': False,
                         'feature_indices': [0, 7], 'learning_rate': .35, 'penalty': .025, 'epochs': 600},
        'metrics': result, 'lowest_holdout_logloss': winner,
        'elo_beats_current': result['elo_only']['logloss'] < result['current_seven_feature']['logloss'],
        'elo_beats_home_baseline': result['elo_only']['logloss'] < baseline,
        'limitations': [
            'No historical pregame sportsbook odds; cannot infer betting profitability.',
            'A single holdout is not sufficient for production promotion.',
            'Do not repeatedly optimize against this holdout after viewing the result.',
            'Uses the existing trainer dataset and Elo feature generation; same-time game handling inherits upstream behavior.'
        ]
    }
    OUTPUT.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    logging.info('Final NHL holdout: %d games; best logloss=%s', len(test), winner)
    logging.info('Saved %s', OUTPUT)


if __name__ == '__main__':
    main()
