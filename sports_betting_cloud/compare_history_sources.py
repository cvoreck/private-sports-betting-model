"""READ-ONLY comparison of raw ESPN and canonical training/prediction inputs.

Does not fit or promote models, write to the database, or call external APIs.
"""
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from weekly_trainer import SPORTS, dataset, load_training_rows, sigmoid, MARGIN_SCALE, NHL_FEATURES, LEGACY_FEATURES


def get_rows(conn, sport, canonical):
    original = os.environ.get('CANONICAL_HISTORY')
    try:
        os.environ['CANONICAL_HISTORY'] = '1' if canonical else '0'
        return load_training_rows(conn, sport)
    finally:
        if original is None:
            os.environ.pop('CANONICAL_HISTORY', None)
        else:
            os.environ['CANONICAL_HISTORY'] = original


def compare(conn, sport, promoted):
    raw_rows = get_rows(conn, sport, False)
    canonical_rows = get_rows(conn, sport, True)
    raw, raw_games = dataset(raw_rows, sport)
    compact, compact_games = dataset(canonical_rows, sport)
    raw_map = {r[1]: r for r in raw}
    compact_map = {r[1]: r for r in compact}
    shared = sorted(raw_map.keys() & compact_map.keys())
    only_raw = sorted(raw_map.keys() - compact_map.keys())
    only_compact = sorted(compact_map.keys() - raw_map.keys())
    feature_differences = []
    outcome_differences = []
    probability_differences = []
    largest_probability_difference = 0.0
    model = promoted.get(sport)
    model_compatible = False
    if model:
        coeff = model['coefficients'] or {}
        expected = NHL_FEATURES if sport == 'icehockey_nhl' else LEGACY_FEATURES
        weights = coeff.get('weights')
        model_compatible = (coeff.get('features') == expected and coeff.get('margin_scale') == MARGIN_SCALE[sport]
                            and isinstance(weights, list) and len(weights) == len(expected)
                            and all(isinstance(w, (int, float)) and math.isfinite(w) for w in weights))
    else:
        weights = None
    for eid in shared:
        a, b = raw_map[eid], compact_map[eid]
        if a[0] != b[0] or a[3] != b[3]:
            if len(outcome_differences) < 12:
                outcome_differences.append({'event_id': eid, 'raw_date': a[0].isoformat(), 'compact_date': b[0].isoformat(), 'raw_outcome': a[3], 'compact_outcome': b[3]})
        if len(a[2]) != len(b[2]) or any(not math.isclose(x, y, abs_tol=1e-10, rel_tol=1e-10) for x, y in zip(a[2], b[2])):
            if len(feature_differences) < 12:
                feature_differences.append({'event_id': eid, 'raw_features': a[2], 'compact_features': b[2]})
        if model_compatible and len(a[2]) == len(weights) and len(b[2]) == len(weights):
            pa = sigmoid(sum(w * x for w, x in zip(weights, a[2])))
            pb = sigmoid(sum(w * x for w, x in zip(weights, b[2])))
            delta = abs(pa-pb)
            largest_probability_difference = max(largest_probability_difference, delta)
            if delta > 1e-10 and len(probability_differences) < 12:
                probability_differences.append({'event_id': eid, 'raw_probability': pa, 'compact_probability': pb, 'absolute_difference': delta})
    feature_mismatch_count = sum(1 for eid in shared if len(raw_map[eid][2]) != len(compact_map[eid][2]) or any(not math.isclose(x,y,abs_tol=1e-10,rel_tol=1e-10) for x,y in zip(raw_map[eid][2],compact_map[eid][2])))
    outcome_mismatch_count = sum(1 for eid in shared if raw_map[eid][0] != compact_map[eid][0] or raw_map[eid][3] != compact_map[eid][3])
    status = 'MATCH' if not only_raw and not only_compact and not feature_mismatch_count and not outcome_mismatch_count else 'DIFFERENCES_FOUND'
    return {
        'status': status,
        'raw_snapshot_rows_loaded': len(raw_rows), 'canonical_rows_loaded': len(canonical_rows),
        'raw_unique_regular_final_games': raw_games, 'canonical_unique_regular_final_games': compact_games,
        'raw_eligible_training_examples': len(raw), 'canonical_eligible_training_examples': len(compact),
        'shared_examples': len(shared), 'only_raw_examples_count': len(only_raw), 'only_canonical_examples_count': len(only_compact),
        'only_raw_example_ids_sample': only_raw[:12], 'only_canonical_example_ids_sample': only_compact[:12],
        'feature_mismatch_count': feature_mismatch_count, 'outcome_or_date_mismatch_count': outcome_mismatch_count,
        'feature_mismatch_sample': feature_differences, 'outcome_or_date_mismatch_sample': outcome_differences,
        'promoted_model_present': model is not None, 'promoted_model_compatible': model_compatible,
        'max_shared_example_probability_delta': largest_probability_difference if model_compatible else None,
        'probability_mismatch_sample': probability_differences,
        'warning': 'This compares training examples and promoted-model probabilities on shared historical games. It does NOT prove equivalence of live event predictions, all research scripts, or a database restore.'
    }


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if not os.environ.get('DATABASE_URL'):
        raise RuntimeError('DATABASE_URL GitHub secret required')
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'read_only': True,
              'database_writes': 0, 'external_api_calls': 0, 'model_promotions': 0,
              'source': 'existing weekly_trainer.load_training_rows and dataset', 'sports': {}}
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row, connect_timeout=20) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        promoted = {r['sport']: r for r in conn.execute('SELECT sport, coefficients FROM sports_edge_trained_models').fetchall()}
        for sport in SPORTS:
            logging.info('Comparing %s', sport)
            report['sports'][sport] = compare(conn, sport, promoted)
            logging.info('%s: %s', sport, report['sports'][sport]['status'])
        conn.rollback()
    report['all_sports_match'] = all(r['status'] == 'MATCH' for r in report['sports'].values())
    report['decision'] = 'REVIEW_REQUIRED_BEFORE_DELETION'  # Always; other reader/restore audits remain.
    output = Path('history_source_comparison_report.json')
    output.write_text(json.dumps(report, indent=2, default=str) + '\n', encoding='utf-8')
    print(json.dumps({'all_sports_match': report['all_sports_match'],
                      'statuses': {s: r['status'] for s,r in report['sports'].items()},
                      'report': str(output), 'deletions': 0}, indent=2))

if __name__ == '__main__':
    main()
