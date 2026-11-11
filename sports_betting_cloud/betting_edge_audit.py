"""Read-only audit of published hypothetical betting picks. No paid API requests.

Uses recorded, settled picks only; results are selection-biased and do not validate
all candidate prices or model probabilities. No tables are created or changed.
"""
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def break_even(odds):
    return 100 / (100 + odds) if odds > 0 else -odds / (100 - odds)


def unit_profit(result, odds):
    if result == 'Win':
        return odds / 100 if odds > 0 else 100 / abs(odds)
    if result == 'Loss':
        return -1.0
    return 0.0


def group_summary(rows):
    graded = [r for r in rows if r['result'] in ('Win', 'Loss')]
    n = len(graded)
    wins = sum(r['result'] == 'Win' for r in graded)
    risked = sum(float(r['stake']) for r in rows if r['result'] in ('Win', 'Loss'))
    net = sum(unit_profit(r['result'], r['odds']) * float(r['stake']) for r in rows)
    probs = [float(r['estimated_prob']) for r in graded if r['estimated_prob'] is not None]
    calibrated = [r for r in graded if r['estimated_prob'] is not None]
    brier = (sum((float(r['estimated_prob']) - (r['result'] == 'Win')) ** 2 for r in calibrated) / len(calibrated)) if calibrated else None
    logloss = (sum(-math.log(max(1e-9, min(1 - 1e-9, float(r['estimated_prob']) if r['result'] == 'Win' else 1 - float(r['estimated_prob'])))) for r in calibrated) / len(calibrated)) if calibrated else None
    return {'graded': n, 'wins': wins, 'losses': n-wins, 'pushes': sum(r['result'] == 'Push' for r in rows),
            'voids': sum(r['result'] == 'Void' for r in rows), 'pending': sum(r['result'] == 'Pending' for r in rows),
            'win_rate': wins/n if n else None, 'avg_predicted_probability': sum(probs)/len(probs) if probs else None,
            'brier_score': brier, 'log_loss': logloss, 'net_units': round(net, 4),
            'risked_units': round(risked, 4), 'roi': net/risked if risked else None}


def bucket(value, thresholds, labels):
    for cutoff, label in zip(thresholds, labels):
        if value < cutoff:
            return label
    return labels[-1]


def main():
    url = os.environ['DATABASE_URL']
    with psycopg.connect(url, row_factory=dict_row, connect_timeout=15, options='-c statement_timeout=30000') as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            records = conn.execute('''SELECT id,date,sport,matchup,market,selection,odds,stake,result,
                                           estimated_prob,source,notes
                                    FROM picks WHERE kind='Hypothetical' AND source LIKE 'Auto:%%'
                                    ORDER BY date,id''').fetchall()
    rows = []
    for raw in records:
        r = dict(raw)
        try:
            o = int(r['odds'])
            if o == 0 or -100 < o < 100 or r['stake'] is None or float(r['stake']) <= 0:
                continue
            p = float(r['estimated_prob']) if r['estimated_prob'] is not None else None
            if p is not None and (not math.isfinite(p) or not 0 < p < 1):
                p = None
            r['odds'], r['estimated_prob'] = o, p
            r['edge_pp'] = 100 * (p-break_even(o)) if p is not None else None
            rows.append(r)
        except (TypeError, ValueError, OverflowError):
            continue
    groups = defaultdict(list)
    for r in rows:
        keys = {'sport': str(r['sport']), 'market': str(r['market']),
                'odds_type': 'underdog_plus_money' if r['odds'] > 0 else 'favorite_minus_money',
                'odds_band': bucket(r['odds'], [-150, -100, 101, 151, 201], ['-200 to -151', '-150 to -101', '+100', '+101 to +150', '+151 to +200']),
                'edge_band_pp': ('missing' if r['edge_pp'] is None else bucket(r['edge_pp'], [0, 5, 10, 15, float('inf')], ['negative', '0 to <5', '5 to <10', '10 to <15', '15+'])),
                'probability_band': ('missing' if r['estimated_prob'] is None else bucket(r['estimated_prob'], [.3, .4, .5, .6, .7, 1.01], ['<30%', '30–<40%', '40–<50%', '50–<60%', '60–<70%', '70%+'])),
                'model_quality': 'experimental' if r['market'] in ('spreads', 'totals') else 'trained_moneyline' if r['market'] == 'h2h' else 'other'}
        for dimension, label in keys.items():
            groups[(dimension, label)].append(r)
    output = {'generated_utc': datetime.now(timezone.utc).isoformat(),
              'scope': 'Auto-published hypothetical picks only; excludes baseline and manually entered picks',
              'limitations': ['Historical published picks are selection-biased; no analysis of rejected candidate prices.',
                              'Sample sizes may be too small for reliable calibration or profitability conclusions.',
                              'The recorded odds and model probability are publication-time values, not independently verified closing prices.',
                              'No adjusted probability is stored as a dedicated column; only raw published probability is analyzed.',
                              'Pushes/voids excluded from win-rate and risked ROI; pending excluded from realized metrics.',
                              'A favorable ROI does not establish a repeatable betting edge.'],
              'rows_read': len(records), 'rows_valid': len(rows), 'overall': group_summary(rows),
              'groups': [{'dimension': dimension, 'label': label, **group_summary(group)} for (dimension,label),group in sorted(groups.items())]}
    dest = Path(os.environ.get('AUDIT_OUTPUT', 'reports/betting_edge_audit.json'))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(output, indent=2, allow_nan=False) + '\n')
    print('Read-only betting edge audit:', json.dumps({'rows': len(rows), 'overall': output['overall']}, indent=2))
    print('Report:', dest)


if __name__ == '__main__':
    main()
