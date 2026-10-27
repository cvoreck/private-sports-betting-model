"""Sports Edge Analytics — Streamlit dashboard, backed by the existing Supabase ledger.
Replace sports_betting_cloud/app.py with this file; keep storage.py and analytics.py.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
from storage import init, add, all_picks, grade, baseline, set_baseline, import_rows
from analytics import stats, format_record

st.set_page_config(page_title="Today's Sports Betting Analytics", page_icon="📈", layout="wide", initial_sidebar_state="expanded")
PT = ZoneInfo("America/Los_Angeles")
TODAY = datetime.now(PT).date()
SPORTS = ['NFL', 'NBA', 'MLB', 'College Football', 'NHL', 'College Basketball']
SPORT_ICONS = ['🏈', '🏀', '⚾', '🏈', '🏒', '🏀']

st.markdown('''<style>
:root {color-scheme:dark}
.stApp {background: #0b1423; color: #e9f1fb}
[data-testid="stSidebar"] {background: #101e31; border-right: 1px solid #253750}
.block-container {max-width: 1540px; padding-top: 1.45rem; padding-bottom: 3rem}
h1,h2,h3 {letter-spacing:-.03em}
h1 {color:#f5f9ff !important; font-size:2.15rem !important; margin-bottom:.15rem !important}
h3 {font-size:1.13rem !important}
[data-testid="stMetric"] {background:#17263a; border:1px solid #293b52; border-radius:11px; padding:17px 16px; min-height:110px}
[data-testid="stMetricLabel"] {color:#9fb0c6; font-size:.87rem}
[data-testid="stMetricValue"] {color:#f5f8ff; font-weight:800; font-size:1.75rem}
[data-testid="stMetricDelta"] {font-size:.84rem}
[data-testid="stVerticalBlockBorderWrapper"] {border-color:#2a3a50 !important; background:#111e30; border-radius:13px}
[data-testid="stDataFrame"] {border:1px solid #2b4058; border-radius:9px}
div[data-testid="stSelectbox"]>div>div, div[data-testid="stDateInput"]>div>div {background:#17263a}
hr {border-color:#2c3d55}
div.stButton > button[kind="primary"] {background:#1767d2; border:0}
.sportcard {border:1px solid #2e445d; background:#16263a; border-radius:11px; padding:17px 15px; min-height:160px}
.sportcard .name {font-size:1rem; font-weight:800; margin-bottom:14px}
.sportcard .detail {color:#a6b7cb; font-size:.85rem; line-height:1.9}
.sportcard strong {color:#f1f6ff; float:right}
.subtle {color:#9eb1c7; font-size:.9rem}
</style>''', unsafe_allow_html=True)

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

st.sidebar.markdown('## 📈 Sports Edge')
page = st.sidebar.radio('Navigation', ['Dashboard','Top 5 card','Add pick','Grade results','Import / backup','Rules'])
kind = st.sidebar.radio('Ledger', ['Hypothetical','Actual'], help='Keep model recommendations separate from bets you actually placed.')
df = all_df[all_df['kind'].eq(kind)].copy()


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
        rows.append({'#':int(r['rank']) if pd.notna(r.get('rank')) else '—',
                     'Sport':r.get('sport',''), 'Matchup':r.get('matchup',''),
                     'Pick':r.get('selection',''), 'Odds':f"{int(r['odds']):+d}" if pd.notna(r.get('odds')) else '—',
