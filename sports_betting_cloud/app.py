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
from storage import init, add, all_picks, grade, baseline, set_baseline, import_rows, connect
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

/* Compact, scan-friendly pick cards */
.pick-card {display:block;border:1px solid #1b4b6c;border-radius:13px;background:linear-gradient(125deg,#092a44,#061e33);margin:14px 0;overflow:hidden}
.pick-card>div {padding:0;border:0}
.pick-top {display:flex;align-items:center;gap:15px;padding:17px 19px!important}
.pick-rank {font-size:1.05rem!important;min-width:29px;flex-shrink:0;color:#a8c3df!important}
.pick-image-cell {padding:0!important;flex:0 0 64px;align-items:center!important}
.pick-image {width:64px;height:64px;object-fit:contain;background:#12344d;border-radius:10px;padding:6px}
.pick-image-pair {flex-direction:row;gap:3px}.pick-image-pair .pick-image {width:31px;height:46px;padding:2px}
.pick-primary {min-width:0;flex:1}
.pick-name {font-size:1.14rem}.pick-meta {margin:5px 0 0;font-size:.82rem}
.pick-price {text-align:right;min-width:82px;flex-shrink:0}.pick-price strong {font-size:1.5rem;color:#f6faff;display:block}.pick-price span {font-size:.78rem;color:#a9c6df}
.pick-kpis {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;padding:14px 19px!important;border-top:1px solid #194462!important;border-bottom:1px solid #194462!important}
.pick-kpi-label {font-size:.77rem;color:#adc7e0;font-weight:600}.pick-kpi-value {font-size:1.48rem;margin-top:4px}
.pick-research {display:grid;grid-template-columns:1fr 1fr;gap:18px;padding:16px 19px!important}
.pick-research section {min-width:0}.pick-col-title {font-size:.92rem;margin-bottom:8px}
.pick-bullets {font-size:.84rem;line-height:1.55}.pick-bullets li {margin-bottom:7px}
.pick-details {border-top:1px solid #194462;padding:0 19px!important}
.pick-details summary {padding:13px 0;cursor:pointer;color:#b6d9f3;font-size:.83rem;font-weight:650}
.pick-details-content {padding:0 0 15px;color:#c5d8ec;font-size:.81rem;line-height:1.55}
.pick-details-content p {margin:6px 0}
.record-recap {border-top:1px solid #174768;margin-top:16px;padding-top:15px}
.recap-result {font-size:1.1rem}
@media(max-width:900px){.pick-card{display:block}.pick-card>div{border-right:0}.pick-research{grid-template-columns:1fr}.pick-top{gap:10px}.pick-price strong{font-size:1.2rem}}
@media(max-width:520px){.pick-top{padding:12px!important}.pick-image-cell{flex-basis:44px}.pick-image{width:44px;height:48px}.pick-name{font-size:.95rem}.pick-meta{font-size:.7rem}.pick-price{min-width:64px}.pick-kpis{padding:12px!important;gap:5px}.pick-kpi-label{font-size:.69rem}.pick-kpi-value{font-size:1.13rem}.pick-research{padding:13px!important}.pick-rank{min-width:18px}}

/* Screenshot-matched Sports Edge dashboard */
.block-container {max-width:1480px!important;padding:1.05rem 1.5rem 3rem!important}
.se-brand {display:flex;align-items:center;gap:17px;margin:0 0 18px 5px}
.se-mark {display:flex;align-items:flex-end;gap:5px;height:40px}
.se-mark i {display:block;width:10px;background:linear-gradient(#11c9ff,#00dbaf);border-radius:3px 3px 0 0}
.se-mark i:nth-child(1){height:14px}.se-mark i:nth-child(2){height:28px}.se-mark i:nth-child(3){height:39px}
.se-brand h1 {font-size:2rem!important;line-height:1.05!important;margin:0!important}
.se-brand p {font-size:1rem;color:#d6e1ef;margin:4px 0 0}
.se-nav {display:flex;gap:12px;margin-left:auto;align-items:center}
.se-nav span {padding:9px 18px;border-radius:24px;font-size:.93rem;color:#f2f7ff}
.se-nav span:first-child {border:1px solid #7296b4;background:#1a3a56;font-weight:750}
.se-board {display:grid;grid-template-columns:1fr 1.7fr 1.2fr;gap:22px;border:1px solid #326384;border-radius:16px;padding:17px 24px;background:#031829;margin-bottom:18px}
.se-board-col {min-width:0}.se-board-col+.se-board-col {border-left:1px solid #356184;padding-left:22px}
.se-board h3 {font-size:1rem!important;margin:0 0 8px!important}
.se-big-record {font-size:2.45rem;font-weight:850;letter-spacing:-.04em;line-height:1.1;margin:9px 0}
.se-net {font-size:1.04rem;color:#d9e4f3}.se-red {color:#ff5266;font-weight:800}.se-green {color:#00e9a4;font-weight:800}
.se-recap-heading {display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.se-recap-heading h3 {font-size:.92rem!important;margin:0!important}
.se-recap-pill {border:1px solid #c2384b;border-radius:20px;padding:2px 9px;color:#ff576b;font-weight:750;font-size:.83rem}
.se-recap-list {margin-top:8px;display:grid;gap:4px}.se-recap-row {display:grid;grid-template-columns:24px minmax(0,1fr) 24px 75px;gap:7px;font-size:.88rem;align-items:center}.se-recap-row .se-result {font-weight:800}
.se-sports {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:9px}.se-sport {border:1px solid #245578;border-radius:11px;text-align:center;padding:7px 3px;background:#082238;font-size:.86rem}.se-sport strong {display:block;font-size:1.02rem;font-weight:500;margin-top:2px}
.se-section-head {margin:14px 0 16px 15px}.se-section-head h2 {font-size:2rem!important;margin:0!important}.se-section-head p {margin:3px 0;color:#dce6f1}
.se-pick {border:1px solid #315e7d;border-radius:14px;background:linear-gradient(110deg,#062239,#041c2e);margin:9px 0;overflow:hidden}
.se-pick.se-first {border:2px solid #20e58b;box-shadow:0 0 0 1px #064d44}
.se-pick summary {list-style:none;cursor:pointer;display:block}.se-pick summary::-webkit-details-marker {display:none}
.se-pick-head {display:grid;grid-template-columns:48px 80px minmax(190px,2.2fr) minmax(75px,.75fr) minmax(75px,.75fr) minmax(78px,.8fr) 96px 22px;align-items:center;gap:12px;padding:14px 18px}
.se-rank {border:1px solid #15df85;background:#073c34;border-radius:9px;text-align:center;font-size:1.4rem;font-weight:800;padding:6px 0;color:white}
.se-pick-logo .pick-image {width:74px;height:67px;background:transparent;padding:0;object-fit:contain}.se-pick-logo .pick-image-pair {display:flex;flex-direction:row}.se-pick-logo .pick-image-pair .pick-image {width:36px;height:54px}
.se-pick-name {font-size:1.11rem;font-weight:800;line-height:1.3}.se-pick-match {font-size:.97rem;font-weight:650;margin:2px 0}.se-pick-meta {font-size:.8rem;color:#d1e0f0}
.se-kpi {border-left:1px solid #37607d;padding-left:12px;min-width:0}.se-kpi small {display:block;font-size:.77rem;color:#d4e2f2;white-space:nowrap}.se-kpi strong {display:block;font-size:1.45rem;white-space:nowrap;margin-top:4px}
.se-odds {text-align:center}.se-odds strong {display:block;border:1px solid #376d90;border-radius:9px;padding:5px 7px;font-size:1.3rem;background:#102e45}.se-odds span {display:block;border:1px solid #078f56;background:#07412f;border-radius:9px;padding:4px;margin-top:6px;font-size:.87rem;color:#61f2a4;font-weight:800}
.se-chevron {font-size:1.3rem;text-align:center}.se-chevron:before {content:'⌄'}.se-pick[open] .se-chevron:before {content:'⌃'}
.se-pick-expanded {padding:0 18px 14px}.se-research {display:grid;grid-template-columns:1fr 1fr;gap:14px}.se-research-panel {border:1px solid #326284;border-radius:11px;padding:10px 16px;min-width:0}.se-research-panel h3 {font-size:1.1rem!important;margin:0 0 7px!important}.se-research-panel ul {margin:0;padding-left:22px;color:#e4edf8;font-size:.84rem;line-height:1.5}.se-research-panel li {margin-bottom:5px}
.se-detail-strip {display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;border:1px solid #315b7d;border-radius:9px;padding:9px 14px;margin-top:10px;font-size:.79rem;color:#d8e8f5}.se-detail-strip strong {color:white}
.se-disclaimer {font-size:.74rem;color:#a7bfd3;margin:7px 2px 0}
@media(max-width:1120px){.se-board{grid-template-columns:1fr 1.3fr}.se-board-col:last-child{grid-column:1/-1;border-left:0;border-top:1px solid #356184;padding:12px 0 0}.se-pick-head{grid-template-columns:40px 58px minmax(170px,1.5fr) repeat(3,minmax(68px,.7fr)) 78px 18px;gap:7px;padding:12px}.se-pick-logo .pick-image{width:56px;height:54px}.se-kpi strong{font-size:1.1rem}.se-kpi small{font-size:.65rem}}
@media(max-width:760px){.block-container{padding:1rem .65rem 2rem!important}.se-brand{flex-wrap:wrap}.se-nav{display:none}.se-board{grid-template-columns:1fr;padding:13px;gap:12px}.se-board-col+.se-board-col{border-left:0;border-top:1px solid #356184;padding:12px 0 0}.se-board-col:last-child{grid-column:auto}.se-pick-head{grid-template-columns:35px 46px minmax(0,1fr) 74px 16px;gap:6px;padding:10px}.se-pick-logo .pick-image{width:45px;height:48px}.se-pick-name{font-size:.89rem}.se-pick-match{font-size:.78rem}.se-pick-meta{font-size:.68rem}.se-kpi{grid-row:2;border-left:0;border-top:1px solid #315b7d;padding:8px 0 0}.se-kpi:nth-of-type(4){grid-column:2}.se-kpi:nth-of-type(5){grid-column:3}.se-kpi:nth-of-type(6){grid-column:4}.se-kpi strong{font-size:1.03rem}.se-odds strong{font-size:1rem}.se-odds span{font-size:.7rem}.se-research{grid-template-columns:1fr}.se-section-head h2{font-size:1.6rem!important}}

/* Reference-style Watch Out For sections, no icons */
.se-watch-sections {display:flex;flex-direction:column;gap:0}
.se-watch-section {border-top:1px solid #315b7d;padding:13px 0 12px}
.se-watch-section:first-child {border-top:0;padding-top:6px}
.se-watch-section h4 {font-size:1rem!important;font-weight:800;color:#f6faff;margin:0 0 10px!important}
.se-watch-team {padding:2px 0 9px 12px}
.se-watch-team h5 {font-size:.86rem;font-weight:800;color:#b9ddf7;margin:0 0 6px}
.se-watch-text {font-size:.82rem;color:#e3eef9;line-height:1.55;margin:0 0 7px 12px;overflow-wrap:anywhere}
.se-watch-muted {font-size:.8rem;color:#a8bed1;line-height:1.5;margin:0 0 5px 12px}
.se-market-strip {display:grid;grid-template-columns:1fr 1fr;gap:7px 16px;border:1px solid #315b7d;border-radius:9px;padding:11px 14px;margin-top:10px;font-size:.84rem;color:#d8e8f5}
.se-market-strip strong {color:#fff}.se-quote-source {color:#a7bfd3;font-size:.76rem}.se-market-strip>.se-quote-source {grid-column:1/-1}
 /* Template-matched single information bar; morning quotes only */
 .se-detail-strip.se-template-strip {display:grid;grid-template-columns:1.2fr 1fr 1fr 1.05fr;align-items:center;gap:0;padding:0;margin-top:10px;flex-wrap:nowrap}
 .se-template-strip .se-strip-item {min-width:0;padding:10px 12px;border-right:1px solid #315b7d;line-height:1.35}
 .se-template-strip .se-strip-item:last-child {border-right:0}
 .se-template-strip .se-strip-item strong {white-space:nowrap}
 .se-template-strip .se-strip-item span {overflow-wrap:anywhere}
 .se-archive-note {font-size:.72rem;color:#a7bfd3;margin:6px 3px 0}
 @media(max-width:850px){.se-detail-strip.se-template-strip{grid-template-columns:1fr 1fr}.se-template-strip .se-strip-item:nth-child(2){border-right:0}.se-template-strip .se-strip-item:nth-child(-n+2){border-bottom:1px solid #315b7d}}
 @media(max-width:650px){.se-market-strip{grid-template-columns:1fr}}


/* Exact compact four-cell market strip from the reference template. */
.se-detail-strip.se-template-strip {display:grid!important;grid-template-columns:1.2fr 1fr .85fr 1.15fr;align-items:center;gap:0!important;padding:0 4px!important;margin-top:10px;border:1px solid #315b7d;border-radius:9px;min-height:43px;font-size:.83rem}
.se-template-strip .se-strip-item {min-width:0;padding:10px 12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.se-template-strip .se-strip-item+.se-strip-item {border-left:1px solid #315b7d}
.se-template-strip .se-strip-item strong {font-weight:800;color:#fff}
.se-template-strip .se-strip-item span {color:#dce8f5}
.se-archive-note {font-size:.75rem;color:#a7bfd3;margin:6px 2px 0}
@media(max-width:900px){.se-detail-strip.se-template-strip{grid-template-columns:1fr 1fr}.se-template-strip .se-strip-item:nth-child(3){border-left:0;border-top:1px solid #315b7d}.se-template-strip .se-strip-item:nth-child(4){border-top:1px solid #315b7d}}
@media(max-width:520px){.se-detail-strip.se-template-strip{font-size:.72rem}.se-template-strip .se-strip-item{padding:9px 6px}}
</style>""", unsafe_allow_html=True)

if 'DATABASE_URL' not in st.secrets:
    st.error('Set DATABASE_URL in Streamlit app settings → Secrets.')
    st.stop()
try:
    init()
except Exception:
    st.error('Cannot connect to Supabase. Check DATABASE_URL and your Supabase project status.')
    st.stop()

# Cache Supabase pick reads for two minutes across dashboard reruns.
# This does not call The Odds API and does not change stored records.
@st.cache_data(ttl=120, show_spinner=False)
def cached_all_picks():
    return all_picks()

all_df = cached_all_picks()
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
        if any(term in lower for term in ('model quality:', 'model rolling-', 'additional recorded research', 'experimental independent record-strength estimate', 'estimated edge and ev are unvalidated', 'not historically validated', 'experimental stake tier:', 'unvalidated bankroll recommendation')):
            continue
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
    # Avoid blocking network requests during dashboard rendering.
    live = {}
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
             if url else f'<div class="pick-image pick-image-fallback" title="{esc(team)} logo unavailable">—</div>')
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
    return '<div class="pick-image pick-image-fallback" role="img" aria-label="Team logo unavailable">—</div>'


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


def saved_market_lines(r):
    """Read only explicitly saved market quotes; never infer from model odds."""
    notes = clean_text(r.get('notes'))
    result = {}
    for raw in notes.splitlines():
        line = raw.strip().lstrip('-• ').strip()
        for key, labels in (('spread', ('spread:', 'recorded spread:')),
                            ('total', ('total o/u:', 'total:', 'recorded total:'))):
            if any(line.lower().startswith(label) for label in labels):
                value = line.split(':', 1)[1].strip()
                if value:
                    result[key] = value
    return result


@st.cache_data(ttl=3600, show_spinner=False)
def morning_market_snapshots(day):
    """Read existing daily Odds API archive; never call the vendor."""
    try:
        with connect() as conn:
            rows = conn.execute("""SELECT sport, payload, collected_at FROM historical_snapshots
                WHERE source='the-odds-api' AND category='daily_odds_cache'
                  AND item_key=%s ORDER BY collected_at ASC""", (day,)).fetchall()
    except Exception:
        return {}
    # First successful snapshot per sport is the morning publication snapshot.
    result = {}
    for row in rows:
        result.setdefault(row['sport'], row)
    return result


def market_strip_html(r, load_live=False):
    """Display morning archived spreads/totals with zero additional Odds API calls."""
    day = clean_text(r.get('date'))[:10]
    sport_key = ODDS_SPORT_KEYS.get(clean_text(r.get('sport')))
    archive = morning_market_snapshots(day).get(sport_key) if sport_key else None
    found = {'spread': '', 'total': '', 'spread_book': '', 'total_book': ''}
    captured = None
    if archive:
        captured = archive.get('collected_at')
        events = (archive.get('payload') or {}).get('events') or []
        teams = matchup_teams(r)
        if len(teams) == 2:
            away, home = map(_team_key, teams)
            for event in events:
                if _team_key(event.get('away_team')) != away or _team_key(event.get('home_team')) != home:
                    continue
                try:
                    event_day = datetime.fromisoformat(event['commence_time'].replace('Z', '+00:00')).astimezone(PT).date().isoformat()
                except (ValueError, TypeError, KeyError):
                    continue
                if event_day != day:
                    continue
                books = event.get('bookmakers') or []
                preferred = BOOK_KEYS.get(recorded_bookmaker(r.get('source')).casefold())
                books = sorted(books, key=lambda book: book.get('key') != preferred)
                for market_key, field in (('spreads', 'spread'), ('totals', 'total')):
                    for book in books:
                        market = _market_from_book(book, market_key)
                        if not market:
                            continue
                        outcomes = market.get('outcomes') or []
                        if market_key == 'spreads':
                            parts = []
                            for team in (event.get('home_team'), event.get('away_team')):
                                outcome = next((o for o in outcomes if _team_key(o.get('name')) == _team_key(team)), None)
                                if outcome:
                                    value = _format_outcome(outcome, include_team=True)
                                    if value:
                                        parts.append(value)
                            value = ' · '.join(parts)
                        else:
                            parts = []
                            for side in ('Over', 'Under'):
                                outcome = next((o for o in outcomes if str(o.get('name', '')).casefold() == side.casefold()), None)
                                if outcome:
                                    value = _format_outcome(outcome)
                                    if value:
                                        parts.append(f'{side} {value}')
                            value = ' · '.join(parts)
                        if value:
                            found[field] = value
                            found[field + '_book'] = book.get('title') or book.get('key') or ''
                            break
                break
    saved = saved_market_lines(r)
    spread_full = found['spread'] or saved.get('spread', '')
    total_full = found['total'] or saved.get('total', '')

    # Template shows one compact quote per market, not both sides of the book.
    # Keep the complete archived quote available as a hover tooltip.
    def compact_spread(value):
        if not value:
            return 'Unavailable'
        sides = [x.strip() for x in re.split(r'\s*[·|]\s*', value) if x.strip()]
        teams = matchup_teams(r)
        selected = clean_text(r.get('selection'))
        chosen = next((t for t in teams if _team_key(t) in _team_key(selected)), '')
        chosen_side = next((x for x in sides if chosen and _team_key(chosen) in _team_key(x)), None)
        side = chosen_side or sides[0]
        match = re.search(r'(.+?)\s+([+-]\d+(?:\.\d+)?)\s*\(([+-]\d+)\)', side)
        if not match:
            return side
        team, point, price = match.groups()
        # Use familiar professional-team abbreviations where available.
        sport = clean_text(r.get('sport'))
        abbr = TEAM_LOGOS.get(sport, {}).get(team.strip(), '')
        if abbr:
            abbr = {'lal':'LAL','gs':'GSW','ny':'NYK','sa':'SAS','no':'NOP','utah':'UTA','wsh':'WAS','cha':'CHA','phx':'PHX','bkn':'BKN'}.get(abbr, abbr.upper())
        else:
            abbr = team.strip()
        return f'{abbr} {point} ({price})'

    def compact_total(value):
        if not value:
            return 'Unavailable'
        match = re.search(r'\bOver\s+([\d.]+)\s*\(([+-]\d+)\)', value, re.I)
        if match:
            return f'{match.group(1)} ({match.group(2)})'
        return value.split('·')[0].strip()

    spread_short = compact_spread(spread_full)
    total_short = compact_total(total_full)
    if captured:
        try:
            stamp = captured.astimezone(PT).strftime('%b %d, %I:%M %p PT')
        except (AttributeError, ValueError):
            stamp = 'time unavailable'
        note = f'Archived odds snapshot: {esc(stamp)}. Prices are not live.'
    else:
        note = 'No morning odds archive found for this sport and date. No live lookup performed.'
    return (f'<div class="se-detail-strip se-template-strip">'
            f'<div class="se-strip-item"><strong>Matchup:</strong> <span>{esc(matchup_display(r))}</span></div>'
            f'<div class="se-strip-item" title="{esc(spread_full)}"><strong>Spread:</strong> <span>{esc(spread_short)}</span></div>'
            f'<div class="se-strip-item" title="{esc(total_full)}"><strong>Total:</strong> <span>{esc(total_short)}</span></div>'
            f'<div class="se-strip-item"><strong>Model:</strong> <span>rolling-homewin-v2 (trained)</span></div>'
            f'</div><p class="se-archive-note">{note}</p>')


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


def matchup_display(r):
    """Preserve the database's explicit away @ home ordering."""
    matchup = clean_text(r.get('matchup'))
    teams = matchup_teams(r)
    if len(teams) != 2:
        return matchup or 'Matchup unavailable'
    if re.search(r'\s+@\s+|\s+at\s+', matchup, flags=re.I):
        return f'{teams[0]} @ {teams[1]}'
    # A 'vs.' source does not unambiguously identify the home side.
    return matchup


def research_items(text, limit=4):
    """Keep factual recorded research readable; do not invent fresh news."""
    items = [line.strip().lstrip('•- ').strip() for line in str(text).splitlines() if line.strip()]
    return items[:limit], items[limit:]


def selection_matchup_display(r):
    """Display matchup in stored order; never infer home/away from ambiguous vs."""
    matchup = matchup_display(r)
    selection = clean_text(r.get('selection'))
    teams = matchup_teams(r)
    if len(teams) != 2:
        return matchup
    chosen = next((t for t in teams if _team_key(t) in _team_key(selection)), '')
    if not chosen:
        return matchup
    other = teams[1] if chosen == teams[0] else teams[0]
    if re.search(r'\s+@\s+|\s+at\s+', clean_text(r.get('matchup')), flags=re.I):
        return f'{chosen} @ {other}' if chosen == teams[0] else f'{chosen} vs. {other}'
    return matchup


def watch_sections_html(warnings, r):
    """Organize saved evidence into reference-style sections; no external requests."""
    lines = [x.strip().lstrip('•- ').strip() for x in warnings.splitlines() if x.strip()]
    teams = matchup_teams(r)
    grouped = {team: [] for team in teams}
    unassigned, rest, travel, market, other = [], [], [], [], []
    for line in lines:
        low = line.lower()
        if low.startswith(('injury (', 'injuries (')):
            match = re.match(r'^Injur(?:y|ies)\s*\(([^)]+)\):\s*(.*)$', line, re.I)
            if match:
                team, detail = match.group(1).strip(), match.group(2).strip()
                actual = next((t for t in teams if _team_key(t) == _team_key(team)), team)
                grouped.setdefault(actual, []).append(detail)
            else:
                unassigned.append(line)
        elif low.startswith(('injury report', 'injuries:', 'injury:', 'suspension')):
            unassigned.append(line)
        elif low.startswith('rest:'):
            rest.append(line.split(':', 1)[1].strip())
        elif low.startswith(('travel context:', 'travel:')):
            travel.append(line.split(':', 1)[1].strip())
        elif low.startswith(('market movement:', 'odds movement:', 'line movement:')):
            market.append(line.split(':', 1)[1].strip())
        else:
            other.append(line)

    # No role/usage data is stored in these notes. Sort by reported status,
    # never assert that one player is more important than another.
    def injury_priority(detail):
        match = re.search(r'—\s*(Out|Doubtful|Questionable|Day-to-day|Probable|Active|Available)\b', detail, re.I)
        status = match.group(1).lower() if match else ''
        return {'out': 0, 'doubtful': 1, 'questionable': 2,
                'day-to-day': 3, 'probable': 4, 'active': 5, 'available': 5}.get(status, 6)

    def entries(items):
        return ''.join(f'<p class="se-watch-text">{esc(item)}</p>' for item in items)

    injuries_html = ''
    for team, reports in grouped.items():
        injuries_html += f'<div class="se-watch-team"><h5>{esc(team)}</h5>'
        if reports:
            unique = list(dict.fromkeys(reports))
            unique.sort(key=injury_priority)
            injuries_html += entries(unique[:10])
            if len(unique) > 10:
                injuries_html += f'<p class="se-watch-muted">{len(unique)-10} additional recorded injuries not shown.</p>'
        else:
            injuries_html += '<p class="se-watch-muted">No player injuries returned in the saved matchup report; availability not confirmed.</p>'
        injuries_html += '</div>'
    if unassigned:
        injuries_html += '<div class="se-watch-team">' + entries(unassigned) + '</div>'
    if not injuries_html:
        injuries_html = '<p class="se-watch-muted">Injury information unavailable.</p>'

    sections = [('Injuries', injuries_html),
                ('Rest', entries(rest) if rest else '<p class="se-watch-muted">Rest data unavailable.</p>'),
                ('Travel', entries(travel) if travel else '<p class="se-watch-muted">No verified travel context recorded.</p>'),
                ('Market movement', entries(market) if market else '<p class="se-watch-muted">No verified line movement recorded.</p>')]
    if other:
        sections.append(('Other matchup factors', entries(other)))
    return '<div class="se-watch-sections">' + ''.join(
        f'<div class="se-watch-section"><h4>{esc(title)}</h4>{content}</div>'
        for title, content in sections) + '</div>'


def why_sections_html(rationale):
    """Render recorded, labeled research; do not invent missing metrics."""
    categories = [
        ('Recent form', 'recent form:'),
        ('Offensive vs. defensive matchup', 'offensive vs. defensive matchup:'),
        ('Home/away performance', 'home/away performance:'),
        ('Strength of schedule', 'strength of schedule:'),
        ('Season-long performance', 'season-long performance:'),
    ]
    sections = {name: [] for name, _ in categories}
    lines = [x.strip().lstrip('•- ').strip() for x in rationale.splitlines() if x.strip()]
    for line in lines:
        lower = line.lower()
        matched = False
        for name, prefix in categories:
            if lower.startswith(prefix):
                sections[name].append(line[len(prefix):].strip())
                matched = True
                break
        if matched:
            continue
        if re.search(r'last \d+ completed regular-season games|last-10 performance', lower):
            sections['Recent form'].append(line)
        elif lower.startswith(('last-', 'matchup comparison:')):
            sections['Offensive vs. defensive matchup'].append(line)
        elif 'home/away form' in lower:
            sections['Home/away performance'].append(line)
    placeholders = {
        'Recent form': 'Recent completed-game data not recorded.',
        'Offensive vs. defensive matchup': 'Comparable scoring and defensive data not recorded.',
        'Home/away performance': 'Comparable recent home/road splits not yet recorded.',
        'Strength of schedule': 'Opponent-strength data not yet recorded.',
        'Season-long performance': 'Season-long team records and rankings not yet recorded.',
    }
    blocks = []
    for title, _ in categories:
        content = sections[title]
        if content:
            body = ''.join(f'<p class="se-watch-text">{esc(x)}</p>' for x in content)
        else:
            body = f'<p class="se-watch-muted">{esc(placeholders[title])}</p>'
        blocks.append(f'<div class="se-watch-section"><h4>{esc(title)}</h4>{body}</div>')
    return '<div class="se-watch-sections se-why-sections">' + ''.join(blocks) + '</div>'


def ordinal_date(value):
    """Show calendar dates with English ordinal suffixes."""
    day = value.day
    suffix = 'th' if 11 <= day % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
    return f'{value:%A, %B} {day}{suffix}, {value.year}'


def pick_time_only(value):
    """Hide the redundant ISO date while retaining the saved Pacific start time."""
    text = clean_text(value)
    text = re.sub(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)', '', text)
    return re.sub(r'\s+', ' ', text).strip(' ·,|-')


def full_pick_card(r, load_live=False):
    odds = number(r.get('odds'), None)
    prob = number(r.get('estimated_prob'), None)
    be = odds_break_even(odds) if odds is not None else None
    edge = prob * 100 - be if prob is not None and 0 < prob < 1 and be is not None else None
    rank = int(r['rank']) if pd.notna(r.get('rank')) else '—'
    rationale, warnings = pick_analysis(r)
    if not rationale:
        rationale = 'Matchup-specific research has not been recorded.'
    if not warnings:
        warnings = 'Injuries and confirmed lineups have not been independently verified.'
    # Show meaningful research, without fabricating news or suppressing warnings.
    why_html = why_sections_html(rationale)
    watch_html = watch_sections_html(warnings, r)
    selection = esc(r.get('selection'))
    matchup = esc(selection_matchup_display(r))
    start = esc(pick_time_only(r.get('start_pt'))) or 'Time unverified'
    sport = esc(r.get('sport'))
    price = f'{odds:+.0f}' if odds is not None else '—'
    units = number(r.get('stake'), 1.0)
    prob_text = f'{prob*100:.1f}%' if prob is not None and 0 < prob < 1 else '—'
    be_text = f'{be:.1f}%' if be is not None else '—'
    edge_text = f'{edge:+.1f} pp' if edge is not None else '—'
    edge_class = 'se-green' if edge is not None and edge >= 0 else 'se-red'
    market = esc(r.get('market')) or 'Not recorded'
    first = ' se-first' if rank == 1 else ''
    open_attr = ''
    market_strip = market_strip_html(r, load_live=load_live)
    return f'''<details class="se-pick{first}"{open_attr}>
      <summary><div class="se-pick-head">
        <div class="se-rank">{rank}</div>
        <div class="se-pick-logo">{card_image_html(r)}</div>
        <div><div class="se-pick-name">{selection}</div><div class="se-pick-match">{matchup}</div><div class="se-pick-meta">{sport} · {start}</div></div>
        <div class="se-kpi"><small>Win Prob.</small><strong>{prob_text}</strong></div>
        <div class="se-kpi"><small>Break-Even</small><strong>{be_text}</strong></div>
        <div class="se-kpi"><small>Model Edge</small><strong class="{edge_class}">{edge_text}</strong></div>
        <div class="se-odds"><strong>{price}</strong><span>{units:g} {'unit' if units == 1 else 'units'}</span></div>
        <div class="se-chevron"></div>
      </div></summary>
      <div class="se-pick-expanded">
        <div class="se-research">
          <section class="se-research-panel"><h3>Why This Pick?</h3>{why_html}</section>
          <section class="se-research-panel"><h3>Watch Out For:</h3>{watch_html}</section>
        </div>
        {market_strip}
        <p class="se-disclaimer">Research reflects saved notes, not a live injury or news feed. Confirm prices, availability, and lineups before placing a wager.</p>
      </div>
    </details>'''


# Render the dashboard once per page load. Avoid expensive periodic full-page rerenders.
def render_live_dashboard():
    today = datetime.now(PT).date()
    fresh_df = cached_all_picks()
    if fresh_df is None:
        fresh_df = pd.DataFrame()
    for col in ['date', 'sport', 'market', 'matchup', 'selection', 'kind', 'result', 'source', 'notes', 'start_pt', 'result_source']:
        if col not in fresh_df.columns:
            fresh_df[col] = pd.Series(dtype='object')
    for col in ['odds', 'stake', 'rank', 'estimated_prob']:
        if col not in fresh_df.columns:
            fresh_df[col] = pd.Series(dtype='float64')
    df = fresh_df[fresh_df['kind'].eq(kind) & (fresh_df['date'].astype(str) >= RECORD_START_DATE)].copy()
    TODAY = today
    summary = stats(df)
    net = number(summary.get('net'))
    roi = summary.get('roi')
    yesterday = TODAY - timedelta(days=1)
    y = df[df['date'].astype(str) == yesterday.isoformat()]
    ys = stats(y)
    yn = number(ys.get('net'))
    recap_html = ''
    for i, (_, item) in enumerate(y.sort_values('rank', na_position='last').iterrows(), 1):
        outcome = clean_text(item.get('result')) or 'Pending'
        pl = profit(item)
        result_letter = {'Win':'W', 'Loss':'L', 'Push':'P', 'Void':'V'}.get(outcome, '—')
        cls = 'se-green' if outcome == 'Win' else 'se-red' if outcome == 'Loss' else ''
        pl_text = signed_units(pl) if pl is not None else 'Pending'
        recap_html += (f'<div class="se-recap-row"><span>{i}.</span>'
                       f'<span>{esc(item.get("selection"))}</span>'
                       f'<span class="se-result {cls}">{result_letter}</span>'
                       f'<span class="{cls}">{esc(pl_text)}</span></div>')
    if not recap_html:
        recap_html = '<div style="color:#adc5d9;font-size:.85rem">No picks recorded yesterday.</div>'
    sport_html = ''
    short_names = {'College Football':'CFB','College Basketball':'CBB'}
    for name in SPORTS:
        sport_record = format_record(stats(df[df['sport'] == name]))
        sport_html += f'<div class="se-sport">{esc(short_names.get(name, name))}<strong>{esc(sport_record)}</strong></div>'
    recap_label = f'{format_record(ys)} ({signed_units(yn)})' if not y.empty else 'No picks'
    roi_text = f'{roi:+.1f}%' if roi is not None else '—'
    st.markdown(f'''<div class="se-brand"><div class="se-mark"><i></i><i></i><i></i></div>
      <div><h1>Sports Edge Analytics</h1><p>Model-Driven Sports Betting</p></div>
      <div class="se-nav"><span>Today's Picks</span><span>Record</span><span>Analytics</span></div></div>
      <section class="se-board">
        <div class="se-board-col"><h3>Overall Record</h3><div class="se-big-record">{esc(format_record(summary))}</div>
          <div class="se-net">Net: <span class="{'se-green' if net>=0 else 'se-red'}">{signed_units(net)}</span>
          &nbsp; | &nbsp; ROI: <span class="{'se-green' if roi is not None and roi>=0 else 'se-red'}">{roi_text}</span></div></div>
        <div class="se-board-col"><div class="se-recap-heading"><h3>Yesterday's Recap — {ordinal_date(yesterday)}</h3><span class="se-recap-pill">{esc(recap_label)}</span></div>
          <div class="se-recap-list">{recap_html}</div></div>
        <div class="se-board-col"><h3>Record by Sport</h3><div class="se-sports">{sport_html}</div></div>
      </section>''', unsafe_allow_html=True)

    today_df=df[df['date'].astype(str)==TODAY.isoformat()].sort_values('rank',na_position='last')
    ranked=today_df[today_df['rank'].notna()].head(5)
    if ranked.empty:
        picks_html='<div class="section-sub" style="padding:20px 5px">No verified Top 5 has been published today. The automated publisher still needs independently modeled probabilities and verified game eligibility.</div>'
    else:
        picks_html=''.join(full_pick_card(pick) for _,pick in ranked.iterrows())
        if len(ranked)!=5 or sorted(ranked['rank'].astype(int).tolist())!=[1,2,3,4,5]:
            picks_html='<div class="section-sub">Today’s card is incomplete; only verified recorded picks are shown.</div>'+picks_html
    st.markdown(f'<div class="se-section-head"><h2>Today’s Top 5 Picks</h2><p>{TODAY:%A, %B %d, %Y} &nbsp; | &nbsp; All times Pacific</p></div>{picks_html}',unsafe_allow_html=True)

    st.divider()
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


if page == 'Dashboard':
    if st.button('Refresh dashboard data', key='refresh_dashboard_data', help='Fetch the latest saved picks from Supabase; no Odds API calls.'):
        cached_all_picks.clear()
        morning_market_snapshots.clear()
    render_live_dashboard()

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
