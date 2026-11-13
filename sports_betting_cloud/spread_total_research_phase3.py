"""Read-only rolling-origin research with empirical residual calibration.

Requires spread_total_research_phase2.py beside this file. No odds API calls,
no production predictions, no database writes, no model promotion.
"""
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from spread_total_research_phase2 import (SPORTS, build_examples, train_model,
                                          prediction, metrics)

LAMBDAS = (0.001, 0.01, 0.1, 1.0, 10.0)
# Each origin: train on all earlier examples, tune on next 10%, test on following 10%.
# Windows are non-overlapping for each sport: 50/10/10, 60/10/10, 70/10/10, 80/10/10.
ORIGINS = (0.50, 0.60, 0.70, 0.80)


def average(values):
    return sum(values) / len(values) if values else None


def empirical_probs(residuals, forecast, line, integer_outcome):
    """Residual bootstrap, with Laplace smoothing, for W/P/L at a fixed line.

    For integer score outcomes, a push occurs if the realized integer score equals
    an integer line. For fractional lines, a push is impossible. The comparison
    uses forecast + validation residual, with nearest-integer rounding only for
    the discrete synthetic outcome (not for estimating an actual score).
    """
    win = push = loss = 0
    for error in residuals:
        simulated = forecast + error
        if integer_outcome:
            simulated = math.floor(simulated + 0.5)
        delta = simulated - line
        if delta > 1e-9:
            win += 1
        elif delta < -1e-9:
            loss += 1
        else:
            push += 1
    # Synthetic half-point lines cannot push for integer final scores.
    # Avoid assigning artificial push probability via smoothing.
    if abs(line - round(line)) > 1e-9:
        denom = len(residuals) + 2
        return ((win + 1) / denom, 0.0, (loss + 1) / denom)
    # Integer lines may push; symmetric smoothing for all three outcomes.
    denom = len(residuals) + 3
    return ((win + 1) / denom, (push + 1) / denom, (loss + 1) / denom)


def evaluate_probabilities(test, forecasts, residuals, target, lines):
    brier = []
    logloss = []
    pushed = 0
    counts = [0] * 10
    sums_p = [0.] * 10
    sums_y = [0.] * 10
    interval_coverage = []
    ordered = sorted(residuals)
    lo = ordered[int(0.05 * (len(ordered)-1))]
    hi = ordered[int(0.95 * (len(ordered)-1))]
    for row, pred, line in zip(test, forecasts, lines):
        win, push, loss = empirical_probs(residuals, pred, line, True)
        # The market outcome is a three-way W/P/L when the line is integral.
        # Brier is evaluated on the binary win event (push counts as not-win),
        # with a separate empirical push rate, avoiding push-as-loss ambiguity.
        actual = row[target]
        y = 1. if actual > line else 0.
        if abs(actual-line) < 1e-9:
            pushed += 1
        brier.append((win-y)**2)
        logloss.append(-(y*math.log(win)+(1-y)*math.log(1-win)))
        idx = min(9, int(win*10))
        counts[idx] += 1
        sums_p[idx] += win
        sums_y[idx] += y
        interval_coverage.append(pred+lo <= actual <= pred+hi)
    bins = [{'bin': i, 'n': counts[i], 'mean_probability': round(sums_p[i]/counts[i],4),
             'observed_win_rate': round(sums_y[i]/counts[i],4)}
            for i in range(10) if counts[i]]
    return {'n': len(test), 'brier_win': round(average(brier), 6),
            'logloss_win': round(average(logloss), 6),
            'observed_pushes': pushed,
            'observed_push_rate': round(pushed/len(test), 5),
            'nominal_90pct_residual_interval_coverage': round(average(interval_coverage), 4),
            'calibration_bins': bins}


def evaluate_market(rows, target, baseline_key):
    n = len(rows)
    folds = []
    all_baseline_errors = []
    all_model_errors = []
    for origin in ORIGINS:
        train_end = int(n * origin)
        valid_end = int(n * (origin + .10))
        test_end = int(n * (origin + .20))
        train = rows[:train_end]
        valid = rows[train_end:valid_end]
        test = rows[valid_end:test_end]
        if len(train) < 100 or len(valid) < 60 or len(test) < 60:
            continue
        candidates = []
        for lam in LAMBDAS:
            model = train_model(train, target, lam)
            vm = metrics(valid, [prediction(model, r) for r in valid], target)
            candidates.append((vm['mae'], lam, model))
        _, lam, model = min(candidates, key=lambda x: (x[0], x[1]))
        trained_valid = [prediction(model, r) for r in valid]
        residuals_model = [r[target]-p for r,p in zip(valid,trained_valid)]
        residuals_baseline = [r[target]-r[baseline_key] for r in valid]
        trained_test = [prediction(model, r) for r in test]
        baseline_test = [r[baseline_key] for r in test]
        all_model_errors.extend(p-r[target] for p,r in zip(trained_test,test))
        all_baseline_errors.extend(p-r[target] for p,r in zip(baseline_test,test))
        # Hypothetical lines are fixed BEFORE looking at each test result:
        # rounded baseline forecast, offset by half a point to avoid pushes.
        # This is NOT a historical sportsbook line or market price.
        lines = [math.floor(p) + 0.5 for p in baseline_test]
        fold = {
            'train_n': len(train), 'validation_n': len(valid), 'test_n': len(test),
            'test_start': test[0]['date'], 'test_end': test[-1]['date'],
            'selected_lambda': lam,
            'mae': {'baseline': metrics(test,baseline_test,target)['mae'],
                    'trained': metrics(test,trained_test,target)['mae']},
            'baseline_calibration': evaluate_probabilities(test,baseline_test,residuals_baseline,target,lines),
            'trained_calibration': evaluate_probabilities(test,trained_test,residuals_model,target,lines),
            'hypothetical_line_rule': 'floor(pregame baseline prediction) + 0.5',
        }
        folds.append(fold)
    if not folds:
        return {'status': 'INSUFFICIENT_DATA'}
    wins = sum(f['mae']['trained'] < f['mae']['baseline'] for f in folds)
    return {'status':'RESEARCH_ONLY', 'folds':folds, 'folds_trained_lower_mae':wins,
            'fold_count':len(folds),
            'pooled_test_mae':{'baseline':round(average([abs(x) for x in all_baseline_errors]),4),
                               'trained':round(average([abs(x) for x in all_model_errors]),4)},
            'note':'Overlapping training windows; non-overlapping test windows. Validation residuals are reused only within each fold. No sportsbook prices or profitability.'}


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL required')
    report = {
        'generated_at':datetime.now(timezone.utc).isoformat(),
        'read_only':True, 'database_writes':0, 'external_api_calls':0, 'model_promotions':0,
        'method':'Four expanding rolling-origin folds; 10% chronological validation and 10% test each; validation-selected ridge penalty; empirical validation-residual calibration.',
        'limitations':[
            'Hypothetical half-point lines, not historical bookmaker lines or odds: no demonstrated EV, ROI, or profitability.',
            'Win-event Brier includes pushes as non-wins; push rate reported separately. Half-point synthetic lines prevent actual pushes in this test.',
            'Empirical residual distributions are not guaranteed calibrated and may be nonstationary.',
            'No injuries, lineups, pace, goaltender or play-by-play data.',
            'Baseline is a scoring-average proxy, not identical to production.',
            'Results are research-only and do not change the publisher.'
        ], 'sports':{}
    }
    with psycopg.connect(url,row_factory=dict_row,connect_timeout=20) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        for sport in SPORTS:
            logging.info('Research %s',sport)
            games=conn.execute('''SELECT event_id,played_at,home_team_id,away_team_id,home_score,away_score
                FROM canonical_completed_games WHERE sport=%s AND season_type=2
                ORDER BY played_at,event_id''',(sport,)).fetchall()
            examples=build_examples(games,sport)
            if len(examples)<400:
                report['sports'][sport]={'status':'INSUFFICIENT_DATA','eligible_games':len(examples)}
                continue
            report['sports'][sport]={'status':'RESEARCH_ONLY','eligible_games':len(examples),
                'markets':{'spread':evaluate_market(examples,'margin','baseline_margin'),
                           'total':evaluate_market(examples,'total','baseline_total')}}
        conn.rollback()
    output=Path('spread_total_phase3_report.json')
    output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    logging.info('Saved %s; database unchanged',output)

if __name__=='__main__':
    main()
