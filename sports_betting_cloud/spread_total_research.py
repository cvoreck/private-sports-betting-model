"""Read-only, chronological spread/total score research using canonical completed games.

No sportsbook lines are available in this table. The study evaluates predicted final
margin and total, NOT betting ROI or calibrated sportsbook cover probabilities.
"""
import json
import logging
import math
import os
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SPORTS = ('basketball_nba', 'icehockey_nhl', 'americanfootball_nfl')
MIN_HISTORY = 5
WINDOW = 12
HALF_LIFE_GAMES = 6
RIDGE = 10.0


def mean(values):
    return sum(values) / len(values) if values else 0.0


def weighted(values):
    """Most recent game last; exponentially downweight older observations."""
    weights = [2 ** (-(len(values) - 1 - i) / HALF_LIFE_GAMES) for i in range(len(values))]
    return sum(x * w for x, w in zip(values, weights)) / sum(weights)


def solve(a, b):
    """Small dense linear system with partial pivoting, stdlib only."""
    n = len(b)
    a = [list(row) + [b[i]] for i, row in enumerate(a)]
    for i in range(n):
        pivot = max(range(i, n), key=lambda k: abs(a[k][i]))
        if abs(a[pivot][i]) < 1e-10:
            raise ValueError('Singular training matrix')
        a[i], a[pivot] = a[pivot], a[i]
        scale = a[i][i]
        a[i] = [x / scale for x in a[i]]
        for j in range(n):
            if j == i:
                continue
            scale = a[j][i]
            for k in range(i, n + 1):
                a[j][k] -= scale * a[i][k]
    return [a[i][n] for i in range(n)]


def fit(train, target):
    p = len(train[0]['x'])
    xtx = [[0.0] * p for _ in range(p)]
    xty = [0.0] * p
    for r in train:
        x, y = r['x'], r[target]
        for i in range(p):
            xty[i] += x[i] * y
            for j in range(p):
                xtx[i][j] += x[i] * x[j]
    for i in range(1, p):
        xtx[i][i] += RIDGE * len(train)  # penalty on normalized features
    return solve(xtx, xty)


def predict(beta, x):
    return sum(a * b for a, b in zip(beta, x))


def metrics(rows, prediction_key, actual_key):
    errors = [r[prediction_key] - r[actual_key] for r in rows]
    return {'n': len(errors), 'mae': round(mean([abs(x) for x in errors]), 4),
            'rmse': round(math.sqrt(mean([x * x for x in errors])), 4),
            'bias': round(mean(errors), 4)}


def make_examples(games):
    """Strictly pregame features; no target game's result in any feature."""
    histories = defaultdict(lambda: deque(maxlen=WINDOW))
    examples = []
    seen = set()
    for g in games:
        eid = str(g['event_id'])
        if eid in seen:
            continue
        seen.add(eid)
        home, away = str(g['home_team_id']), str(g['away_team_id'])
        if not home or not away or home == away:
            continue
        hs, a_s = float(g['home_score']), float(g['away_score'])
        if not all(math.isfinite(x) and x >= 0 for x in (hs, a_s)):
            continue
        hh, ah = histories[home], histories[away]
        if len(hh) >= MIN_HISTORY and len(ah) >= MIN_HISTORY:
            # History tuples are (points_for, points_against, home_flag).
            h_for = weighted([x[0] for x in hh]); h_against = weighted([x[1] for x in hh])
            a_for = weighted([x[0] for x in ah]); a_against = weighted([x[1] for x in ah])
            # Existing scoring-average baseline; opponent defense is included in research features.
            baseline_home = (h_for + a_against) / 2
            baseline_away = (a_for + h_against) / 2
            base_margin = baseline_home - baseline_away
            base_total = baseline_home + baseline_away
            # Normalize features to manageable units for ridge penalty.
            x = [1.0, base_margin / 20.0, base_total / 100.0,
                 (h_for - a_for) / 20.0, (h_against - a_against) / 20.0,
                 (weighted([x[0] - x[1] for x in hh]) - weighted([x[0] - x[1] for x in ah])) / 20.0]
            examples.append({'id': eid, 'date': g['played_at'].isoformat(), 'x': x,
                             'margin': hs - a_s, 'total': hs + a_s,
                             'baseline_margin': base_margin, 'baseline_total': base_total})
        hh.append((hs, a_s, 1))
        ah.append((a_s, hs, 0))
    return examples


def evaluate(examples):
    n = len(examples)
    if n < 300:
        return {'status': 'INSUFFICIENT_DATA', 'eligible_games': n}
    # Fixed chronological splits; final 20% never used for model fitting or selection.
    cut1, cut2 = int(n * .6), int(n * .8)
    train, validation, test = examples[:cut1], examples[cut1:cut2], examples[cut2:]
    result = {'status': 'RESEARCH_ONLY', 'eligible_games': n,
              'splits': {'train': len(train), 'validation': len(validation), 'test': len(test),
                         'test_start': test[0]['date'], 'test_end': test[-1]['date']},
              'markets': {}}
    for target in ('margin', 'total'):
        beta = fit(train, target)
        base_key = 'baseline_' + target
        val = [dict(r, trained=predict(beta, r['x'])) for r in validation]
        tst = [dict(r, trained=predict(beta, r['x'])) for r in test]
        result['markets']['spread' if target == 'margin' else 'total'] = {
            'validation': {'baseline': metrics(val, base_key, target), 'trained': metrics(val, 'trained', target)},
            'test': {'baseline': metrics(tst, base_key, target), 'trained': metrics(tst, 'trained', target)},
            'coefficients': [round(x, 6) for x in beta],
            'interpretation': 'Spread evaluation uses actual home-minus-away score margin; total uses sum of scores. No sportsbook lines, prices, pushes or EV are inferred.'
        }
    return result


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL is required')
    report = {'generated_at': datetime.now(timezone.utc).isoformat(), 'read_only': True,
              'database_writes': 0, 'external_api_calls': 0, 'model_promotions': 0,
              'method': 'Chronological 60/20/20, per-team rolling 12 completed games, 5-game minimum, ridge regression; fixed settings chosen before seeing test results.',
              'limitations': ['No historical bookmaker lines or odds: cannot establish cover probability, push rate, EV, ROI or profitability.',
                              'Baseline is a rolling scoring average proxy, not a byte-for-byte reproduction of the production experimental model.',
                              'No injuries, starting lineups, pace, strength-of-schedule or market information.',
                              'Only regular-season games; predictions are not deployed.'],
              'sports': {}}
    with psycopg.connect(url, row_factory=dict_row, connect_timeout=20) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        for sport in SPORTS:
            logging.info('Loading canonical finals: %s', sport)
            games = conn.execute('''SELECT event_id, played_at, home_team_id, away_team_id, home_score, away_score
                 FROM canonical_completed_games WHERE sport=%s AND season_type=2
                 ORDER BY played_at ASC, event_id ASC''', (sport,)).fetchall()
            report['sports'][sport] = evaluate(make_examples(games))
            logging.info('%s: %s', sport, report['sports'][sport]['status'])
        conn.rollback()
    out = Path('spread_total_research_report.json')
    out.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    logging.info('Saved %s; database unchanged', out)


if __name__ == '__main__':
    main()
