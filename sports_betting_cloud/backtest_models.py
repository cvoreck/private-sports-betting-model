"""Read-only, date-grouped walk-forward probability audit for Sports Edge Analytics.

Run: python sports_betting_cloud/backtest_models.py
Requires DATABASE_URL, psycopg, and the existing weekly_trainer.py.
Uses ESPN archive only; no Odds API calls, database writes, or betting ROI claims.
"""
import json
import logging
import math
import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from weekly_trainer import SPORTS, dataset, predict, train_candidate

LOG = logging.getLogger('sports_edge_backtest')
MIN_TRAIN = 200
MIN_TEST = 25
FOLDS = 4
BINS = 10
EPS = 1e-12


def clip(p):
    return max(EPS, min(1.0 - EPS, float(p)))


def logloss(ys, ps):
    return -sum(y * math.log(clip(p)) + (1 - y) * math.log1p(-clip(p))
                for y, p in zip(ys, ps)) / len(ys)


def brier(ys, ps):
    return sum((float(p) - y) ** 2 for y, p in zip(ys, ps)) / len(ys)


def summarize(ys, ps):
    if not ys or len(ys) != len(ps):
        raise ValueError('Expected equally sized, nonempty outcomes and probabilities')
    bins = []
    for k in range(BINS):
        pairs = [(y, p) for y, p in zip(ys, ps)
                 if min(BINS - 1, int(clip(p) * BINS)) == k]
        if pairs:
            bins.append({'range': [k / BINS, (k + 1) / BINS],
                         'count': len(pairs),
                         'mean_probability': sum(p for _, p in pairs) / len(pairs),
                         'observed_win_rate': sum(y for y, _ in pairs) / len(pairs)})
    ece = sum(b['count'] * abs(b['observed_win_rate'] - b['mean_probability'])
              for b in bins) / len(ys)
    mce = max(abs(b['observed_win_rate'] - b['mean_probability']) for b in bins)
    return {'games': len(ys), 'log_loss': logloss(ys, ps),
            'brier_score': brier(ys, ps),
            'mean_predicted_home_win_rate': sum(ps) / len(ps),
            'observed_home_win_rate': sum(ys) / len(ys),
            'expected_calibration_error': ece,
            'maximum_calibration_error': mce,
            'calibration_bins': bins}


def baselines(train, test):
    """All baseline parameters are determined using past training games only."""
    train_y = [int(r[3]) for r in train]
    # Laplace smoothing prevents certainty in small samples.
    historical_home_rate = (sum(train_y) + 1) / (len(train_y) + 2)
    return {'coin_flip': [0.5] * len(test),
            'historical_home_rate': [historical_home_rate] * len(test)}


def metrics_for(ys, ps):
    return {'log_loss': logloss(ys, ps), 'brier_score': brier(ys, ps)}


def date_boundaries(data, folds):
    """Find chronological boundaries without splitting games on the same UTC date."""
    n = len(data)
    # Each requested threshold is moved forward to the first new date.
    boundaries = []
    for i in range(folds + 1):
        target = int(n * (0.4 + 0.6 * i / folds))
        if i == folds:
            boundaries.append(n)
            continue
        while 0 < target < n and data[target - 1][0].date() == data[target][0].date():
            target += 1
        boundaries.append(target)
    return boundaries


def backtest_rows(rows, sport, folds=FOLDS):
    data, games = dataset(rows, sport)
    data = sorted(data, key=lambda r: r[0])
    n = len(data)
    boundaries = date_boundaries(data, folds)
    all_y, all_p, fold_reports = [], [], []
    pooled_baselines = {'coin_flip': [], 'historical_home_rate': []}
    for i in range(folds):
        start, end = boundaries[i], boundaries[i + 1]
        train, test = data[:start], data[start:end]
        if len(train) < MIN_TRAIN or len(test) < MIN_TEST:
            continue
        if train[-1][0].date() >= test[0][0].date():
            raise ValueError('Date overlap between train and test')
        details = {}
        weights = train_candidate(train, sport, details)
        probs = list(predict(weights, test))
        ys = [int(r[3]) for r in test]
        baseline_ps = baselines(train, test)
        report = summarize(ys, probs)
        baseline_metrics = {name: metrics_for(ys, ps)
                            for name, ps in baseline_ps.items()}
        report.update({'fold': i + 1, 'train_games': len(train),
                       'train_last': train[-1][0].isoformat(),
                       'test_first': test[0][0].isoformat(),
                       'test_last': test[-1][0].isoformat(),
                       'baselines': baseline_metrics,
                       'improvement_vs_baselines': {
                           name: {'log_loss_reduction': m['log_loss'] - report['log_loss'],
                                  'brier_reduction': m['brier_score'] - report['brier_score']}
                           for name, m in baseline_metrics.items()},
                       # Backward-compatible fields for existing report consumers.
                       'baseline_log_loss': baseline_metrics['coin_flip']['log_loss'],
                       'baseline_brier': baseline_metrics['coin_flip']['brier_score']})
        fold_reports.append(report)
        all_y.extend(ys)
        all_p.extend(probs)
        for name, ps in baseline_ps.items():
            pooled_baselines[name].extend(ps)
    result = {'sport': sport, 'archived_unique_games': games,
              'pregame_examples': n, 'folds': fold_reports,
              'evaluation_type': 'date-grouped expanding-window out-of-sample',
              'limitations': [
                  'No historical pregame odds: ROI, realizable betting edge, and market comparison are not evaluated.',
                  'The historical home-rate baseline is stronger than a coin flip but not a sportsbook baseline.',
                  'Historical ESPN archive completeness, event timestamps, and team IDs affect coverage.',
                  'Results evaluate re-trained candidates, not necessarily currently promoted production weights.',
                  'College models are evaluated on all archived regular-season games, not exclusively AP Top 25 eligible games.',
                  'Calibration metrics are descriptive and can be noisy in sparsely populated bins.',
                  'Date grouping prevents same-date boundary overlap but does not by itself audit upstream feature leakage.'
              ]}
    if all_y:
        overall = summarize(all_y, all_p)
        overall['baselines'] = {name: metrics_for(all_y, ps)
                                for name, ps in pooled_baselines.items()}
        overall['improvement_vs_baselines'] = {
            name: {'log_loss_reduction': m['log_loss'] - overall['log_loss'],
                   'brier_reduction': m['brier_score'] - overall['brier_score']}
            for name, m in overall['baselines'].items()}
        overall['baseline_log_loss'] = overall['baselines']['coin_flip']['log_loss']
        overall['baseline_brier'] = overall['baselines']['coin_flip']['brier_score']
        overall['folds_beating_home_rate_log_loss'] = sum(
            f['log_loss'] < f['baselines']['historical_home_rate']['log_loss']
            for f in fold_reports)
        result['overall'] = overall
    else:
        result['status'] = 'insufficient_data'
    return result


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if not os.getenv('DATABASE_URL'):
        raise RuntimeError('DATABASE_URL is required')
    output = {'generated_at': datetime.now(timezone.utc).isoformat(),
              'method': 'date-grouped chronological expanding-window probability evaluation',
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
                    LOG.info('%s: games=%d model_logloss=%.6f coinflip=%.6f home_rate=%.6f ECE=%.4f folds_beating_home=%d/%d',
                             sport, m['games'], m['log_loss'], m['baseline_log_loss'],
                             m['baselines']['historical_home_rate']['log_loss'],
                             m['expected_calibration_error'],
                             m['folds_beating_home_rate_log_loss'], len(report['folds']))
                else:
                    LOG.warning('%s: insufficient data', sport)
    filename = 'sports_edge_backtest_report.json'
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2)
    LOG.info('Report saved: %s', filename)


if __name__ == '__main__':
    main()
