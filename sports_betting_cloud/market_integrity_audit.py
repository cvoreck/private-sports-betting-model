"""Read-only audit of archived pregame sportsbook markets and completed ESPN games.

No external HTTP, database writes, model promotion, or betting recommendations.
Only compares historical model probabilities when their stored generated_at precedes kickoff.
"""
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

MARKETS = ('h2h', 'spreads', 'totals')


def timestamp(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    try:
        d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return d if d.tzinfo else None
    except (TypeError, ValueError):
        return None


def implied(price):
    if not isinstance(price, (int, float)) or isinstance(price, bool) or price == 0:
        return None
    return 100 / (100 + price) if price > 0 else -price / (100 - price)


def outcome_key(outcome):
    return (str(outcome.get('name', '')), str(outcome.get('point', '')))


def market_pair(market):
    outcomes = market.get('outcomes') or []
    if len(outcomes) != 2:
        return None
    a, b = outcomes
    pa, pb = implied(a.get('price')), implied(b.get('price'))
    if pa is None or pb is None or pa + pb <= 0:
        return None
    if market.get('key') == 'spreads':
        try:
            if abs(float(a['point']) + float(b['point'])) > 1e-7:
                return None
        except (ValueError, TypeError, KeyError):
            return None
    if market.get('key') == 'totals':
        if str(a.get('point')) != str(b.get('point')) or {a.get('name'), b.get('name')} != {'Over', 'Under'}:
            return None
    return [(a, pa / (pa + pb)), (b, pb / (pa + pb))]


def final_event(payload):
    if not isinstance(payload, dict):
        return None
    if str((payload.get('season') or {}).get('type')) != '2':
        return None
    if not ((payload.get('status') or {}).get('type') or {}).get('completed'):
        return None
    competitors = ((payload.get('competitions') or [{}])[0]).get('competitors') or []
    if len(competitors) != 2:
        return None
    try:
        home = next(x for x in competitors if x.get('homeAway') == 'home')
        away = next(x for x in competitors if x.get('homeAway') == 'away')
        hs, aws = float(home['score']), float(away['score'])
        if not all(map(math.isfinite, (hs, aws))):
            return None
        return {'home': (home.get('team') or {}).get('displayName'),
                'away': (away.get('team') or {}).get('displayName'),
                'home_score': hs, 'away_score': aws, 'start': timestamp(payload.get('date'))}
    except (StopIteration, ValueError, TypeError, KeyError, IndexError):
        return None


def normalize(name):
    return ''.join(c for c in str(name or '').lower() if c.isalnum())


def resolve(market, outcome, game, odds_event):
    """Return 1 win, 0 loss, None push/unknown; no guessing team matches."""
    key = market['key']
    name = str(outcome.get('name', ''))
    home, away = game['home_score'], game['away_score']
    if key == 'totals':
        try:
            value = home + away - float(outcome['point'])
        except (KeyError, ValueError, TypeError):
            return None
        if name == 'Under':
            value = -value
        elif name != 'Over':
            return None
    else:
        if normalize(name) == normalize(odds_event.get('home_team')) == normalize(game['home']):
            value = home - away
        elif normalize(name) == normalize(odds_event.get('away_team')) == normalize(game['away']):
            value = away - home
        else:
            return None
        if key == 'spreads':
            try:
                value += float(outcome['point'])
            except (KeyError, ValueError, TypeError):
                return None
    if abs(value) < 1e-8:
        return None
    return int(value > 0)


def summarize(rows):
    if not rows:
        return {'samples': 0}
    brier = sum((r['fair_probability'] - r['won']) ** 2 for r in rows) / len(rows)
    ll = -sum(r['won'] * math.log(max(1e-9, r['fair_probability'])) +
              (1-r['won']) * math.log(max(1e-9, 1-r['fair_probability'])) for r in rows) / len(rows)
    return {'samples': len(rows), 'market_no_vig_brier': round(brier, 6),
            'market_no_vig_log_loss': round(ll, 6),
            'mean_market_probability': round(sum(r['fair_probability'] for r in rows)/len(rows), 5),
            'observed_win_rate': round(sum(r['won'] for r in rows)/len(rows), 5)}


def audit(odds_rows, espn_rows, prediction_rows=None):
    finals = {}
    finals_by_teams = defaultdict(list)
    for r in espn_rows:
        game = final_event(r['payload'])
        if game:
            finals[(r['sport'], str(r['event_id']))] = game
            finals_by_teams[(r['sport'], normalize(game['home']), normalize(game['away']))].append(game)
    # Choose the earliest archived pregame snapshot per event. This prevents
    # hindsight selection of a better-looking later price.
    earliest = {}
    counts = Counter()
    for r in odds_rows:
        event = r['payload']
        if not isinstance(event, dict):
            counts['malformed_odds_payload'] += 1
            continue
        collected, start = timestamp(r['collected_at']), timestamp(event.get('commence_time'))
        if not collected or not start or collected >= start:
            counts['not_confirmed_pregame'] += 1
            continue
        key = (r['sport'], str(r['event_id']))
        if key not in earliest or collected < earliest[key][0]:
            earliest[key] = (collected, event)
    predictions = defaultdict(list)
    for r in prediction_rows or []:
        predictions[(r['sport'], str(r['event_id']))].append(r)
    by_sport_market = defaultdict(list)
    prediction_evaluations = defaultdict(list)
    coverage = defaultdict(Counter)
    for (sport, eid), (collected, event) in earliest.items():
        coverage[sport]['events_with_pregame_odds'] += 1
        game = finals.get((sport, eid))
        if not game:
            # Odds API IDs and ESPN IDs differ. Match only exact normalized
            # home/away names with a unique kickoff within 12 hours.
            start = timestamp(event.get('commence_time'))
            matches = [g for g in finals_by_teams.get((sport, normalize(event.get('home_team')), normalize(event.get('away_team'))), [])
                       if start and g.get('start') and abs((g['start'] - start).total_seconds()) <= 43200]
            if len(matches) == 1:
                game = matches[0]
                coverage[sport]['matched_by_teams_and_kickoff'] += 1
        if not game:
            start = timestamp(event.get('commence_time'))
            if start and start > datetime.now(timezone.utc):
                coverage[sport]['future_event_not_final'] += 1
            elif (sport, normalize(event.get('home_team')), normalize(event.get('away_team'))) in finals_by_teams:
                coverage[sport]['team_match_but_kickoff_unmatched'] += 1
            else:
                coverage[sport]['missing_final_or_team_name_mismatch'] += 1
            continue
        coverage[sport]['events_with_final_result'] += 1
        # Fixed deterministic reference: alphabetically first sportsbook per market.
        market_quotes = {}
        for book in sorted(event.get('bookmakers') or [], key=lambda b: str(b.get('key', ''))):
            for market in book.get('markets') or []:
                mk = market.get('key')
                if mk not in MARKETS or mk in market_quotes:
                    continue
                pair = market_pair(market)
                if pair:
                    market_quotes[mk] = pair
        for mk, pair in market_quotes.items():
            # One side per event-market to avoid double counting opposing bets.
            # Home team for moneylines/spreads, Over for totals.
            if mk == 'totals':
                selected = next(((o, p) for o, p in pair if o.get('name') == 'Over'), None)
            else:
                selected = next(((o, p) for o, p in pair
                                 if normalize(o.get('name')) == normalize(game['home'])), None)
            if not selected:
                coverage[sport][f'{mk}_unmatched_team'] += 1
                continue
            outcome, fair = selected
            won = resolve({'key': mk}, outcome, game, event)
            if won is None:
                coverage[sport][f'{mk}_push_or_unresolved'] += 1
                continue
            by_sport_market[(sport, mk)].append({'fair_probability': fair, 'won': won})
            coverage[sport][f'{mk}_resolved_events'] += 1
        # Existing predictions are mutable; only use rows explicitly timestamped pregame.
        # Archive can have multiple runs per event. One immutable prediction
        # per event/market/selection/line avoids duplicate samples.
        seen_prediction_keys = set()
        for pred in sorted(predictions.get((sport,eid), []), key=lambda r: timestamp(r.get('generated_at')) or datetime.min.replace(tzinfo=timezone.utc)):
            ptime = timestamp(pred.get('generated_at'))
            start = timestamp(event.get('commence_time'))
            if not ptime or not start or ptime >= start:
                coverage[sport]['prediction_missing_proven_pregame_timestamp'] += 1
                continue
            mk = pred.get('market')
            unique_key = mk  # exactly one historical model observation per event-market
            if unique_key in seen_prediction_keys:
                continue
            if mk not in market_quotes:
                continue
            for outcome, fair in market_quotes[mk]:
                if normalize(pred.get('selection')) != normalize(outcome.get('name')):
                    continue
                if mk in ('spreads', 'totals') and str(pred.get('point_key', '')) != str(outcome.get('point', '')):
                    continue
                won = resolve({'key': mk}, outcome, game, event)
                prob = pred.get('estimated_prob')
                if won is None or not isinstance(prob, (int, float)) or not 0 < prob < 1:
                    continue
                seen_prediction_keys.add(unique_key)
                prediction_evaluations[(sport, mk)].append({'model_probability': prob,
                    'fair_probability': fair, 'won': won})
                break
    model_reports = {}
    for (sport, mk), items in sorted(prediction_evaluations.items()):
        model_brier = sum((x['model_probability'] - x['won']) ** 2 for x in items)/len(items)
        market_brier = sum((x['fair_probability'] - x['won']) ** 2 for x in items)/len(items)
        model_reports[f'{sport}/{mk}'] = {'samples': len(items),
            'model_brier': round(model_brier, 6), 'same_sample_market_brier': round(market_brier, 6),
            'model_minus_market_brier': round(model_brier-market_brier, 6),
            'warning': 'Immutable snapshots are trustworthy only for predictions archived after deployment; no retrospective reconstruction.'}
    return {'generated_at': datetime.now(timezone.utc).isoformat(),
            'method': 'earliest archived pregame snapshot; fixed sportsbook order; one fixed side per event-market',
            'coverage': {k: dict(v) for k, v in sorted(coverage.items())},
            'market_baselines': {f'{s}/{m}': summarize(rows) for (s,m),rows in sorted(by_sport_market.items())},
            'historical_prediction_comparisons': model_reports,
            'notes': ['No Odds API or ESPN calls. No database writes.',
                'This is a market calibration audit, not a strategy ROI or spread/total model backtest.',
                'Odds API event IDs may differ from ESPN event IDs; missing matches are reported, not guessed.',
                'The earliest archived snapshot may be hours before a bet would be placed.',
                'Only append-only predictions recorded after deployment are evaluated; prior overwritten predictions cannot be recovered.',
                'Pushes and unresolved team names are excluded from binary metrics.',
                'Spread and total market baselines are not evidence of trained spread/total model performance.',
                'No model promotion or live changes are authorized by this report.'],
            'counters': dict(counts)}


def main():
    url = os.getenv('DATABASE_URL')
    if not url:
        raise SystemExit('DATABASE_URL missing')
    with psycopg.connect(url, row_factory=dict_row, connect_timeout=10) as conn:
        # Transaction-level read-only protection, including any accidental future SQL.
        conn.execute('SET TRANSACTION READ ONLY')
        odds = conn.execute("""SELECT sport,event_id,collected_at,payload FROM historical_snapshots
            WHERE source='the-odds-api' AND category='pregame_odds'
            ORDER BY collected_at ASC LIMIT 100000""").fetchall()
        espn = conn.execute("""SELECT sport,event_id,payload FROM historical_snapshots
            WHERE source='espn' AND category='game' AND (payload->'status'->'type'->>'completed')='true'
            ORDER BY collected_at ASC LIMIT 100000""").fetchall()
        # Only immutable archive rows. Do not silently fall back to mutable predictions.
        preds = conn.execute("""SELECT sport,event_id,market,selection,point_key,estimated_prob,generated_at
            FROM prediction_snapshots WHERE generated_at < now()
            ORDER BY generated_at ASC LIMIT 100000""").fetchall()
    result = audit(odds, espn, preds)
    out = Path('sports_edge_market_validation.json')
    out.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('Audit report:', out)
    print('Archived pregame events:', sum(x.get('events_with_pregame_odds',0) for x in result['coverage'].values()))
    print('Resolved market categories:', len(result['market_baselines']))
    print('Pregame model comparisons:', len(result['historical_prediction_comparisons']))


if __name__ == '__main__':
    main()
