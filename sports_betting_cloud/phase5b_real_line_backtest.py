#!/usr/bin/env python3
"""Phase 5B: read-only, pre-registered real-line retrospective backtest.

The only stored data accessed are canonical results and historical Odds API snapshots.
No vendor calls, DB writes, or model promotion. No future scores are allowed in
model training, residual calibration, or as-of-quote feature construction.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import math
import os
import statistics
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from phase5a_event_matching_audit import norm, parse_time, valid_market
from spread_total_research_phase2 import build_examples, train_model, prediction

UTC = dt.timezone.utc
SPORTS = ('basketball_nba', 'americanfootball_nfl', 'icehockey_nhl')
CATEGORIES = ('pregame_odds', 'daily_odds_cache')
BOOK_PRIORITY = ('draftkings', 'fanduel', 'betmgm', 'betrivers', 'bovada',
                 'betonlineag', 'betus', 'lowvig', 'mybookieag')
LAMBDA_GRID = (0.001, 0.01, 0.1, 1.0, 10.0)
RESULTS_LAG = dt.timedelta(hours=24)  # conservatively assume final known after 24h
MIN_QUOTE_LEAD = dt.timedelta(minutes=10)
MAX_QUOTE_AGE = dt.timedelta(days=7)
EV_THRESHOLD = 0.05             # preregistered; never tuned on this test period
STAKE_UNITS = 1.0
MIN_PRIOR_EXAMPLES = 300


def finite_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def american_decimal_profit(price):
    if not finite_num(price):
        return None
    if abs(price - round(price)) > 1e-6:
        return None
    price = int(round(price))
    if price >= 100 and price <= 1000:
        return price / 100.0
    if price <= -100 and price >= -1000:
        return 100.0 / abs(price)
    return None


def implied_probability(price):
    profit = american_decimal_profit(price)
    return 1.0 / (profit + 1.0) if profit is not None else None


def as_result(value, market, side, point):
    if market == 'spreads':
        adjusted = value + point if side == 'home' else -value + point
    else:
        adjusted = value - point if side == 'over' else point - value
    return 'W' if adjusted > 1e-8 else ('L' if adjusted < -1e-8 else 'P')


def half_up(x):
    # Scores and score differentials are integer valued. Avoid banker rounding.
    return math.floor(x + 0.5)


def empirical_probs(pred, residuals, market, side, point):
    outcomes = collections.Counter(as_result(half_up(pred + res), market, side, point)
                                   for res in residuals)
    n = len(residuals)
    if n < 1:
        raise ValueError('Need pre-test validation residuals')
    if abs(point-round(point)) > 1e-9:
        # Integer final scores cannot tie a fractional line. Preserve this
        # mathematical certainty instead of inventing a push probability.
        denominator = n + 1.0
        return {'W': (outcomes['W'] + 0.5) / denominator,
                'L': (outcomes['L'] + 0.5) / denominator, 'P': 0.0}
    # Whole-point lines allow real pushes; use three-outcome smoothing.
    denominator = n + 1.5
    return {key: (outcomes[key] + 0.5) / denominator for key in ('W', 'L', 'P')}


def quote_from_market(kind, market, home, away):
    if not valid_market(market, home, away):
        return None
    items = {norm(x.get('name')): x for x in market['outcomes']}
    if kind == 'spreads':
        a, b = items[home], items[away]
        points = {'home': float(a['point']), 'away': float(b['point'])}
        prices = {'home': a['price'], 'away': b['price']}
    else:
        a, b = items['over'], items['under']
        points = {'over': float(a['point']), 'under': float(b['point'])}
        prices = {'over': a['price'], 'under': b['price']}
    if any(american_decimal_profit(v) is None for v in prices.values()):
        return None
    if not all(math.isfinite(v) and abs(v) < 1000 for v in points.values()):
        return None
    return {'points': points, 'prices': {k: int(v) for k, v in prices.items()}}


def gather_events(conn, sport, counts):
    events = {}
    with conn.cursor(name='phase5b_odds_'+sport) as cur:
        cur.execute('''SELECT category,event_id,collected_at,payload FROM historical_snapshots
           WHERE source='the-odds-api' AND sport=%s AND category=ANY(%s)
           ORDER BY collected_at''', (sport, list(CATEGORIES)))
        for row in cur:
            counts['snapshot_rows'] += 1
            data = row['payload']
            if not isinstance(data, dict):
                counts['invalid_payload'] += 1
                continue
            candidates = data.get('events', []) if row['category'] == 'daily_odds_cache' else [data]
            if not isinstance(candidates, list):
                counts['invalid_payload'] += 1
                continue
            for ev in candidates:
                if not isinstance(ev, dict):
                    continue
                eid = str(ev.get('id') or row['event_id'] or '').strip()
                if not eid:
                    counts['missing_odds_id'] += 1
                    continue
                start = parse_time(ev.get('commence_time'))
                home, away = norm(ev.get('home_team')), norm(ev.get('away_team'))
                rec = events.setdefault(eid, {'meta': set(), 'quotes': []})
                rec['meta'].add((home, away, start))
                captured = parse_time(row['collected_at'])
                if not (start and captured) or captured > start-MIN_QUOTE_LEAD:
                    counts['non_pregame_snapshots'] += 1
                    continue
                if start-captured > MAX_QUOTE_AGE:
                    counts['stale_snapshot_observations'] += 1
                    continue
                for book in ev.get('bookmakers') or []:
                    if not isinstance(book, dict):
                        continue
                    bookmaker = str(book.get('key') or '').lower()
                    if not bookmaker:
                        continue
                    for m in book.get('markets') or []:
                        if not isinstance(m, dict) or m.get('key') not in ('spreads', 'totals'):
                            continue
                        kind = m['key']
                        vals = quote_from_market(kind, m, home, away)
                        if vals is None:
                            counts['invalid_market_or_prices'] += 1
                            continue
                        updated_raw = m.get('last_update') or book.get('last_update')
                        updated = parse_time(updated_raw)
                        if updated_raw and updated is None:
                            counts['invalid_last_update'] += 1
                            continue
                        if updated and (updated >= start or updated > captured):
                            counts['late_or_inconsistent_last_update'] += 1
                            continue
                        # A deterministic single bookmaker / single snapshot quote per event-market.
                        rec['quotes'].append({'kind': kind, 'book': bookmaker,
                          'captured_at': captured, 'updated_at': updated,
                          'snapshot_category': row['category'], **vals})
    return events


def choose_quote(quotes, kind):
    candidates = [q for q in quotes if q['kind'] == kind]
    if not candidates:
        return None
    # First select latest valid capture, then fixed bookmaker priority, then
    # latest market update. No ex-post optimal line/odds shopping.
    latest = max(q['captured_at'] for q in candidates)
    latest_quotes = [q for q in candidates if q['captured_at'] == latest]
    return sorted(latest_quotes, key=lambda q: (
        BOOK_PRIORITY.index(q['book']) if q['book'] in BOOK_PRIORITY else len(BOOK_PRIORITY),
        -(q['updated_at'].timestamp() if q['updated_at'] else 0),
        q['book'], 0 if q['snapshot_category'] == 'pregame_odds' else 1))[0]


def choose_training(examples, target):
    n = len(examples)
    if n < MIN_PRIOR_EXAMPLES:
        return None
    cutoff = int(n * 0.8)
    training, validation = examples[:cutoff], examples[cutoff:]
    options = []
    for lam in LAMBDA_GRID:
        model = train_model(training, target, lam)
        pred = [prediction(model, r) for r in validation]
        mae = statistics.mean(abs(p-r[target]) for p, r in zip(pred, validation))
        options.append((mae, lam, pred))
    val_mae, lam, val_predictions = min(options, key=lambda z: (z[0], z[1]))
    residuals = [r[target]-p for p,r in zip(val_predictions, validation)]
    final_model = train_model(examples, target, lam)
    return {'model': final_model, 'residuals': residuals,
            'lambda': lam, 'train_games': len(training), 'validation_games': len(validation),
            'fit_games': n, 'validation_mae': round(val_mae, 4)}


def features_as_of(games, deadline, final_game, sport):
    # Rebuild pregame rolling opponent-adjusted features *as of price capture*.
    # Canonical games within 24h before capture are excluded, as their final
    # scores may not have been publicly available yet.
    past = [g for g in games if parse_time(g['played_at']) <= deadline - RESULTS_LAG
            and str(g['event_id']) != str(final_game['event_id'])]
    placeholder = {'event_id': '__phase5b_target__', 'played_at': deadline,
                   'home_team_id': final_game['home_team_id'],
                   'away_team_id': final_game['away_team_id'],
                   'home_score': 0, 'away_score': 0}
    rows = build_examples(past + [placeholder], sport)
    return rows[-1] if rows and rows[-1]['event_id'] == '__phase5b_target__' else None


def choose_side(pred, residuals, market, q):
    outcomes = []
    for side, point in q['points'].items():
        price = q['prices'][side]
        pp = empirical_probs(pred, residuals, market, side, point)
        net = pp['W'] * american_decimal_profit(price) - pp['L']
        raw = implied_probability(price)
        opp = [k for k in q['prices'] if k != side][0]
        opp_implied = implied_probability(q['prices'][opp])
        fair = raw / (raw + opp_implied)
        conditional_win = pp['W'] / (pp['W'] + pp['L'])
        outcomes.append({'side':side,'point':point,'american_odds':price,
             'p_win':pp['W'], 'p_push':pp['P'], 'p_loss':pp['L'],
             'model_ev_per_unit':net, 'raw_break_even_no_push':raw,
             'no_vig_implied_win_probability':fair,
             'model_conditional_win_probability':conditional_win,
             'conditional_edge_vs_no_vig':conditional_win-fair})
    # Tie-break determined without reference to scores, fixed insertion side order.
    return max(outcomes, key=lambda x:x['model_ev_per_unit'])


def settle(final, market, chosen):
    val = float(final['home_score'])-float(final['away_score']) if market == 'spreads' else float(final['home_score'])+float(final['away_score'])
    result = as_result(val, market, chosen['side'], chosen['point'])
    units = (american_decimal_profit(chosen['american_odds']) if result == 'W'
             else -STAKE_UNITS if result == 'L' else 0.0)
    return result, units


def average(xs):
    return sum(xs)/len(xs) if xs else None


def summary(rows):
    total = len(rows)
    selected = [r for r in rows if r['selected']]
    rr = collections.Counter(x['result'] for x in selected)
    gain = sum(r['units'] for r in selected)
    outputs = {'eligible_game_markets': total, 'selected_bets': len(selected),
        'selected_w': rr['W'], 'selected_l': rr['L'], 'selected_p': rr['P'],
        'net_units': round(gain,4),
        'roi_on_one_unit_stakes': round(gain/len(selected),4) if selected else None,
        'median_model_ev_of_selected': round(statistics.median(r['model_ev_per_unit'] for r in selected),4) if selected else None}
    for label in ('trained_absolute_error','baseline_absolute_error','sportsbook_line_absolute_error'):
        outputs[label+'_mae'] = round(average([r[label] for r in rows]),4) if rows else None
    if selected:
        payoffs = [r['units'] for r in selected]
        se = statistics.stdev(payoffs) / math.sqrt(len(payoffs)) if len(payoffs)>1 else None
        outputs['roi_normal_approx_95pct_interval'] = ([round(gain/len(payoffs)-1.96*se,4),
            round(gain/len(payoffs)+1.96*se,4)] if se is not None else None)
    else:
        outputs['roi_normal_approx_95pct_interval'] = None
    # Brier and log loss for selected direction conditional on no push, only
    # on real outcomes that did not push. Separate push rate is reported above.
    no_push = [r for r in selected if r['result'] != 'P']
    if no_push:
        brier = average([(r['model_conditional_win_probability']-int(r['result']=='W'))**2 for r in no_push])
        ll = average([-math.log(max(1e-9,min(1-1e-9,r['model_conditional_win_probability']))) if r['result']=='W'
                      else -math.log(max(1e-9,min(1-1e-9,1-r['model_conditional_win_probability']))) for r in no_push])
        outputs['conditional_nonpush_brier'] = round(brier,6)
        outputs['conditional_nonpush_log_loss'] = round(ll,6)
        bins = collections.defaultdict(list)
        for r in no_push:
            b=min(9,int(10*r['model_conditional_win_probability']))
            bins[b].append(r)
        outputs['nonpush_calibration_bins'] = [{
          'bin':b, 'n':len(batch),
          'mean_probability':round(average([r['model_conditional_win_probability'] for r in batch]),4),
          'observed_win_rate':round(average([float(r['result']=='W') for r in batch]),4)
        } for b,batch in sorted(bins.items())]
    else:
        outputs['conditional_nonpush_brier'] = None
        outputs['conditional_nonpush_log_loss'] = None
        outputs['nonpush_calibration_bins'] = []
    return outputs


def jsonify_record(row):
    ans = dict(row)
    for field in ('capture_time','event_start'):
        if field in ans and isinstance(ans[field], dt.datetime):
            ans[field] = ans[field].isoformat()
    return ans


def evaluate_sport(conn, sport, now):
    counts = collections.Counter()
    events = gather_events(conn, sport, counts)
    finals = conn.execute('''SELECT event_id,played_at,home_team_id,away_team_id,
                  home_team,away_team,home_score,away_score
          FROM canonical_completed_games
          WHERE sport=%s AND season_type=2 AND played_at<=%s
          ORDER BY played_at,event_id''', (sport, now)).fetchall()
    by_pair = collections.defaultdict(list)
    for g in finals:
        h,a = norm(g['home_team']),norm(g['away_team'])
        if h and a:
            by_pair[(h,a)].append(g)
    pending = []
    matched_by_final = collections.defaultdict(list)
    for eid, record in sorted(events.items()):
        counts['unique_odds_events'] += 1
        if len(record['meta']) != 1:
            counts['conflicting_event_metadata'] += 1
            continue
        h,a,start = next(iter(record['meta']))
        if not(h and a and start):
            counts['incomplete_event_metadata'] += 1
            continue
        if start > now:
            counts['future_event'] += 1
            continue
        matches = [g for g in by_pair.get((h,a), [])
                   if abs((parse_time(g['played_at'])-start).total_seconds()) <= 18*3600]
        if len(matches) != 1:
            counts['unmatched_or_ambiguous_final'] += 1
            continue
        final = matches[0]
        if not all(finite_num(v) and v >= 0 for v in (final['home_score'],final['away_score'])):
            counts['invalid_final_scores'] += 1
            continue
        matched_by_final[str(final['event_id'])].append((eid, final, start, record))
    for target_eid, options in matched_by_final.items():
        if len(options) != 1:
            # Avoid arbitrary selection when more than one vendor ID points to
            # the same result. Never grade duplicate games as separate bets.
            counts['multiple_odds_events_same_final'] += len(options)
            continue
        eid, final, start, record = options[0]
        for market in ('spreads','totals'):
            q = choose_quote(record['quotes'], market)
            if q is None:
                counts['matched_without_eligible_'+market] += 1
                continue
            if q['captured_at'] > parse_time(final['played_at']) - MIN_QUOTE_LEAD:
                # If Odds API start differs from canonical start, err on caution.
                counts['quote_too_late_for_canonical_start'] += 1
                continue
            pending.append((eid,final,start,market,q))
            counts['eligible_matched_'+market] += 1
    # Train just once per sport on observations finished before the earliest
    # decision timestamp. Lambda and residual calibration are pretest only.
    first_capture = min((q['captured_at'] for _,_,_,_,q in pending),default=None)
    out = {'status':'NO_ELIGIBLE_MATCHES','counts':dict(counts),'markets':{},
           'first_test_quote':first_capture.isoformat() if first_capture else None}
    if first_capture is None:
        return out
    fit_cutoff = first_capture - RESULTS_LAG
    prior = [g for g in finals if parse_time(g['played_at']) <= fit_cutoff]
    examples = build_examples(prior, sport)
    trained = {}
    for kind,target in (('spreads','margin'),('totals','total')):
        trained[kind] = choose_training(examples,target)
    if any(trained[k] is None for k in trained):
        out['status']='INSUFFICIENT_PRIOR_TRAINING'
        out['prior_examples']=len(examples)
        return out
    out['status']='RESEARCH_ONLY'
    out['first_test_quote']=first_capture.isoformat()
    out['training_latest_allowed_game_start']=fit_cutoff.isoformat()
    out['prior_examples']=len(examples)
    out['training']={m:{k:v for k,v in cfg.items() if k not in ('model','residuals')}
                     for m,cfg in trained.items()}
    # Cache features by (game, decision time), as spreads and totals typically
    # share the same quote snapshot and should share as-of pregame information.
    cache = {}
    observations = {'spreads':[],'totals':[]}
    for odds_id, final, start, market,q in sorted(pending,key=lambda item:(item[4]['captured_at'],item[0],item[3])):
        cachekey = (str(final['event_id']),q['captured_at'])
        if cachekey not in cache:
            cache[cachekey] = features_as_of(finals,q['captured_at'],final,sport)
        row = cache[cachekey]
        if row is None:
            counts['no_asof_features_'+market] += 1
            continue
        m = trained[market]
        forecast = prediction(m['model'],row)
        base = row['baseline_margin'] if market=='spreads' else row['baseline_total']
        target = 'margin' if market=='spreads' else 'total'
        finalval = (float(final['home_score'])-float(final['away_score']) if market=='spreads'
                    else float(final['home_score'])+float(final['away_score']))
        winner = choose_side(forecast, m['residuals'], market, q)
        result,units = settle(final,market,winner)
        market_line = (-q['points']['home']) if market=='spreads' else q['points']['over']
        rec = {'sport':sport,'market':market,'odds_event_id':odds_id,
          'final_event_id':str(final['event_id']),
          'home_team':final['home_team'],'away_team':final['away_team'],
          'event_start':start,'capture_time':q['captured_at'],
          'bookmaker':q['book'],'home_score':final['home_score'],'away_score':final['away_score'],
          'actual_value':finalval,'forecast':round(forecast,4),'baseline_forecast':round(base,4),
          'sportsbook_line_home_margin_or_total':market_line,
          'trained_absolute_error':abs(forecast-finalval),
          'baseline_absolute_error':abs(base-finalval),
          'sportsbook_line_absolute_error':abs(market_line-finalval),
          **winner,'result':result,'units':units,
          'selected':winner['model_ev_per_unit'] >= EV_THRESHOLD}
        observations[market].append(rec)
    out['counts'] = dict(counts)
    for market, rows in observations.items():
        out['markets'][market] = {'summary':summary(rows),
            'records':[jsonify_record({k:(round(v,6) if isinstance(v,float) else v)
                                      for k,v in r.items()}) for r in rows]}
    return out


def main():
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL missing (existing GitHub Actions secret)')
    now = dt.datetime.now(UTC)
    report = {'generated_at':now.isoformat(), 'read_only':True,
       'database_writes':0,'external_api_calls':0,'paid_api_calls':0,
       'model_promotions':0,'production_changes':0,
       'sports':{},
       'protocol':{
          'universe':'NBA, NFL and NHL regular season verified canonical finals',
          'event_matching':'Phase 5A exact normalized home/away team pair, unique canonical final within 18h of Odds API start',
          'freeze_training':'Model fitted and ridge penalty selected only from games at least 24h before earliest valid test price snapshot per sport',
          'validation':'Historical chronological 80% train / 20% validation selects lambda and obtains held-out residual distribution; final model refit on all pre-period data',
          'features':'Phase 2 build_examples called with only canonical starts at least 24h before the chosen sportsbook snapshot; synthetic zero-score placeholder never used to train',
          'quote_policy':'Latest collected pregame snapshot per event-market >=10min before start (both Odds API/canonical), one bookmaker selected using fixed priority; max 7-day snapshot age',
          'bet_policy':'At most one of two sides per game-market, max ex-ante estimated unit EV; bet only if expected profit >=0.05 units on 1u stake',
          'probabilities':'Empirical pre-period validation residuals rounded to integer scores, smoothed W/L/P counts; not independently calibrated for real lines',
          'stale_final_guard':'Exclude game results occurring in final 24 hours before quote capture',
          'roi':'Hypothetical flat 1-unit stake per selected bet, pushes return 0 units, no transaction costs or execution guarantees',
        },
        'limitations':['Phase 5B historical test sample is narrow (~late Oct to Nov 2026); price observations on a single game are not independent bets.',
          'No real-money recommendation or profitability established; historical selection is exploratory and can overfit analyst choices.',
          'Models do not include injuries, starters, lineups, pace, goaltending or forward-looking covariates.',
          'Prediction error beating a simple scoring baseline does not imply beating sportsbook lines.',
          'Model probabilities use past residual frequencies; calibration and ROI uncertainty may be large.',
          'Games with late, inconsistent, invalid or unmatched odds are omitted; selection bias possible.',
          'Only one bookmaker / one snapshot per event-market is graded; no hindsight best-line shopping.',
          'Regression retrains on all pre-period examples after lambda selection; historical validation residuals are held out only from model-selection fit.']}
    with psycopg.connect(url,row_factory=dict_row,connect_timeout=20) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        for sport in SPORTS:
            logging.info('Running read-only Phase 5B: %s',sport)
            report['sports'][sport] = evaluate_sport(conn,sport,now)
            s=report['sports'][sport]
            logging.info('%s status=%s; spread games=%s, total games=%s',sport,s['status'],
              s.get('markets',{}).get('spreads',{}).get('summary',{}).get('eligible_game_markets'),
              s.get('markets',{}).get('totals',{}).get('summary',{}).get('eligible_game_markets'))
        conn.rollback()
    output = Path('phase5b_real_line_backtest_report.json')
    output.write_text(json.dumps(report,indent=2,sort_keys=True,default=str)+'\n',encoding='utf-8')
    logging.info('Report saved to %s; no API calls, writes, or model promotions',output)


if __name__=='__main__':
    main()
