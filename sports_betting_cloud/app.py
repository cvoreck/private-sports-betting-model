"""Sports Edge Analytics — Streamlit dashboard, backed by the existing Supabase ledger.
Replace sports_betting_cloud/app.py with this file; keep storage.py and analytics.py.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import html
from urllib.parse import urlsplit
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
.pick-card {display:grid;grid-template-columns:42px 76px minmax(190px,2.1fr) minmax(85px,.72fr) minmax(85px,.72fr) minmax(85px,.72fr) minmax(180px,1.65fr) minmax(180px,1.65fr);border:1px solid #164c72;border-radius:7px;background:linear-gradient(110deg,#082841,#061d33);margin:6px 0;overflow:hidden}
.pick-card>div {padding:13px 11px;min-width:0;border-right:1px solid #143b5b}
.pick-card>div:last-child {border-right:none}
.pick-image {width:66px;height:66px;object-fit:contain;border-radius:8px;background:#12344d;padding:5px}
.pick-rank {font-size:1.85rem;font-weight:800;text-align:center;color:#fff}
.pick-name {font-size:1.02rem;font-weight:800;color:#f4f8ff;line-height:1.35}
.pick-meta {font-size:.76rem;color:#b9cee7;margin:5px 0 10px}
.pick-desc {font-size:.79rem;color:#c5d7ec;line-height:1.45}
.pick-kpi-label {font-size:.76rem;color:#bed0e8;line-height:1.45}
.pick-kpi-value {font-size:1.17rem;font-weight:750;color:#f5f9ff;margin-top:9px}
.pick-col-title {font-size:.81rem;font-weight:800;color:#fff;margin-bottom:9px}
.pick-bullets {font-size:.76rem;color:#c5d8ee;line-height:1.47;overflow-wrap:anywhere}
.pick-bullets ul {padding-left:15px;margin:0}.pick-bullets li {margin-bottom:5px}
.final-table {width:100%;border-collapse:collapse;color:#dceaff;font-size:.81rem}
.final-table th {text-align:left;color:#d0e1f5;background:#08243c;padding:8px;border-bottom:1px solid #174768}
.final-table td {padding:8px;border-bottom:1px solid #113b59}
.final-table tr:nth-child(even) {background:#07233a}
[data-testid="stVerticalBlockBorderWrapper"] {background:#061f34!important;border-color:#174669!important}
[data-testid="stDataFrame"] {border:1px solid #16486d;border-radius:7px}
@media(max-width:1150px){.sports-grid{grid-template-columns:repeat(3,1fr)}.pick-card{grid-template-columns:36px 76px 2fr repeat(3,minmax(75px,1fr))}.pick-card .analysis-cell{grid-column:span 2;border-top:1px solid #16486d}.pick-card .watch-cell{grid-column:span 3;border-top:1px solid #16486d}}
@media(max-width:680px){.block-container{padding:1rem!important}.metrics-grid{grid-template-columns:repeat(2,1fr)}.sports-grid{grid-template-columns:repeat(2,1fr)}.pick-card{display:block}.pick-card>div{border-right:0;border-bottom:1px solid #16486d}.recap-flex{display:block}}
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


def show_pick_card(r):
    rank = int(r['rank']) if pd.notna(r.get('rank')) else '—'
    odds = number(r.get('odds'), None)
    prob = number(r.get('estimated_prob'), None)
    be = odds_break_even(odds) if odds is not None else None
    edge = (prob * 100 - be) if prob is not None and 0 < prob < 1 and be is not None else None
    label = clean_text(r.get('selection')) or 'Unspecified selection'
    price = f' ({int(odds):+d})' if odds is not None else ''
    rationale, warnings = pick_analysis(r)

    with st.container(border=True):
        st.markdown(f"### {rank}. {label}{price}")
        st.caption(f"{clean_text(r.get('sport'))} • {clean_text(r.get('matchup'))} • {clean_text(r.get('start_pt')) or 'Start time not sourced'}")
        a, b, c = st.columns(3)
        a.metric('Break-even probability', f'{be:.1f}%' if be is not None else '—')
        b.metric('Estimated win probability', f'{prob * 100:.1f}%' if prob is not None and 0 < prob < 1 else 'Not available')
        c.metric('Estimated edge', f'{edge:+.1f} pp' if edge is not None else 'Not available')

        st.markdown('<div style="height:16px"></div>', unsafe_allow_html=True)
        why_col, watch_col = st.columns([1.7, 1], gap='large')
        with why_col:
            st.markdown('**Why this pick**')
            st.write(rationale if rationale else 'No independently sourced matchup analysis has been recorded for this pick yet.')
            if not rationale or 'last-10' not in rationale.lower():
                st.caption('Last-10 form has not been documented or verified for this pick.')
        with watch_col:
            st.markdown('**Watch out for**')
            st.write(warnings if warnings else 'Injury, suspension, lineup, and breaking-news checks have not yet been documented. Verify before placing a wager.')

        st.markdown('<div style="height:12px"></div>', unsafe_allow_html=True)
        st.caption(f"Odds / research source: {clean_text(r.get('source')) or 'Not recorded'}")
        if odds is not None:
            st.caption(f"Price requirement: {int(odds):+d} or better at a traditional sportsbook. Confirm the current line before betting.")
    st.markdown('<div style="height:26px"></div>', unsafe_allow_html=True)


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
    image=pick_image(r.get('notes'))
    image_html=f'<img class="pick-image" src="{esc(image)}" alt="Team logo" loading="lazy">' if image else '<div class="pick-image" aria-label="No verified image available"></div>'
    source=esc(r.get('source')) or 'Source not recorded'
    description=f'Odds / research source: {source}. Confirm the current price before wagering.'
    return f"""<div class="pick-card">
      <div class="pick-rank">{rank}</div>
      <div><div class="pick-name">{label}{price}</div><div class="pick-meta">{esc(r.get('sport'))} · {esc(r.get('matchup'))} · {esc(r.get('start_pt')) or 'Start time unverified'}</div><div class="pick-desc">{description}</div></div>
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
    st.markdown(f'<section class="dashboard-section"><div class="section-title">Running Record</div><div class="section-sub">Hypothetical picks · ungraded picks excluded from W–L–P</div><div class="metrics-grid">{metrics_html}</div></section>',unsafe_allow_html=True)
    hist=baseline() if kind=='Hypothetical' else None
    if hist:
        st.caption(f"Historical baseline through {hist['as_of']}: {hist['wins']}–{hist['losses']}–{hist['pushes']}, {hist['net']:+.2f}u (separate from pick-level totals; not double counted).")

    yesterday=TODAY-timedelta(days=1)
    y=df[df['date'].astype(str)==yesterday.isoformat()]
    ys=stats(y)
    yn=number(ys.get('net'))
    recap=(f'{format_record(ys)} &nbsp; | &nbsp; {signed_units(yn)}' if not y.empty else 'No picks recorded')
    recap_detail=('Some results are awaiting verification.' if y['result'].eq('Pending').any() else f'{len(y)} recorded picks; see history below.') if not y.empty else 'No recorded picks for yesterday.'
    st.markdown(f'<section class="dashboard-section"><div class="recap-flex"><div><div class="section-title">Yesterday’s Recap</div><div class="section-sub">{yesterday:%A, %B %d, %Y}</div><div class="section-sub">{esc(recap_detail)}</div></div><div class="recap-result {signed_class(yn)}">{recap}</div></div></section>',unsafe_allow_html=True)

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
    st.markdown(f'<section class="dashboard-section"><div class="section-title" style="font-size:1.35rem">Today’s Top 5</div><div class="section-sub">Ranked, independently verified selections · Pacific Time · no fictional picks</div>{picks_html}</section>',unsafe_allow_html=True)

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
            done['stake_num']=pd.to_numeric(done['stake'],errors='coerce').fillna(0)
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
            show_pick_card(pick)
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
- **Automation:** A scheduled publisher can read an odds feed, but picks require independently generated probabilities and verified event eligibility. Auto-grading is not implemented.
''')
