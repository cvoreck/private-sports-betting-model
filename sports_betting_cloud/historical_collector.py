"""Daily append-only sports archive. Uses existing DATABASE_URL and ODDS_API_KEY.

Stores available ESPN game/boxscore data and current Odds API snapshots. Historical
player props are optional and quota-limited; the free tier may reject them.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

import psycopg
import requests

PT = ZoneInfo('America/Los_Angeles')
SPORTS = {
    'americanfootball_nfl': ('football', 'nfl'),
    'basketball_nba': ('basketball', 'nba'),
    'baseball_mlb': ('baseball', 'mlb'),
    'americanfootball_ncaaf': ('football', 'college-football'),
    'icehockey_nhl': ('hockey', 'nhl'),
    'basketball_ncaab': ('basketball', 'mens-college-basketball'),
}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS historical_snapshots (
 id BIGSERIAL PRIMARY KEY, collected_at TIMESTAMPTZ NOT NULL,
 source TEXT NOT NULL, sport TEXT NOT NULL, event_id TEXT NOT NULL DEFAULT '',
 category TEXT NOT NULL, item_key TEXT NOT NULL DEFAULT '',
 payload JSONB NOT NULL, UNIQUE (source, sport, event_id, category, item_key, collected_at)
);
CREATE INDEX IF NOT EXISTS historical_snapshots_lookup ON historical_snapshots(sport,event_id,category,collected_at DESC);
CREATE TABLE IF NOT EXISTS historical_collection_runs (
 run_date DATE PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL,
 completed_at TIMESTAMPTZ, status TEXT NOT NULL,
 snapshot_count INTEGER NOT NULL DEFAULT 0, detail JSONB NOT NULL DEFAULT '{}'::jsonb
);
'''
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'sports-edge-history/1.0'})


def get(url, params=None):
    response = SESSION.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response


def store(conn, collected_at, source, sport, event_id, category, item_key, payload):
    conn.execute('''INSERT INTO historical_snapshots
      (collected_at,source,sport,event_id,category,item_key,payload)
      VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING''',
      (collected_at, source, sport, str(event_id or ''), category, str(item_key or ''), json.dumps(payload)))


def archive_espn(conn, now, today, stats):
    # Backfill recent results, including previous day's finals; avoids depending on
    # the morning job running after every game's final whistle.
    backfill = max(1, min(30, int(os.getenv('ESPN_BACKFILL_DAYS', '7'))))
    for sport, (domain, league) in SPORTS.items():
        for offset in range(-1, backfill + 1):
            day = today - timedelta(days=offset)
            date_key = day.strftime('%Y%m%d')
            try:
                board = get(f'https://site.api.espn.com/apis/site/v2/sports/{domain}/{league}/scoreboard',
                            {'dates': date_key, 'limit': 300}).json()
            except (requests.RequestException, ValueError) as exc:
                logging.warning('ESPN %s %s: %s', sport, date_key, exc)
                stats['errors'].append(f'ESPN {sport} {date_key}: {exc}')
                continue
            for event in board.get('events') or []:
                eid = str(event.get('id') or '')
                if not eid:
                    continue
                # One daily observation per event; upsert is not needed because
                # timestamp is per-run and each run keeps its own snapshot.
                store(conn, now, 'espn', sport, eid, 'game', date_key, event)
                stats['games'] += 1
                completed = (event.get('status') or {}).get('type', {}).get('completed', False)
                if not completed or offset > 2 or stats['boxscores'] >= 60:
                    continue
                # ESPN summary includes boxscore player/team statistics when offered.
                try:
                    summary = get(f'https://site.api.espn.com/apis/site/v2/sports/{domain}/{league}/summary',
                                  {'event': eid}).json()
                    box = summary.get('boxscore') or {}
                    if box:
                        store(conn, now, 'espn', sport, eid, 'boxscore', date_key, box)
                        stats['boxscores'] += 1
                    # Preserve availability/injury context if present; these
                    # reports are not guaranteed to exist for every league.
                    for category in ('injuries', 'rosters', 'leaders', 'predictor'):
                        if summary.get(category):
                            store(conn, now, 'espn', sport, eid, category, date_key, summary[category])
                except (requests.RequestException, ValueError) as exc:
                    logging.warning('ESPN boxscore %s %s: %s', sport, eid, exc)
                    stats['errors'].append(f'boxscore {sport} {eid}: {exc}')
                time.sleep(0.04)
        conn.commit()


def archive_odds(conn, now, stats):
    """One quota-limited daily snapshot per sport, reused by every downstream step.

    Budget is a maximum estimate, not a guarantee of vendor billing. Actual consumed
    credits are logged from response headers; stop on a low vendor-reported balance.
    """
    key = os.getenv('ODDS_API_KEY')
    if not key:
        logging.warning('ODDS_API_KEY absent: ESPN-only run')
        return
    day = now.astimezone(PT).date()
    weekday = day.weekday()  # Saturday=5, Sunday=6
    # Saturday gives CFB priority; Sunday NFL; weekdays NBA/NHL first.
    if weekday == 5:
        priority = ['americanfootball_ncaaf','basketball_nba','icehockey_nhl','americanfootball_nfl','basketball_ncaab','baseball_mlb']
    elif weekday == 6:
        priority = ['americanfootball_nfl','basketball_nba','icehockey_nhl','americanfootball_ncaaf','basketball_ncaab','baseball_mlb']
    else:
        priority = ['basketball_nba','icehockey_nhl','americanfootball_nfl','baseball_mlb','basketball_ncaab','americanfootball_ncaaf']
    # Quota allocation is adaptive to the *remaining* days in the billing cycle.
    # Configure ODDS_RESET_DAY to the day of month your provider resets credits.
    # Actual provider balance is authoritative; we do not assume calendar reset.
    reserve = max(0, int(os.getenv('ODDS_MONTHLY_RESERVE', '20')))
    monthly_limit = max(1, int(os.getenv('ODDS_MONTHLY_LIMIT', '500')))
    reset_day = max(1, min(28, int(os.getenv('ODDS_RESET_DAY', '1'))))
    cycle_start = date(day.year, day.month, reset_day)
    if day < cycle_start:
        year, month = (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
        cycle_start = date(year, month, reset_day)
    year, month = (cycle_start.year + 1, 1) if cycle_start.month == 12 else (cycle_start.year, cycle_start.month + 1)
    next_reset = date(year, month, reset_day)
    days_left = (next_reset - day).days
    # Weekend weight is larger; allocate available credits by remaining weighted days.
    weight = {5: 2.2, 6: 2.2, 3: 1.2}.get(weekday, 1.0)
    remaining_weight = sum({5: 2.2, 6: 2.2, 3: 1.2}.get((day + timedelta(days=i)).weekday(), 1.0)
                           for i in range(days_left))
    # Persist the last known provider balance across daily runs, and never exceed
    # the balance after reserve. Provider header after each request updates it.
    balance_row = conn.execute("""SELECT payload FROM historical_snapshots
       WHERE source='the-odds-api' AND category='quota_balance'
         AND collected_at >= %s
       ORDER BY collected_at DESC LIMIT 1""", (cycle_start,)).fetchone()
    known_remaining = None
    if balance_row:
        try:
            known_remaining = int(balance_row[0]['remaining'])
        except (KeyError, TypeError, ValueError):
            pass
    # Fail closed: an unknown provider balance cannot establish a hard cap.
    # A balance from an older run can be stale if the key is used elsewhere.
    # ODDS_CONFIRMED_REMAINING is a user-verified dashboard balance for bootstrapping;
    # it is never allowed to increase a lower provider-reported balance.
    confirmed = os.getenv('ODDS_CONFIRMED_REMAINING', '').strip()
    if confirmed:
        try:
            confirmed_balance = max(0, min(monthly_limit, int(confirmed)))
            known_remaining = min(known_remaining, confirmed_balance) if known_remaining is not None else confirmed_balance
        except ValueError:
            logging.error('Invalid ODDS_CONFIRMED_REMAINING; refusing paid requests')
            return
    if known_remaining is None:
        logging.warning('HARD CAP: no confirmed provider balance for current cycle; ESPN archive continues, paid odds disabled')
        return
    # Reserve is held for the first part of the cycle, then becomes spendable.
    # This allows the final 20 credits to be used, without crossing the ceiling.
    effective_reserve = reserve if days_left > 3 else 0
    spendable = max(0, known_remaining - effective_reserve)
    budget = int(spendable * weight / max(remaining_weight, 1))
    # One sport costs at most 3 estimated credits in the current market configuration.
    # Round to a usable market bundle, but do not exceed available spendable balance.
    budget = min(spendable, max(0, budget // 3 * 3))
    # Manual override can only LOWER the adaptive cap.
    if os.getenv('ODDS_DAILY_BUDGET'):
        budget = min(budget, max(0, int(os.environ['ODDS_DAILY_BUDGET'])))
    logging.info('HARD CAP quota plan: reset_day=%s days_left=%s provider_remaining=%s effective_reserve=%s daily_budget=%s',
                 reset_day, days_left, known_remaining, effective_reserve, budget)
    used = 0
    # Only ask for odds for sports with upcoming games in the next 36h.
    # ESPN's schedule is a free filter, not a guarantee that Odds API has prices.
    for sport in priority:
        if used + 3 > budget or known_remaining is None or known_remaining < 3 + effective_reserve:
            logging.info('HARD CAP: stopping before request (budget=%s used=%s remaining=%s reserve=%s)',
                         budget, used, known_remaining, effective_reserve)
            break
        domain, league = SPORTS[sport]
        upcoming = False
        for delta in (0, 1):
            date_key = (day + timedelta(days=delta)).strftime('%Y%m%d')
            try:
                board = get(f'https://site.api.espn.com/apis/site/v2/sports/{domain}/{league}/scoreboard',
                            {'dates':date_key,'limit':300}).json()
                for e in board.get('events') or []:
                    if not (e.get('status') or {}).get('type',{}).get('completed',False):
                        upcoming = True
                        break
            except (requests.RequestException, ValueError) as exc:
                logging.warning('Schedule precheck %s: %s',sport,exc)
            if upcoming: break
        if not upcoming:
            logging.info('Skip %s: no upcoming ESPN events', sport)
            continue
        existing = conn.execute("""SELECT 1 FROM historical_snapshots
            WHERE source='the-odds-api' AND sport=%s AND category='daily_odds_cache'
              AND item_key=%s LIMIT 1""",(sport,day.isoformat())).fetchone()
        if existing:
            logging.info('Reuse existing %s snapshot for %s',sport,day)
            continue
        try:
            response = get(f'https://api.the-odds-api.com/v4/sports/{sport}/odds/', {
                'apiKey':key,'regions':'us','markets':'h2h,spreads,totals',
                'oddsFormat':'american','dateFormat':'iso'})
            events=response.json()
            if not isinstance(events,list): raise ValueError('Unexpected odds response')
            # One US region and three markets; reserve the full estimated cost.
            used += 3
            # Save an empty result too, preventing a second paid call on manual rerun.
            store(conn,now,'the-odds-api',sport,'','daily_odds_cache',day.isoformat(),
                  {'events':events,'request_time':now.isoformat()})
            for event in events:
                if event.get('id'):
                    store(conn,now,'the-odds-api',sport,event['id'],'pregame_odds','h2h_spreads_totals',event)
                    stats['odds_events']+=1
            conn.commit()
            remaining=response.headers.get('x-requests-remaining')
            last=response.headers.get('x-requests-last')
            if remaining is None:
                logging.error('HARD CAP: missing vendor remaining header; refusing subsequent paid requests')
                known_remaining = None
            else:
                try:
                    reported = int(remaining)
                    known_remaining = min(known_remaining, reported) if known_remaining is not None else reported
                    store(conn,now,'the-odds-api',sport,'','quota_balance',
                          day.isoformat(),{'remaining':known_remaining,'last':last,'observed_at':now.isoformat()})
                    conn.commit()
                except ValueError:
                    logging.warning('Unparseable provider remaining-credit header: %s', remaining)
            logging.info('Odds %s: events=%d estimated_daily_spend=%d vendor_last=%s vendor_remaining=%s',
                         sport,len(events),used,last,remaining)
            if known_remaining is None or known_remaining < 3 + effective_reserve:
                logging.warning('HARD CAP: provider balance at spending threshold (%s)', remaining)
                break
        except (requests.RequestException,ValueError) as exc:
            logging.warning('Odds %s unavailable: %s',sport,exc)
            stats['errors'].append(f'odds {sport}: {exc}')
            logging.warning('HARD CAP: stopping after uncertain API outcome; avoid retrying charged requests')
            break
    # Player box scores continue to be archived for free. Paid prop odds disabled
    # until plan support and exact billing are confirmed; no speculative charges.
    logging.info('Paid player-prop requests disabled (MAX_PROP_EVENT_REQUESTS=0).')
    logging.info('Estimated odds credits spent this collector run: <=%d',used)

def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    now = datetime.now(timezone.utc)
    today = now.astimezone(PT).date()
    stats = {'games': 0, 'boxscores': 0, 'odds_events': 0, 'prop_events': 0, 'errors': []}
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=20) as conn:
        conn.execute(SCHEMA)
        conn.execute('''INSERT INTO historical_collection_runs(run_date,started_at,status)
          VALUES(%s,%s,'running') ON CONFLICT(run_date) DO UPDATE SET
          started_at=EXCLUDED.started_at,status='running' ''', (today, now))
        conn.commit()
        try:
            archive_espn(conn, now, today, stats)
            archive_odds(conn, now, stats)
            conn.execute('''UPDATE historical_collection_runs SET completed_at=NOW(),status=%s,
              snapshot_count=(SELECT count(*) FROM historical_snapshots WHERE collected_at=%s),
              detail=%s::jsonb WHERE run_date=%s''',
              ('partial' if stats['errors'] else 'completed', now, json.dumps(stats), today))
            conn.commit()
        except Exception:
            logging.exception('Historical archive failed')
            conn.rollback()
            conn.execute('''UPDATE historical_collection_runs SET completed_at=NOW(),status='failed',detail=%s::jsonb WHERE run_date=%s''',
                         (json.dumps(stats), today))
            conn.commit()
            raise
    logging.info('Historical archive: %s', json.dumps(stats))


if __name__ == '__main__':
    main()
