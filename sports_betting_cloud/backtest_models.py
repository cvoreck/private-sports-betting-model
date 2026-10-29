"""Read-only, walk-forward calibration backtest for Sports Edge Analytics.

Place next to weekly_trainer.py and run: python sports_betting_cloud/backtest_models.py
Requires DATABASE_URL and existing psycopg dependency. No Odds API requests.
No historical betting ROI is claimed: ESPN results alone do not provide historical
pre-game sportsbook prices or prove that a wager could have been placed.
"""
import json
import logging
import math
import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from weekly_trainer import SPORTS, dataset, fit, predict, logloss, train_candidate

LOG = logging.getLogger('sports_edge_backtest')
MIN_TRAIN = 200
MIN_TEST = 25
FOLDS = 4
BINS = 10


def brier(ys, ps):
    return sum((p - y) ** 2 for y, p in zip(ys, ps)) / len(ys)


def summarize(ys, ps):
    n = len(ys)
    bins = []
    for k in range(BINS):
        pairs = [(y, p) for y, p in zip(ys, ps)
                 if min(BINS - 1, int(p * BINS)) == k]
        if pairs:
            bins.append({'range': [k / BINS, (k + 1) / BINS],
                         'count': len(pairs),
                         'mean_probability': sum(p for _, p in pairs) / len(pairs),
                         'observed_win_rate': sum(y for y, _ in pairs) / len(pairs)})
    return {'games': n, 'log_loss': logloss(ys, ps), 'brier_score': brier(ys, ps),
            'mean_predicted_home_win_rate': sum(ps) / n,
            'observed_home_win_rate': sum(ys) / n,
            'calibration_bins': bins}


def backtest_rows(rows, sport, folds=FOLDS):
    data, games = dataset(rows, sport)
    n = len(data)
    # Expanding training windows and disjoint subsequent evaluation windows.
    # Earliest 40% is always training-only; each of four folds tests next 15%.
    boundaries = [int(n * (0.4 + 0.6 * i / folds)) for i in range(folds + 1)]
    all_y, all_p, fold_reports = [], [], []
    for i in range(folds):
        start, end = boundaries[i], boundaries[i + 1]
        train, test = data[:start], data[start:end]
        if len(train) < MIN_TRAIN or len(test) < MIN_TEST:
            continue
        details = {}
        weights = train_candidate(train, sport, details)
        probs = predict(weights, test)
        ys = [r[3] for r in test]
        # Fit/tune exclusively on past rows. Never retrain on this test block.
        report = summarize(ys, probs)
        report.update({'fold': i + 1, 'train_games': len(train),
                       'test_first': test[0][0].isoformat(),
                       'test_last': test[-1][0].isoformat(),
                       'baseline_log_loss': logloss(ys, [0.5] * len(ys)),
                       'baseline_brier': brier(ys, [0.5] * len(ys))})
        fold_reports.append(report)
        all_y.extend(ys)
        all_p.extend(probs)
    result = {'sport': sport, 'archived_unique_games': games,
              'pregame_examples': n, 'folds': fold_reports,
              'evaluation_type': 'expanding-window out-of-sample',
              'limitations': [
                  'No historical odds: betting ROI and estimated edge are not evaluated.',
                  'Historical ESPN archive completeness and team IDs affect coverage.',
                  'Results are for a re-trained candidate, not necessarily the promoted production model.',
                  'Same-date games can straddle fold boundaries; strict date grouping is recommended for final audit.',
              ]}
    if all_y:
        result['overall'] = summarize(all_y, all_p)
        result['overall']['baseline_log_loss'] = logloss(all_y, [0.5] * len(all_y))
        result['overall']['baseline_brier'] = brier(all_y, [0.5] * len(all_y))
    else:
        result['status'] = 'insufficient_data'
    return result


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if not os.getenv('DATABASE_URL'):
        raise RuntimeError('DATABASE_URL is required')
    output = {'generated_at': datetime.now(timezone.utc).isoformat(),
              'method': 'chronological expanding-window model probability evaluation',
              'sports': []}
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row) as conn:
        with conn.transaction():
            for sport in SPORTS:
                LOG.info('Loading %s archived ESPN results', sport)
                rows = conn.execute('''SELECT event_id,payload FROM historical_snapshots
                    WHERE source='espn' AND sport=%s AND category='game'
                    ORDER BY collected_at DESC LIMIT 30000''', (sport,)).fetchall()
                report = backtest_rows(rows, sport)
                output['sports'].append(report)
                if report.get('overall'):
                    m = report['overall']
                    LOG.info('%s: test_games=%d logloss=%.6f baseline=%.6f brier=%.6f',
                             sport, m['games'], m['log_loss'], m['baseline_log_loss'], m['brier_score'])
                else:
                    LOG.warning('%s: insufficient data', sport)
            # No writes. Read-only by design.
    filename = 'sports_edge_backtest_report.json'
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2)
    LOG.info('Report saved: %s', filename)


if __name__ == '__main__':
    main()
