"""Read-only audit of a stored NBA moneyline estimate; no Odds API requests.
Place alongside prediction_engine.py and weekly_trainer.py; run with DATABASE_URL set.
Usage: python audit_lakers_probability.py --date 2026-11-02 --team Lakers
"""
import argparse
import math
import os
from datetime import date, datetime, timedelta, timezone
import psycopg
from psycopg.rows import dict_row
import prediction_engine as pe
from weekly_trainer import LEGACY_FEATURES, features, sigmoid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default='2026-11-02', help='Pacific game date YYYY-MM-DD')
    ap.add_argument('--team', default='Lakers', help='Case-insensitive substring in either team')
    args = ap.parse_args()
    day = date.fromisoformat(args.date)
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        model = conn.execute('''SELECT version,coefficients,trained_at,test_logloss,train_games,test_games
            FROM sports_edge_trained_models WHERE sport=%s''', ('basketball_nba',)).fetchone()
        if not model:
            raise SystemExit('No promoted NBA model found')
        weights = model['coefficients']['weights']
        print('MODEL:', model['version'])
        print('TRAINED AT:', model['trained_at'])
        print('HOLDOUT LOG LOSS:', model['test_logloss'], 'train:', model['train_games'], 'test:', model['test_games'])
        if model['coefficients'].get('features') != LEGACY_FEATURES or len(weights) != len(LEGACY_FEATURES):
            raise SystemExit('NBA model feature schema mismatch')
        odds = pe.cached_odds_events(conn, 'basketball_nba', day)
        candidates = [e for e in odds if args.team.lower() in (e.get('home_team','')+' '+e.get('away_team','')).lower()]
        if not candidates:
            raise SystemExit('No matching game in cached odds for requested Pacific date')
        boards = []
        for offset in (-1, 0, 1):
            boards += pe.fetch('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
                               {'dates':(day+timedelta(days=offset)).strftime('%Y%m%d'),'limit':200}).get('events',[])
        matches = pe.match_info(boards)
        for odds_event in candidates:
            home, away = odds_event['home_team'], odds_event['away_team']
            match = matches.get((pe.norm(home), pe.norm(away)))
            if not match:
                print('ESPN MATCH NOT FOUND:', away, '@', home)
                continue
            event, home_comp, away_comp = match
            start = datetime.fromisoformat(odds_event['commence_time'].replace('Z','+00:00'))
            history = pe.archived_training_history(conn, 'basketball_nba', start)
            ht, at = home_comp['team'], away_comp['team']
            hid = str(ht.get('id') or pe.norm(ht.get('displayName')))
            aid = str(at.get('id') or pe.norm(at.get('displayName')))
            x = features(hid, aid, history, 'basketball_nba')
            print('\nMATCH:', away, '@', home, '| event:', odds_event.get('id'), '| start:', start.isoformat())
            print('AVAILABLE PREGAME GAMES:', home, len(history.get(hid,[])), away, len(history.get(aid,[])))
            if x is None:
                print('INSUFFICIENT HISTORY FOR TRAINED PREDICTION')
                continue
            z = sum(w*v for w,v in zip(weights,x))
            p = sigmoid(z)
            print('FEATURE / VALUE / COEFFICIENT / LOG-ODDS CONTRIBUTION')
            for name, value, weight in zip(LEGACY_FEATURES, x, weights):
                print(f'  {name:35s} {value:+.5f} {weight:+.5f} {value*weight:+.5f}')
            print(f'RECONSTRUCTED: {home} {p:.2%}; {away} {1-p:.2%}')
            saved = conn.execute('''SELECT selection,estimated_prob,generated_at,model_version
                FROM model_predictions WHERE event_id=%s AND market='h2h' ORDER BY selection''',
                (odds_event['id'],)).fetchall()
            for row in saved:
                print('CURRENT SAVED PREDICTION:',row['selection'],f"{row['estimated_prob']:.2%}",
                      'generated:',row['generated_at'],'model:',row['model_version'])
            print('NOTE: Current stored predictions may differ from the published card if regenerated.')
            print('NOTE: Archived ESPN game results may have changed since publication; compare carefully.')
            print('NOTE: No injury, rest-day, or back-to-back feature is included in this NBA model.')

if __name__ == '__main__':
    main()
