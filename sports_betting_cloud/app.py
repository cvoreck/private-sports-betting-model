"""Sports Edge Analytics — Streamlit dashboard, backed by the existing Supabase ledger.
Replace sports_betting_cloud/app.py with this file; keep storage.py and analytics.py.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import html
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, urlopen
import json
import re
from threading import RLock
import streamlit as st
from storage import init, add, all_picks, grade, baseline, set_baseline, import_rows
from analytics import stats, format_record

st.set_page_config(page_title="Sports Betting Analytics", page_icon="🏟️", layout="wide", initial_sidebar_state="collapsed")
PT = ZoneInfo("America/Los_Angeles")
TODAY = datetime.now(PT).date()
SPORTS = ['NFL', 'NBA', 'MLB', 'College Football', 'NHL', 'College Basketball']
SPORT_ICONS = ['🏈', '🏀', '⚾', '🏈', '🏒', '🏀']

st.markdown("""<style>
:root {color-scheme:dark}
html, body, .stApp, [data-testid="stAppViewContainer"] {background:#041b2e !important;color:#edf5ff!important}
[data-testid="stSidebar"],[data-testid="stSidebarCollapsedControl"],[data-testid="stDecoration"],#MainMenu,footer {display:none!important}
[data-testid="stHeader"] {background:transparent!important;height:0!important}
.block-container {max-width:1550px!important;padding:0.35rem 2.5rem 3rem!important}
h1 {font-size:2.5rem!important;letter-spacing:-.045em!important;color:#f6f8ff!important;margin:0!important}
h2,h3 {color:#f5f8ff!important;letter-spacing:-.025em!important}
[data-testid="stCaptionContainer"] {color:#b5cce4!important}
.dashboard-section {border:1px solid #16486d;background:#041a2d;border-radius:8px;padding:11px 13px;margin:8px 0}
.section-title {font-size:1.07rem;font-weight:750;color:#f4f8ff;line-height:1.3}
.section-sub {font-size:.8rem;color:#b6c9e0;margin:2px 0 7px}
.metrics-grid {display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}
.metric-box,.sport-box {background:linear-gradient(110deg,#092943,#08233c);border:1px solid #15486f;border-radius:6px;padding:12px 13px;min-width:0}
.metric-label {font-size:.82rem;color:#b8cde5;margin-bottom:6px}
.metric-value {font-size:1.8rem;font-weight:750;line-height:1.15;color:#f5f8ff}
.positive {color:#00e4aa!important}.negative {color:#ff7181!important}
.sports-grid {display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-top:7px}
.sport-name {font-size:.82rem;font-weight:750;color:#f4f7fb;white-space:nowrap}
.sport-record {font-size:1rem;margin-top:8px;color:#e9f3ff;white-space:nowrap}
.sport-profit {font-size:.88rem;font-weight:700;margin-left:8px}
.recap-flex {display:flex;justify-content:space-between;align-items:center;gap:20px}
.recap-result {font-size:1.3rem;font-weight:750;white-space:nowrap}
.recap-list {margin-top:10px;display:grid;gap:7px}
.recap-item {display:flex;justify-content:space-between;gap:12px;align-items:center;border-top:1px solid #16486d;padding:8px 2px 0;font-size:.85rem;color:#dceaff}
.recap-item-detail {color:#adc7df;font-size:.75rem;margin-top:2px}
.recap-item-result {font-weight:800;white-space:nowrap;text-align:right}
.pick-card {display:grid;grid-template-columns:36px 94px minmax(230px,2.15fr) minmax(86px,.7fr) minmax(86px,.7fr) minmax(86px,.7fr) minmax(180px,1.65fr) minmax(180px,1.65fr);border:1px solid #164c72;border-radius:7px;background:linear-gradient(110deg,#082841,#061d33);margin:6px 0;overflow:hidden}
.pick-card>div {padding:13px 11px;min-width:0;border-right:1px solid #143b5b}
.pick-card>div:last-child {border-right:none}
.pick-image {display:block;width:84px;height:100px;max-width:100%;object-fit:contain;border-radius:7px;background:#12344d;padding:5px}
.pick-image-fallback {display:flex;align-items:center;justify-content:center;font-size:2.4rem}
.pick-image-cell {display:flex;align-items:flex-start;justify-content:center;padding:9px 4px!important}
.pick-image-pair {display:flex;flex-direction:column;gap:4px;align-items:center;width:100%}
.pick-image-pair .pick-image {width:74px;height:48px;padding:3px}

.pick-card .pick-rank {padding-left:4px;padding-right:4px}
.pick-rank {font-size:1.85rem;font-weight:800;text-align:center;color:#fff}
.pick-name {font-size:1.02rem;font-weight:800;color:#f4f8ff;line-height:1.35}
.pick-meta {font-size:.76rem;color:#b9cee7;margin:5px 0 10px}
.pick-desc {font-size:.79rem;color:#c5d7ec;line-height:1.45}
.pick-odds-line {display:flex;align-items:baseline;gap:7px;flex-wrap:wrap;margin:7px 0 8px;color:#c5d7ec;font-size:.78rem}
.pick-odds-line strong {font-size:1.05rem;color:#f4f8ff}
.pick-odds-context {font-size:.7rem;color:#9ebbd7}
.pick-market-lines {font-size:.76rem;color:#c5d7ec;margin:0 0 10px;line-height:1.5}
.pick-market-lines strong {color:#f2f8ff;font-weight:750}
.pick-market-muted {color:#9ebbd7;font-size:.71rem}
.pick-preview-label {color:#f1f7ff;font-weight:750}
.pick-preview {color:#c5d7ec;line-height:1.45}
.pick-kpi-label {font-size:.97rem;font-weight:750;color:#e3efff;line-height:1.35;letter-spacing:-.01em}
.pick-kpi-value {font-size:1.27rem;font-weight:750;color:#f5f9ff;margin-top:9px}
.pick-col-title {font-size:.81rem;font-weight:800;color:#fff;margin-bottom:9px}
.pick-bullets {font-size:.76rem;color:#c5d8ee;line-height:1.47;overflow-wrap:anywhere}
.pick-bullets ul {padding-left:15px;margin:0}.pick-bullets li {margin-bottom:5px}
.final-table {width:100%;border-collapse:collapse;color:#dceaff;font-size:.81rem}
.final-table th {text-align:left;color:#d0e1f5;background:#08243c;padding:8px;border-bottom:1px solid #174768}
.final-table td {padding:8px;border-bottom:1px solid #113b59}
.final-table tr:nth-child(even) {background:#07233a}
[data-testid="stVerticalBlockBorderWrapper"] {background:#061f34!important;border-color:#174669!important}
[data-testid="stDataFrame"] {border:1px solid #16486d;border-radius:7px}
@media(max-width:1250px){.sports-grid{grid-template-columns:repeat(3,1fr)}.pick-card{grid-template-columns:36px 90px minmax(200px,2fr) repeat(3,minmax(80px,1fr))}.pick-card .analysis-cell{grid-column:1 / span 4;border-top:1px solid #16486d}.pick-card .watch-cell{grid-column:5 / span 2;border-top:1px solid #16486d}}
@media(max-width:680px){.block-container{padding:1rem!important}.metrics-grid{grid-template-columns:repeat(2,1fr)}.sports-grid{grid-template-columns:repeat(2,1fr)}.pick-card{display:grid;grid-template-columns:40px 94px minmax(0,1fr)}.pick-card>div{border-bottom:1px solid #16486d}.pick-card .analysis-cell,.pick-card .watch-cell{grid-column:1 / -1}.recap-flex{display:block}}
</style>""", unsafe_allow_html=True)

if 'DATABASE_URL' not in st.secrets:
    st.error('Set DATABASE_URL in Streamlit app settings → Secrets.')
    st.stop()
try:
    init()
except Exception:
    st.error('Cannot connect to Supabase. Check DATABASE_URL and your Supabase project status.')
    st.stop()

all_df = all_picks()
if all_df is None:
    all_df = pd.DataFrame()
for col in ['date','sport','market','matchup','selection','kind','result','source','notes','start_pt','result_source']:
    if col not in all_df.columns: all_df[col] = pd.Series(dtype='object')
for col in ['odds','stake','rank','estimated_prob']:
    if col not in all_df.columns: all_df[col] = pd.Series(dtype='float64')

page = 'Dashboard'  # Single-page dashboard; no top navigation
kind = 'Hypothetical'
if page in ('Add pick', 'Grade results', 'Import / backup'):
    kind = st.selectbox('Pick type', ['Hypothetical','Actual'],
                        help='Actual wagers remain separate from hypothetical recommendations.')
# A fresh public performance ledger begins October 28, 2026. Earlier rows remain
# untouched in Supabase but do not contribute to the new record or pending counts.
RECORD_START_DATE = '2026-10-28'
df = all_df[all_df['kind'].eq(kind) & (all_df['date'].astype(str) >= RECORD_START_DATE)].copy()


def number(v, default=0.0):
    try:
        return float(v) if pd.notna(v) else default
    except (TypeError, ValueError):
        return default


def odds_break_even(v):
    o = number(v, None)
    if o is None or (-100 < o < 100): return None
    return 100/(1+o/100) if o>0 else 100*abs(o)/(100+abs(o))


def profit(row):
    result = str(row.get('result','')).lower()
    stake = number(row.get('stake'))
    odds = number(row.get('odds'))
    if result == 'win': return stake * (odds/100 if odds > 0 else 100/abs(odds)) if odds else 0
    if result == 'loss': return -stake
    if result in ('push','void'): return 0
    return None


def make_table(data):
    rows=[]
    for _, r in data.iterrows():
        be=odds_break_even(r.get('odds'))
        prob=number(r.get('estimated_prob'), None)
        edge=(prob*100-be) if prob is not None and be is not None and prob>0 else None
        p=profit(r)
        row = {'#': int(r['rank']) if pd.notna(r.get('rank')) else '—',
                     'Sport':r.get('sport',''), 'Matchup':r.get('matchup',''),
                     'Pick':r.get('selection',''), 'Odds':f"{int(r['odds']):+d}" if pd.notna(r.get('odds')) else '—',
                     'Stake':f"{number(r.get('stake')):.2f}u",
                     'Win %':f'{prob*100:.1f}%' if prob else '—',
                     'Break-even %':f'{be:.1f}%' if be is not None else '—',
                     'Edge':f'{edge:+.1f} pp' if edge is not None else '—',
                     'Result':r.get('result','Pending'),
                     'P/L':f'{p:+.2f}u' if p is not None else '—',
                     'Time (PT)':r.get('start_pt') or '—',
                     'Notes':r.get('notes') or ''}
        rows.append(row)
    return pd.DataFrame(rows)


def header(text, caption=None):
    st.subheader(text)
    if caption: st.caption(caption)

st.title("Sports Betting Analytics")
st.caption(f"Performance tracking and research ledger • {TODAY:%A, %B %d, %Y} • Pacific Time")

def clean_text(value):
    """Keep missing database fields from appearing as 'nan' in the UI."""
    return str(value).strip() if pd.notna(value) and str(value).strip() else ''


def pick_analysis(r):
    """Use only recorded analysis; never manufacture injury or form information."""
    notes = clean_text(r.get('notes'))
    rationale, warnings = [], []
    for line in notes.splitlines():
        item = line.strip().lstrip('-• ').strip()
        lower = item.lower()
        if lower.startswith(('watch out for:', 'watch out:', 'risks:', 'injuries:', 'injury news:', 'suspensions:')):
            warnings.append(item.split(':', 1)[1].strip())
        elif lower.startswith(('why:', 'rationale:', 'analysis:')):
            rationale.append(item.split(':', 1)[1].strip())
        elif lower.startswith(('image url:', 'source:', 'book:', 'model:', 'ev:')):
            continue
        elif item:
            rationale.append(item)
    return '\n\n'.join(rationale), '\n\n'.join(warnings)


def final_card_table(data):
    rows = []
    for _, r in data.iterrows():
        odds = number(r.get('odds'), None)
        prob = number(r.get('estimated_prob'), None)
        be = odds_break_even(odds) if odds is not None else None
        edge = prob*100-be if prob is not None and prob>0 and be is not None else None
        rows.append({
            'Rank': int(r['rank']) if pd.notna(r.get('rank')) else '—',
            'Pick': str(r.get('selection') or '—'),
            'Odds': f'{int(odds):+d}' if odds is not None else '—',
            'Break-even': f'{be:.1f}%' if be is not None else '—',
            'Estimated probability': f'{prob*100:.1f}%' if prob is not None and prob>0 else '—',
            'Estimated edge': f'{edge:+.1f} pp' if edge is not None else '—'
        })
    return pd.DataFrame(rows)

def esc(value):
    return html.escape(clean_text(value), quote=True)


def signed_units(value):
    n=number(value)
    return f'{n:+.2f}u'


def signed_class(value):
    return 'positive' if number(value)>=0 else 'negative'


def html_lines(text):
    items=[x.strip().lstrip('•- ').strip() for x in str(text).splitlines() if x.strip()]
    if not items: return '<p>Not yet documented or verified.</p>'
    return '<ul>'+''.join(f'<li>{esc(item)}</li>' for item in items)+'</ul>'


def pick_image(notes):
    for line in clean_text(notes).splitlines():
        if line.lower().startswith('image url:'):
            u=line.split(':',1)[1].strip()
            parsed=urlsplit(u)
            if parsed.scheme=='https' and ((parsed.hostname or '').endswith('.espn.com') or (parsed.hostname or '').endswith('.espncdn.com')):
                return u
    return ''

# ESPN CDN team logos are a display fallback, not evidence of roster availability.
TEAM_LOGOS = {
    'NFL': dict(zip(
        ['Arizona Cardinals','Atlanta Falcons','Baltimore Ravens','Buffalo Bills','Carolina Panthers','Chicago Bears','Cincinnati Bengals','Cleveland Browns','Dallas Cowboys','Denver Broncos','Detroit Lions','Green Bay Packers','Houston Texans','Indianapolis Colts','Jacksonville Jaguars','Kansas City Chiefs','Las Vegas Raiders','Los Angeles Chargers','Los Angeles Rams','Miami Dolphins','Minnesota Vikings','New England Patriots','New Orleans Saints','New York Giants','New York Jets','Philadelphia Eagles','Pittsburgh Steelers','San Francisco 49ers','Seattle Seahawks','Tampa Bay Buccaneers','Tennessee Titans','Washington Commanders'],
        ['ari','atl','bal','buf','car','chi','cin','cle','dal','den','det','gb','hou','ind','jax','kc','lv','lac','lar','mia','min','ne','no','nyg','nyj','phi','pit','sf','sea','tb','ten','wsh'])),
    'NBA': dict(zip(
        ['Atlanta Hawks','Boston Celtics','Brooklyn Nets','Charlotte Hornets','Chicago Bulls','Cleveland Cavaliers','Dallas Mavericks','Denver Nuggets','Detroit Pistons','Golden State Warriors','Houston Rockets','Indiana Pacers','Los Angeles Clippers','Los Angeles Lakers','Memphis Grizzlies','Miami Heat','Milwaukee Bucks','Minnesota Timberwolves','New Orleans Pelicans','New York Knicks','Oklahoma City Thunder','Orlando Magic','Philadelphia 76ers','Phoenix Suns','Portland Trail Blazers','Sacramento Kings','San Antonio Spurs','Toronto Raptors','Utah Jazz','Washington Wizards'],
        ['atl','bos','bkn','cha','chi','cle','dal','den','det','gs','hou','ind','lac','lal','mem','mia','mil','min','no','ny','okc','orl','phi','phx','por','sac','sa','tor','utah','wsh'])),
    'NHL': dict(zip(
        ['Anaheim Ducks','Boston Bruins','Buffalo Sabres','Calgary Flames','Carolina Hurricanes','Chicago Blackhawks','Colorado Avalanche','Columbus Blue Jackets','Dallas Stars','Detroit Red Wings','Edmonton Oilers','Florida Panthers','Los Angeles Kings','Minnesota Wild','Montreal Canadiens','Nashville Predators','New Jersey Devils','New York Islanders','New York Rangers','Ottawa Senators','Philadelphia Flyers','Pittsburgh Penguins','San Jose Sharks','Seattle Kraken','St. Louis Blues','Tampa Bay Lightning','Toronto Maple Leafs','Utah Mammoth','Vancouver Canucks','Vegas Golden Knights','Washington Capitals','Winnipeg Jets'],
        ['ana','bos','buf','cgy','car','chi','col','cbj','dal','det','edm','fla','la','min','mtl','nsh','nj','nyi','nyr','ott','phi','pit','sj','sea','stl','tb','tor','utah','van','vgk','wsh','wpg'])),
    'MLB': dict(zip(
        ['Arizona Diamondbacks','Athletics','Atlanta Braves','Baltimore Orioles','Boston Red Sox','Chicago Cubs','Chicago White Sox','Cincinnati Reds','Cleveland Guardians','Colorado Rockies','Detroit Tigers','Houston Astros','Kansas City Royals','Los Angeles Angels','Los Angeles Dodgers','Miami Marlins','Milwaukee Brewers','Minnesota Twins','New York Mets','New York Yankees','Philadelphia Phillies','Pittsburgh Pirates','San Diego Padres','San Francisco Giants','Seattle Mariners','St. Louis Cardinals','Tampa Bay Rays','Texas Rangers','Toronto Blue Jays','Washington Nationals'],
        ['ari','ath','atl','bal','bos','chc','chw','cin','cle','col','det','hou','kc','laa','lad','mia','mil','min','nym','nyy','phi','pit','sd','sf','sea','stl','tb','tex','tor','wsh'])),
}


# Retrieve team images by team identity, never by the bet selection (which may be
# "Over 225.5" or "Under 6.5"). ESPN team metadata is cached for one day.
ESPN_LEAGUES = {
    'NFL': ('football', 'nfl'), 'NBA': ('basketball', 'nba'),
    'MLB': ('baseball', 'mlb'), 'College Football': ('football', 'college-football'),
    'NHL': ('hockey', 'nhl'), 'College Basketball': ('basketball', 'mens-college-basketball'),
}

@st.cache_data(ttl=86400, show_spinner=False)
def espn_team_logos(sport):
    if sport not in ESPN_LEAGUES:
        return {}
    category, league = ESPN_LEAGUES[sport]
    url = f'https://site.api.espn.com/apis/site/v2/sports/{category}/{league}/teams?limit=1000'
    try:
        req = Request(url, headers={'User-Agent': 'SportsEdgeDashboard/1.0'})
        with urlopen(req, timeout=8) as response:
            payload = json.load(response)
        entries = payload.get('sports', [{}])[0].get('leagues', [{}])[0].get('teams', [])
        found = {}
        for entry in entries:
            team = entry.get('team', entry)
            logo = team.get('logos', [{}])
            logo_url = (logo[0].get('href') if logo else None) or team.get('logo')
            if logo_url and str(logo_url).startswith('https://'):
                for name in (team.get('displayName'), team.get('name'), team.get('shortDisplayName')):
                    if name:
                        found[_team_key(name)] = logo_url
        return found
    except Exception:
        return {}


def matchup_teams(r):
    matchup = clean_text(r.get('matchup'))
    for sep in (' @ ', ' vs. ', ' vs ', ' at '):
        if sep in matchup:
            return [part.strip() for part in matchup.split(sep, 1)]
    return []


def team_logo(sport, team):
    live = espn_team_logos(sport)
    direct = live.get(_team_key(team))
    if direct:
        return direct
    # Exact full-team fallback for known professional teams.
    abbr = TEAM_LOGOS.get(sport, {}).get(team)
    if abbr:
        league = {'NHL': 'nhl', 'NBA': 'nba', 'MLB': 'mlb'}.get(sport)
        if league:
            return f'https://a.espncdn.com/i/teamlogos/{league}/500/{abbr}.png'
    return ''


def card_image_html(r):
    sport = clean_text(r.get('sport'))
    selection = clean_text(r.get('selection'))
    teams = matchup_teams(r)
    # Totals apply to both teams; show two team logos rather than a blank tile.
    is_total = bool(re.match(r'^\s*(over|under)\b', selection, re.I)) or clean_text(r.get('market')).lower() in ('total', 'totals')
    if is_total and len(teams) == 2:
        images = [team_logo(sport, team) for team in teams]
        imgs = ''.join(
            (f'<img class="pick-image" src="{esc(url)}" alt="{esc(team)} logo" loading="lazy">'
             if url else f'<div class="pick-image pick-image-fallback" title="{esc(team)} logo unavailable">🏟️</div>')
            for team, url in zip(teams, images))
        if imgs:
            return f'<div class="pick-image-pair">{imgs}</div>'
    for team in teams:
        if _team_key(team) in _team_key(selection):
            url = team_logo(sport, team)
            if url:
                return f'<img class="pick-image" src="{esc(url)}" alt="{esc(team)} logo" loading="lazy">'
    saved = pick_image(r.get('notes'))
    if saved:
        return f'<img class="pick-image" src="{esc(saved)}" alt="Team image" loading="lazy">'
    if teams:
        url = team_logo(sport, teams[0])
        if url:
            return f'<img class="pick-image" src="{esc(url)}" alt="{esc(teams[0])} logo" loading="lazy">'
    sport_icon = {'NFL':'🏈','NBA':'🏀','MLB':'⚾','NHL':'🏒','College Football':'🏈','College Basketball':'🏀'}.get(sport, '🏟️')
    return f'<div class="pick-image pick-image-fallback" role="img" aria-label="Team logo unavailable">{sport_icon}</div>'


def recorded_bookmaker(source):
    """Extract a bookmaker name, not the entire technical odds-source field."""
    source = clean_text(source)
    if not source:
        return ''
    if '/' in source:
        tail = source.rsplit('/', 1)[-1].strip()
        if tail and not tail.startswith(('http:', 'https:')):
            return tail
    if source.lower().startswith(('auto:', 'the odds api', 'http')):
        return ''
    return source if len(source) <= 35 else ''


# Live market quotes are display-only. They do not modify the saved pick or its edge.
ODDS_SPORT_KEYS = {
    'NFL': 'americanfootball_nfl', 'NBA': 'basketball_nba',
    'MLB': 'baseball_mlb', 'College Football': 'americanfootball_ncaaf',
    'NHL': 'icehockey_nhl', 'College Basketball': 'basketball_ncaab',
}
BOOK_KEYS = {'draftkings': 'draftkings', 'fanduel': 'fanduel',
             'betonline.ag': 'betonlineag', 'betrivers': 'betrivers',
             'betmgm': 'betmgm', 'caesars': 'williamhill_us'}


def _team_key(name):
    return re.sub(r'[^a-z0-9]', '', str(name).casefold())


@st.cache_data(ttl=600, show_spinner=False)
def market_events(sport_key, api_key):
    """Fetch current spread/total markets. Returns events and an explicit status."""
    params = urlencode({'apiKey': api_key, 'regions': 'us',
                        'markets': 'spreads,totals', 'oddsFormat': 'american',
                        'dateFormat': 'iso'})
    url = f'https://api.the-odds-api.com/v4/sports/{sport_key}/odds/?{params}'
    try:
        with urlopen(Request(url, headers={'User-Agent': 'SportsEdgeDashboard/1.1'}), timeout=10) as response:
            payload = json.load(response)
        if not isinstance(payload, list):
            return [], 'Odds API returned an unexpected response'
        return payload, ''
    except Exception as exc:
        # Do not reveal the URL: it contains the private API key.
        return [], f'Odds API request failed ({type(exc).__name__})'


@st.cache_resource(show_spinner=False)
def _market_quote_memory():
    """Last successful quotes shared across Streamlit reruns in this server process.

    These are display-only snapshots, not a durable odds archive. A Streamlit
    restart/redeploy clears this memory; durable storage requires a DB table.
    """
    return {'lock': RLock(), 'quotes': {}}


def _remember_market_quotes(cache_key, quotes):
    memory = _market_quote_memory()
    now = datetime.now(timezone.utc).isoformat()
    with memory['lock']:
        prior = memory['quotes'].get(cache_key, {})
        merged = dict(prior)
        for market in ('spread', 'total'):
            if quotes.get(market):
                merged[market] = quotes[market]
                merged[f'{market}_book'] = quotes.get(f'{market}_book', '')
                merged[f'{market}_captured_at'] = now
        memory['quotes'][cache_key] = merged
    return merged


def _market_quote_fallback(cache_key, reason):
    memory = _market_quote_memory()
    with memory['lock']:
        saved = dict(memory['quotes'].get(cache_key, {}))
    if not saved:
        return {'status': reason}
    times = [saved.get(f'{m}_captured_at') for m in ('spread', 'total') if saved.get(m)]
    captured = min(times) if times else None
    try:
        when = datetime.fromisoformat(captured).astimezone(PT).strftime('%b %d, %I:%M %p PT')
    except (TypeError, ValueError):
        when = 'an earlier session'
    saved['status'] = f'STALE saved quote (captured {when}); not live. {reason}'
    return saved


def _format_outcome(outcome, include_team=False):
    try:
        point = float(outcome['point'])
        price = int(outcome['price'])
    except (KeyError, TypeError, ValueError):
        return ''
    prefix = str(outcome.get('name', '')).strip() if include_team else ''
    point_text = f'{point:+g}' if include_team else f'{point:g}'
    return f'{prefix} {point_text} ({price:+d})'.strip()


def _market_from_book(book, market_key):
    return next((m for m in (book.get('markets') or []) if m.get('key') == market_key), None)


def current_market_lines(r):
    """Return spread and total quotes independently, even from different books.

    Current-odds API only lists offered games. Never substitute lines from
    another matchup or present old prices as live.
    """
    sport = clean_text(r.get('sport'))
    key = ODDS_SPORT_KEYS.get(sport)
    if not key:
        return {'status': 'Sport not supported by the current odds feed'}
    api_key = st.secrets.get('ODDS_API_KEY', '')
    if not api_key:
        return {'status': 'ODDS_API_KEY missing in Streamlit secrets'}

    matchup = clean_text(r.get('matchup'))
    pieces = re.split(r'\s+@\s+|\s+vs\.?\s+', matchup, maxsplit=1, flags=re.I)
    if len(pieces) != 2:
        return {'status': 'Could not identify the teams in this matchup'}
    away, home = map(_team_key, pieces)
    day = clean_text(r.get('date'))[:10]
    cache_key = (key, away, home, day)
    events, error = market_events(key, api_key)
    if error:
        return _market_quote_fallback(cache_key, error)

    matching = []
    for event in events:
        if _team_key(event.get('away_team')) != away or _team_key(event.get('home_team')) != home:
            continue
        try:
            event_day = datetime.fromisoformat(event['commence_time'].replace('Z', '+00:00')).astimezone(PT).date().isoformat()
        except (KeyError, TypeError, ValueError):
            continue
        if event_day == day:
            matching.append(event)
    if not matching:
        return _market_quote_fallback(cache_key, 'Game not listed in current odds feed')

    preferred = BOOK_KEYS.get(recorded_bookmaker(r.get('source')).casefold())
    for event in matching:
        books = event.get('bookmakers') or []
        books = sorted(books, key=lambda b: b.get('key') != preferred)
        result = {'spread': '', 'total': '', 'spread_book': '', 'total_book': ''}
        for market_key, result_key in (('spreads', 'spread'), ('totals', 'total')):
            for book in books:
                market = _market_from_book(book, market_key)
                if not market:
                    continue
                outcomes = market.get('outcomes') or []
                if market_key == 'spreads':
                    selected = []
                    for team in (event.get('home_team'), event.get('away_team')):
                        outcome = next((o for o in outcomes if _team_key(o.get('name')) == _team_key(team)), None)
                        if outcome:
                            value = _format_outcome(outcome, include_team=True)
                            if value:
                                selected.append(value)
                    formatted = ' · '.join(selected)
                else:
                    selected = []
                    for side in ('Over', 'Under'):
                        outcome = next((o for o in outcomes if str(o.get('name', '')).casefold() == side.casefold()), None)
                        if outcome:
                            value = _format_outcome(outcome)
                            if value:
                                selected.append(f'{side} {value}')
                    formatted = ' · '.join(selected)
                if formatted:
                    result[result_key] = formatted
                    result[result_key + '_book'] = book.get('title') or book.get('key') or 'Sportsbook'
                    break
        if result['spread'] or result['total']:
            saved = _remember_market_quotes(cache_key, result)
            # If one market is absent in the fresh response, retain it only as
            # explicitly stale rather than presenting a mixed snapshot as live.
            missing = [m for m in ('spread', 'total') if not result.get(m) and saved.get(m)]
            if missing:
                result.update({m: saved[m] for m in missing})
                result.update({f'{m}_book': saved.get(f'{m}_book', '') for m in missing})
                result['status'] = ('STALE saved ' + ', '.join(missing) +
                                    ' quote(s); other quoted markets are current. Prices may change.')
            else:
                result['status'] = 'Latest available API snapshot; prices can change'
            return result
    return _market_quote_fallback(cache_key, 'Matching game found, but spread and total markets are not offered')


def market_lines_html(r):
    quotes = current_market_lines(r)
    spread = esc(quotes.get('spread') or 'Unavailable')
    total = esc(quotes.get('total') or 'Unavailable')
    spread_book = quotes.get('spread_book')
    total_book = quotes.get('total_book')
    if spread_book:
        spread += f' <span class="pick-market-muted">· {esc(spread_book)}</span>'
    if total_book:
        total += f' <span class="pick-market-muted">· {esc(total_book)}</span>'
    status = quotes.get('status') or 'Latest available API snapshot; prices can change'
    return (f'<div class="pick-market-lines"><strong>Spread:</strong> {spread}'
            f'<br><strong>Total O/U:</strong> {total}'
            f'<br><span class="pick-market-muted">{esc(status)}</span></div>')


def matchup_preview(rationale):
    """Summarize facts already stored in the research notes; no invented news."""
    items = [line.strip().lstrip('•- ').strip() for line in rationale.splitlines() if line.strip()]
    facts = []
    for item in items:
        low = item.lower()
        if any(x in low for x in ('overall records:', 'last 10 completed', 'last-10 vs last-10', 'last 10 games', 'last-10 record', 'home record', 'away record')):
            facts.append(item.rstrip('.'))
        if len(facts) >= 2:
            break
    if not facts:
        for item in items:
            low = item.lower()
            if any(x in low for x in ('scored', 'allowed', 'record', 'averag', 'last-10', 'last 10', 'home', 'away')) and not any(x in low for x in ('uncalibrated', 'model', 'probability')):
                facts.append(item.rstrip('.'))
            if len(facts) >= 2:
                break
    if not facts:
        return 'Matchup-specific form and scoring data have not been recorded for this selection.'
    return '. '.join(facts) + '.'


def full_pick_card(r):
    odds=number(r.get('odds'),None)
    prob=number(r.get('estimated_prob'),None)
    be=odds_break_even(odds) if odds is not None else None
    edge=prob*100-be if prob is not None and 0<prob<1 and be is not None else None
    rank=int(r['rank']) if pd.notna(r.get('rank')) else '—'
    label=esc(r.get('selection'))
    price=f' ({odds:+.0f})' if odds is not None else ''
    rationale,warnings=pick_analysis(r)
    if not rationale: rationale='No independently verified matchup analysis has been recorded for this pick.'
    if not warnings: warnings='Injuries, suspensions, lineups and game-time weather/wind have not been verified. For indoor games, weather is generally irrelevant.'
    image_html = card_image_html(r)
    market_line = market_lines_html(r)
    preview = esc(matchup_preview(rationale))
    description = f'<div class="pick-preview"><span class="pick-preview-label">Matchup preview:</span> {preview}</div>'
    suggested_units = number(r.get("stake"), 1.0)
    units_html = f'<div class="pick-odds-line"><strong>Suggested Units: {suggested_units:g}u</strong></div>' 
    return f"""<div class="pick-card">
      <div class="pick-rank">{rank}</div>
      <div class="pick-image-cell">{image_html}</div>
      <div><div class="pick-name">{label}{price}</div><div class="pick-meta">{esc(r.get('sport'))} · {esc(r.get('matchup'))} · {esc(r.get('start_pt')) or 'Start time unverified'}</div>{market_line}{units_html}<div class="pick-desc">{description}</div></div>
      <div><div class="pick-kpi-label">Break-even<br>probability</div><div class="pick-kpi-value">{f'{be:.1f}%' if be is not None else '—'}</div></div>
      <div><div class="pick-kpi-label">Model win<br>probability</div><div class="pick-kpi-value">{f'{prob*100:.1f}%' if prob is not None and 0<prob<1 else '—'}</div></div>
      <div><div class="pick-kpi-label">Model edge</div><div class="pick-kpi-value {('positive' if edge>=0 else 'negative') if edge is not None else ''}">{f'{edge:+.1f} pp' if edge is not None else '—'}</div></div>
      <div class="analysis-cell"><div class="pick-col-title">Why this pick?</div><div class="pick-bullets">{html_lines(rationale)}</div></div>
      <div class="watch-cell"><div class="pick-col-title">Watch out for</div><div class="pick-bullets">{html_lines(warnings)}</div></div>
    </div>"""


if page == 'Dashboard':
    summary=stats(df)
    net=number(summary.get('net'))
    roi=summary.get('roi')
    settled=int(df['result'].isin(['Win','Loss','Push','Void']).sum())
    pending=int(df['result'].eq('Pending').sum())
    metrics=[('W–L–P',format_record(summary),''),('Net units',signed_units(net),signed_class(net)),('ROI',f'{roi:+.1f}%' if roi is not None else '—',signed_class(roi)),('Settled',str(settled),''),('Pending',str(pending),'')]
    metrics_html=''.join(f'<div class="metric-box"><div class="metric-label">{esc(label)}</div><div class="metric-value {css}">{esc(value)}</div></div>' for label,value,css in metrics)
    st.markdown(f'<section class="dashboard-section"><div class="section-title">Running Record</div><div class="section-sub">Since October 28, 2026 · hypothetical picks · ungraded picks excluded from W–L–P</div><div class="metrics-grid">{metrics_html}</div></section>',unsafe_allow_html=True)
    hist=None  # Old aggregate baseline excluded from the October 28 fresh start.
    if hist:
        st.caption(f"Historical baseline through {hist['as_of']}: {hist['wins']}–{hist['losses']}–{hist['pushes']}, {hist['net']:+.2f}u (separate from pick-level totals; not double counted).")

    yesterday=TODAY-timedelta(days=1)
    y=df[df['date'].astype(str)==yesterday.isoformat()]
    ys=stats(y)
    yn=number(ys.get('net'))
    recap=(f'{format_record(ys)} &nbsp; | &nbsp; {signed_units(yn)}' if not y.empty else 'No picks recorded')
    recap_detail=('Some results are awaiting verification.' if y['result'].eq('Pending').any() else f'{len(y)} recorded picks; see history below.') if not y.empty else 'No recorded picks for yesterday.'
    if yesterday.isoformat() < RECORD_START_DATE:
        recap = 'New record starts today'
        recap_detail = 'Earlier selections are archived outside the new running record.'
    recap_items = ''
    for _, item in y.sort_values('rank', na_position='last').iterrows():
        outcome = clean_text(item.get('result')) or 'Pending'
        pl = profit(item)
        units = number(item.get('stake'), 1.0)
        pl_text = signed_units(pl) if pl is not None else 'Awaiting result'
        css = signed_class(pl) if pl is not None else ''
        odds_value = number(item.get('odds'), None)
        odds_text = f'{odds_value:+.0f}' if odds_value is not None else 'Odds unavailable'
        recap_items += (f'<div class="recap-item"><div><strong>{esc(item.get("selection"))}</strong>'
                        f'<div class="recap-item-detail">{esc(item.get("sport"))} · {esc(odds_text)} · {units:g}u risked</div></div>'
                        f'<div class="recap-item-result {css}">{esc(outcome.upper())} · {esc(pl_text)}</div></div>')
    st.markdown(f'<section class="dashboard-section"><div class="recap-flex"><div><div class="section-title">Yesterday’s Recap</div><div class="section-sub">{yesterday:%A, %B %d, %Y}</div><div class="section-sub">{esc(recap_detail)}</div></div><div class="recap-result {signed_class(yn)}">{recap}</div></div><div class="recap-list">{recap_items}</div></section>',unsafe_allow_html=True)

    sport_html=''
    for name,emoji in zip(SPORTS,SPORT_ICONS):
        q=df[df['sport']==name]
        sq=stats(q)
        n=number(sq.get('net'))
        sport_html+=f'<div class="sport-box"><div class="sport-name">{emoji} {esc(name)}</div><div class="sport-record">{esc(format_record(sq))} <span class="sport-profit {signed_class(n)}">{signed_units(n)}</span></div></div>'
    st.markdown(f'<section class="dashboard-section"><div class="section-title">Record by Sport</div><div class="sports-grid">{sport_html}</div></section>',unsafe_allow_html=True)

    today_df=df[df['date'].astype(str)==TODAY.isoformat()].sort_values('rank',na_position='last')
    ranked=today_df[today_df['rank'].notna()].head(5)
    if ranked.empty:
        picks_html='<div class="section-sub" style="padding:20px 5px">No verified Top 5 has been published today. The automated publisher still needs independently modeled probabilities and verified game eligibility.</div>'
    else:
        picks_html=''.join(full_pick_card(pick) for _,pick in ranked.iterrows())
        if len(ranked)!=5 or sorted(ranked['rank'].astype(int).tolist())!=[1,2,3,4,5]:
            picks_html='<div class="section-sub">Today’s card is incomplete; only verified recorded picks are shown.</div>'+picks_html
    st.markdown(f'<section class="dashboard-section"><div class="section-title" style="font-size:1.35rem">Today’s Top 5</div><div class="section-sub">Ranked recorded selections · Pacific Time · model estimates are experimental and uncalibrated</div>{picks_html}</section>',unsafe_allow_html=True)

    table_rows=''
    for _,r in ranked.iterrows():
        odds=number(r.get('odds'),None)
        prob=number(r.get('estimated_prob'),None)
        be=odds_break_even(odds) if odds is not None else None
        edge=prob*100-be if prob is not None and 0<prob<1 and be is not None else None
        edge_text=f'{edge:+.1f} pp' if edge is not None else '—'
        table_rows+=f'<tr><td>{int(r["rank"])}</td><td>{esc(r.get("selection"))}</td><td>{odds:+.0f}</td><td>{f"{be:.1f}%" if be is not None else "—"}</td><td>{f"{prob*100:.1f}%" if prob is not None and 0<prob<1 else "—"}</td><td class="{("positive" if edge>=0 else "negative") if edge is not None else ""}">{edge_text}</td></tr>'
    if not table_rows: table_rows='<tr><td colspan="6">No verified selections available for today.</td></tr>'
    st.markdown(f'<section class="dashboard-section"><div class="section-title">Final Card</div><table class="final-table"><thead><tr><th>Rank</th><th>Pick</th><th>Odds</th><th>Break-even</th><th>Estimated probability</th><th>Estimated edge</th></tr></thead><tbody>{table_rows}</tbody></table></section>',unsafe_allow_html=True)

    header('Performance Trends')
    filtered=df.copy()
    with st.container(border=True):
        header('Bet Status & Performance')
        left,right=st.columns([1,2])
        counts=filtered['result'].value_counts().reindex(['Win','Loss','Push','Void','Pending'],fill_value=0)
        with left:
            st.markdown('**Bet Status Distribution**')
            if counts.sum():
                import altair as alt
                chartdata=pd.DataFrame({'Result':counts.index,'Picks':counts.values})
                donut=alt.Chart(chartdata).mark_arc(innerRadius=65,outerRadius=108).encode(theta='Picks:Q',color=alt.Color('Result:N',scale=alt.Scale(domain=['Win','Loss','Push','Void','Pending'],range=['#13c98b','#ef5261','#388bff','#8595aa','#f7b733'])),tooltip=['Result','Picks']).properties(height=265)
                st.altair_chart(donut,use_container_width=True)
            else: st.info('No picks in this filter range.')
        with right:
            st.markdown('**Cumulative Profit / Loss (units)**')
            done=filtered[filtered['result'].isin(['Win','Loss','Push','Void'])].copy()
            if not done.empty:
                done['pl']=done.apply(profit,axis=1)
                daily=done.groupby('date',as_index=False)['pl'].sum().sort_values('date')
                daily['Cumulative P/L']=daily['pl'].cumsum()
                daily=daily.rename(columns={'pl':'Daily P/L','date':'Date'})
                st.line_chart(daily.set_index('Date')[['Daily P/L','Cumulative P/L']],height=265)
            else: st.info('Charts will populate after you grade recorded picks.')
    with st.container(border=True):
        header('Cumulative ROI')
        done=filtered[filtered['result'].isin(['Win','Loss','Push','Void'])].copy()
        if not done.empty:
            done['pl']=done.apply(profit,axis=1)
            done['stake_num']=pd.to_numeric(done['stake'],errors='coerce').fillna(0).where(done['result'].isin(['Win','Loss','Push']), 0)
            byday=done.groupby('date',as_index=False)[['pl','stake_num']].sum().sort_values('date')
            byday['cumulative_roi']=100*byday['pl'].cumsum()/byday['stake_num'].cumsum().replace(0,float('nan'))
            st.line_chart(byday.set_index('date')['cumulative_roi'],height=230,y_label='ROI (%)')
        else: st.info('No settled picks to chart.')

    with st.container(border=True):
        header('Filters & Pick History','Filters affect only the history below.')
        a,b,c,d=st.columns([2,1.25,1.25,1.25])
        date_range=a.date_input('Date range',value=(TODAY-timedelta(days=30),TODAY),key='filter_dates')
        sport=b.selectbox('Sport',['All']+SPORTS)
        market=c.selectbox('Market',['All','Moneyline','Spread','Total','Player prop','Other'])
        status=d.selectbox('Status',['All','Pending','Win','Loss','Push','Void'])
        historical=df.copy()
        if isinstance(date_range,(list,tuple)) and len(date_range)==2:
            historical=historical[(historical['date'].astype(str)>=date_range[0].isoformat())&(historical['date'].astype(str)<=date_range[1].isoformat())]
        if sport!='All': historical=historical[historical['sport']==sport]
        if market!='All': historical=historical[historical['market']==market]
        if status!='All': historical=historical[historical['result']==status]
        st.dataframe(make_table(historical),hide_index=True,use_container_width=True)

elif page=='Top 5 card':
    header('Daily Top 5 Card')
    day=st.date_input('Card date',TODAY).isoformat()
    card=df[df['date'].astype(str)==day].sort_values('rank',na_position='last')
    if card.empty: st.info('No picks for this day. Add verified, sourced picks first.')
    else:
        if len(card)!=5 or sorted(card['rank'].dropna().astype(int).tolist())!=[1,2,3,4,5]:
            st.warning('A complete card needs five unique ranks from 1 to 5.')
        for _, pick in card.head(5).iterrows():
            st.markdown(full_pick_card(pick), unsafe_allow_html=True)
        with st.container(border=True):
            header('Final Card')
            st.dataframe(final_card_table(card.head(5)), hide_index=True, use_container_width=True)


elif page=='Add pick':
    header('Add a Sourced Pick')
    with st.form('new_pick'):
        a,b,c=st.columns(3)
        day=a.date_input('Date',TODAY);sport=b.selectbox('Sport',SPORTS);rank=c.selectbox('Top 5 rank (optional)',['None',1,2,3,4,5])
        matchup=st.text_input('Matchup');selection=st.text_input('Selection')
        a,b,c=st.columns(3)
        market=a.selectbox('Market',['Moneyline','Spread','Total','Player prop','Other'])
        odds=b.number_input('American odds',value=-110,step=5)
        stake=c.number_input('Units risked',min_value=0.01,value=1.0,step=0.25)
        a,b=st.columns(2)
        start=a.text_input('Start time PT (from source)')
        prob=b.number_input('Estimated win probability % (0 = unknown)',min_value=0.0,max_value=100.0,value=0.0)
        source=st.text_input('Odds/stat source URL')
        notes=st.text_area('Recent form, injuries, rationale and verification notes')
        submitted=st.form_submit_button('Save pick',type='primary')
        if submitted:
            if not matchup.strip() or not selection.strip() or -100<int(odds)<100:
                st.error('Enter a matchup, selection, and valid American odds (≤ -100 or ≥ +100).')
            elif rank!='None' and ((df['date'].astype(str)==day.isoformat()) & (df['rank']==rank)).any():
                st.error('That rank is already used for this date and ledger.')
            else:
                add(dict(date=day.isoformat(),sport=sport,matchup=matchup,market=market,selection=selection,odds=int(odds),stake=stake,kind=kind,result='Pending',source=source,result_source='',notes=notes,rank=None if rank=='None' else rank,start_pt=start,estimated_prob=prob/100 if prob else None))
                st.success('Saved to Supabase.');st.rerun()

elif page=='Grade results':
    header('Grade Verified Outcomes')
    pending=df[df['result']=='Pending']
    if pending.empty: st.info('No pending picks in this ledger.')
    else:
        ids=pending['id'].tolist()
        pid=st.selectbox('Pick',ids,format_func=lambda x:f"#{x} · {pending[pending.id==x].iloc[0]['date']} · {pending[pending.id==x].iloc[0]['selection']}")
        result=st.selectbox('Verified result',['Win','Loss','Push','Void'])
        url=st.text_input('Result verification URL')
        if st.button('Settle',type='primary'):
            if not url.strip(): st.error('Enter a verification URL; otherwise leave the pick pending.')
            else: grade(pid,result,url);st.success('Settled.');st.rerun()

elif page=='Import / backup':
    header('Import / Backup')
    st.write('Required CSV columns: date,sport,matchup,market,selection,odds,stake,kind,result. Optional: source,result_source,notes,rank,start_pt,estimated_prob (0–1).')
    st.warning('Imports append rows without deduplication. Back up first and avoid importing picks represented in the aggregate baseline.')
    upload=st.file_uploader('Pick ledger CSV',type='csv')
    if upload:
        try:
            incoming=pd.read_csv(upload,keep_default_na=False)
            st.dataframe(incoming.head(10),hide_index=True)
            if st.button('Import rows'): n=import_rows(incoming);st.success(f'Imported {n} rows.');st.rerun()
        except Exception as exc: st.error(str(exc))
    st.download_button('Export all picks CSV',all_df.to_csv(index=False),'picks_backup.csv','text/csv')
    st.divider();header('Optional Historical Aggregate Baseline')
    st.caption('Use only if individual historical picks are unavailable. This cannot reconstruct ROI or individual results.')
    old=baseline()
    with st.form('baseline_form'):
        asof=st.text_input('As-of date (YYYY-MM-DD)',value=old['as_of'] if old else '')
        a,b,c=st.columns(3)
        w=a.number_input('Wins',min_value=0,value=old['wins'] if old else 0)
        l=b.number_input('Losses',min_value=0,value=old['losses'] if old else 0)
        p=c.number_input('Pushes',min_value=0,value=old['pushes'] if old else 0)
        net=st.number_input('Net units',value=float(old['net']) if old else 0.0,format='%.2f')
        risked=st.text_input('Total units risked (blank if unknown)',value=str(old['risked']) if old and old['risked'] is not None else '')
        note=st.text_area('Provenance / uncertainty',value=old['note'] if old else '')
        if st.form_submit_button('Save baseline'):
            try:
                if pd.isna(pd.to_datetime(asof,errors='coerce')): raise ValueError('Enter a valid as-of date.')
                risk=float(risked) if risked.strip() else None
                if risk is not None and risk<=0: raise ValueError('Risk must be positive.')
                set_baseline(dict(as_of=asof,wins=w,losses=l,pushes=p,net=net,risked=risk,note=note))
                st.success('Baseline saved.');st.rerun()
            except Exception as exc: st.error(str(exc))

else:
    header('Research and Tracking Rules')
    st.markdown('''
- Rank five evidence-backed selections when five qualifying opportunities exist; never fabricate picks to fill the card.
- Priority: NFL, NBA, MLB, College Football, NHL, College Basketball. No preseason games.
- College Football and College Basketball picks require an AP Top 25 team; basketball is men's only internally.
- NHL player props have lower priority; other sports may include player props when the edge warrants it.
- Record sportsbook American odds, Pacific start times, recent ten-game form (or available sample), break-even percentage, estimated probability, model edge, injury context and source.
- Verify results before grading. Keep hypothetical picks and actual wagers separate.
- **Automation:** A scheduled publisher can read an odds feed, but picks require independently generated probabilities and verified event eligibility. Auto-grading of supported completed games is implemented through the scheduled settlement workflow; unresolved outcomes remain pending.
''')
