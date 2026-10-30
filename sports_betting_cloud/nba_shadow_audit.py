"""Read-only NBA moneyline shadow audit of currently saved production predictions.

Does not re-train, publish, place wagers, settle bets, or write to the DB.
Uses the most recent cached odds for each matched event and the stored
prediction version. A snapshot is NOT a historical backtest.
"""
import csv
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

SPORT = 'basketball_nba'
PT = ZoneInfo('America/Los_Angeles')
T = 0.70
OUT = Path('nba_shadow_audit_report.json')
CSV = Path('nba_shadow_comparisons.csv')


def clip(p):
    return max(1e-9, min(1 - 1e-9, float(p)))


def calibrated(p):
    p = clip(p)
    z = math.log(p / (1 - p)) / T
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def implied(odds):
    if not isinstance(odds, (int, float)) or isinstance(odds, bool) or odds == 0:
        return None
    return 100 / (100 + odds) if odds > 0 else -odds / (100 - odds)


def units(edge_pp):
    """Experimental stake tiers. Non-positive edge is explicitly NO BET (0u)."""
    if not math.isfinite(edge_pp):
        raise ValueError('Estimated edge must be finite')
    if edge_pp <= 0:
        return 0
    return 5 if edge_pp >= 15 else 4 if edge_pp >= 10 else 3 if edge_pp >= 6 else 2 if edge_pp >= 3 else 1


def fetch_table(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def main():
    if not os.environ.get('DATABASE_URL'):
        raise RuntimeError('DATABASE_URL secret is missing')
    now = datetime.now(timezone.utc)
    day = now.astimezone(PT).date().isoformat()
    report = {
        'created_at_utc': now.isoformat(),
        'pacific_date': day,
        'scope': 'NBA h2h only; saved production predictions matched to cached sportsbook odds',
        'temperature': T,
        'experimental': True,
        'read_only': True,
        'limitations': [
            'This is a point-in-time shadow comparison, not a scored backtest.',
            'No future outcomes are known and no profitability is established.',
            'Historical saved predictions may have been overwritten by upserts.',
            'Cached odds and model predictions may have different timestamps.',
            'The historical temperature 0.70 is not validated on the current promoted model.',
            'Edge is model probability minus the sportsbook raw break-even probability (includes vig).',
            'Unit tiers are experimental; not validated as a staking system.',
            'Non-positive edges receive 0u (no bet); 0u selections are still displayed for diagnostic comparison.',
            'Spreads and totals are excluded because they use different, heuristic models.'
        ],
        'promoted_model': None,
        'predictions_examined': 0,
        'matched_quotes': 0,
        'comparisons': [],
        'status': 'pending'
    }
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row, connect_timeout=20) as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            models = fetch_table(conn, '''SELECT version, coefficients, trained_at
                FROM sports_edge_trained_models WHERE sport=%s''', (SPORT,))
            if not models:
                report['status'] = 'no_promoted_nba_model'
            else:
                model = models[0]
                meta = model['coefficients'] or {}
                expected = ['intercept', 'win_rate_diff_last10', 'margin_diff_last10_scaled']
                compatible = (meta.get('features') == expected and meta.get('margin_scale') == 15.0
                              and isinstance(meta.get('weights'), list) and len(meta['weights']) == 3
                              and all(isinstance(v, (int, float)) and math.isfinite(v) for v in meta['weights']))
                report['promoted_model'] = {
                    'version': model['version'],
                    'trained_at': model['trained_at'].isoformat(),
                    'feature_metadata_compatible': compatible
                }
                if not compatible:
                    report['status'] = 'promoted_model_incompatible'
                else:
                    predictions = fetch_table(conn, '''SELECT event_id, selection, estimated_prob,
                            generated_at, model_version FROM model_predictions
                        WHERE market='h2h' AND model_version=%s
                        ORDER BY generated_at DESC LIMIT 10000''', (model['version'],))
                    predictions = [r for r in predictions if r['generated_at'].astimezone(PT).date().isoformat() == day]
                    report['predictions_examined'] = len(predictions)
                    snapshots = fetch_table(conn, '''SELECT payload, collected_at
                        FROM historical_snapshots
                        WHERE source='the-odds-api' AND sport=%s
                          AND category='daily_odds_cache' AND item_key=%s
                        ORDER BY collected_at DESC LIMIT 1''', (SPORT, day))
                    quotes = {}
                    if snapshots:
                        payload = snapshots[0]['payload'] or {}
                        for event in payload.get('events', []):
                            eid = event.get('id')
                            if not eid:
                                continue
                            for book in event.get('bookmakers', []):
                                for market in book.get('markets', []):
                                    if market.get('key') != 'h2h':
                                        continue
                                    for outcome in market.get('outcomes', []):
                                        price = outcome.get('price')
                                        if implied(price) is not None:
                                            quotes.setdefault((eid, outcome.get('name')), []).append({
                                                'bookmaker': book.get('key') or book.get('title'),
                                                'odds': price,
                                                'event_start': event.get('commence_time'),
                                                'cache_collected_at': snapshots[0]['collected_at'].isoformat()
                                            })
                    for prediction in predictions:
                        matches = quotes.get((prediction['event_id'], prediction['selection']), [])
                        if not matches:
                            continue
                        # Deterministic single quote for comparability; not best-price optimization.
                        quote = sorted(matches, key=lambda q: (str(q['bookmaker']), q['odds']))[0]
                        raw = clip(prediction['estimated_prob'])
                        adj = calibrated(raw)
                        breakeven = implied(quote['odds'])
                        edge_raw = (raw - breakeven) * 100
                        edge_adj = (adj - breakeven) * 100
                        report['comparisons'].append({
                            'event_id': prediction['event_id'],
                            'selection': prediction['selection'],
                            'model_version': prediction['model_version'],
                            'prediction_generated_at': prediction['generated_at'].isoformat(),
                            **quote,
                            'break_even_probability': round(breakeven, 6),
                            'original_probability': round(raw, 6),
                            'shadow_probability': round(adj, 6),
                            'original_edge_pp': round(edge_raw, 4),
                            'shadow_edge_pp': round(edge_adj, 4),
                            'original_experimental_units': units(edge_raw),
                            'shadow_experimental_units': units(edge_adj),
                            'unit_change': units(edge_adj) - units(edge_raw),
                            'original_expected_roi_per_unit': round(raw * (quote['odds'] / 100 if quote['odds'] > 0 else 100 / -quote['odds']) - (1 - raw), 6),
                            'shadow_expected_roi_per_unit': round(adj * (quote['odds'] / 100 if quote['odds'] > 0 else 100 / -quote['odds']) - (1 - adj), 6)
                        })
                    report['matched_quotes'] = len(report['comparisons'])
                    report['status'] = 'comparison_ready' if report['comparisons'] else 'no_matching_today_predictions_and_odds'
    OUT.write_text(json.dumps(report, indent=2), encoding='utf-8')
    columns = ['event_id', 'selection', 'model_version', 'prediction_generated_at', 'bookmaker',
               'odds', 'event_start', 'cache_collected_at', 'break_even_probability',
               'original_probability', 'shadow_probability', 'original_edge_pp', 'shadow_edge_pp',
               'original_experimental_units', 'shadow_experimental_units', 'unit_change',
               'original_expected_roi_per_unit', 'shadow_expected_roi_per_unit']
    with CSV.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(report['comparisons'])
    print('Status:', report['status'])
    print('Promoted model:', report['promoted_model'])
    print('Predictions examined:', report['predictions_examined'])
    print('Matched quotes:', report['matched_quotes'])
    print('Reports:', OUT, CSV)


if __name__ == '__main__':
    main()
