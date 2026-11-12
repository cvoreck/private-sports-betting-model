"""Read-only, walk-forward NHL model diagnostic using existing weekly_trainer features.

No schema creation, model promotion, sportsbook API, or writes. Compares the
seven-feature NHL model to constant 50/50 and training-period home-win baseline.
The final 10% of examples are reserved as an untouched evaluation window.
"""
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from weekly_trainer import dataset, fit, load_training_rows, logloss, predict, train_candidate

SPORT = 'icehockey_nhl'
OUTPUT = Path(os.getenv('NHL_DIAGNOSTIC_OUTPUT', 'nhl_rolling_diagnostic_report.json'))


def brier(y, p):
    return sum((a - b) ** 2 for a, b in zip(y, p)) / len(y)


def calibration(y, p, bins=10):
    buckets = []
    for i in range(bins):
        indices = [j for j, value in enumerate(p) if min(bins - 1, int(value * bins)) == i]
        if indices:
            buckets.append({'range': [round(i / bins, 2), round((i + 1) / bins, 2)],
                            'count': len(indices),
                            'predicted': round(sum(p[j] for j in indices) / len(indices), 5),
                            'observed': round(sum(y[j] for j in indices) / len(indices), 5)})
    ece = sum(b['count'] * abs(b['predicted'] - b['observed']) for b in buckets) / len(y)
    return {'ece_approx': round(ece, 5), 'buckets': buckets}


def assess(train, test, label):
    details = {}
    weights = train_candidate(train, SPORT, details)
    ys = [int(row[3]) for row in test]
    model = predict(weights, test)
    home_rate = (sum(int(row[3]) for row in train) + 1) / (len(train) + 2)
    baselines = {'coin_flip': [0.5] * len(test), 'historical_home_rate': [home_rate] * len(test)}
    scores = {}
    for name, probabilities in [('seven_feature_nhl', model), *baselines.items()]:
        scores[name] = {'logloss': round(logloss(ys, probabilities), 6),
                        'brier': round(brier(ys, probabilities), 6),
                        'mean_predicted_home_win': round(sum(probabilities) / len(probabilities), 5)}
    return {'window': label, 'train_examples': len(train), 'test_examples': len(test),
            'test_start': test[0][0].isoformat(), 'test_end': test[-1][0].isoformat(),
            'observed_home_win_rate': round(sum(ys) / len(ys), 5),
            'training_home_win_rate_smoothed': round(home_rate, 5),
            'metrics': scores,
            'model_minus_home_baseline_logloss': round(scores['seven_feature_nhl']['logloss'] - scores['historical_home_rate']['logloss'], 6),
            'model_minus_coin_flip_logloss': round(scores['seven_feature_nhl']['logloss'] - scores['coin_flip']['logloss'], 6),
            'calibration': calibration(ys, model),
            'tuning': details.get('nhl_tuning')}


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if not os.getenv('DATABASE_URL'):
        raise RuntimeError('DATABASE_URL is required')
    # Session read-only mode prevents accidental mutations, even if a helper changes.
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        rows = load_training_rows(conn, SPORT)
        examples, unique_games = dataset(rows, SPORT)
        conn.rollback()
    n = len(examples)
    if n < 500:
        raise RuntimeError(f'Only {n} NHL examples; at least 500 needed for walk-forward analysis')
    # Chronological expanding windows: 60-70, 70-80, 80-90%; last 10% untouched.
    bounds = [(0.60, 0.70), (0.70, 0.80), (0.80, 0.90)]
    results = []
    for lower, upper in bounds:
        start, end = int(n * lower), int(n * upper)
        result = assess(examples[:start], examples[start:end], f'{int(lower*100)}-{int(upper*100)}%')
        results.append(result)
        logging.info('%s n=%d model=%.6f home_baseline=%.6f coin_flip=%.6f',
                     result['window'], result['test_examples'],
                     result['metrics']['seven_feature_nhl']['logloss'],
                     result['metrics']['historical_home_rate']['logloss'],
                     result['metrics']['coin_flip']['logloss'])
    wins_home = sum(x['model_minus_home_baseline_logloss'] < 0 for x in results)
    wins_coin = sum(x['model_minus_coin_flip_logloss'] < 0 for x in results)
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'sport': SPORT, 'source': 'existing weekly_trainer.py NHL features and fitting',
        'source_mode': 'canonical' if os.getenv('CANONICAL_HISTORY', '0') == '1' else 'historical_snapshots',
        'unique_completed_games': unique_games, 'eligible_examples': n,
        'method': 'expanding-window chronological validation; 3 disjoint 10%-of-data test windows; 10% reserved untouched',
        'read_only': True, 'promotes_models': False, 'odds_api_calls': False,
        'note': 'A training-period constant home-win-rate baseline is stronger than 50/50 but not a sportsbook benchmark. Repeated exploration of these windows can overfit; reserve final 10% for later confirmation. No profitability claims.',
        'wins_vs_home_baseline': wins_home, 'wins_vs_coin_flip': wins_coin,
        'windows': results,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    logging.info('Read-only diagnostic complete: model beat historical home-rate baseline in %d/3 windows; report=%s', wins_home, OUTPUT)


if __name__ == '__main__':
    main()
