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

st.title("📈 Today's Sports Betting Analytics")
st.caption(f"Performance tracking and research ledger • {TODAY:%A, %B %d, %Y} • Pacific Time • {kind} picks")

if page == 'Dashboard':
    with st.container(border=True):
        header('Filters', 'Select a date range, sport, market or result to explore recorded picks.')
        a,b,c,d=st.columns([2,1.25,1.25,1.25])
        date_range=a.date_input('Date range', value=(TODAY-timedelta(days=30), TODAY), key='filter_dates')
        sport=b.selectbox('Sport', ['All']+SPORTS)
        market=c.selectbox('Market', ['All','Moneyline','Spread','Total','Player prop','Other'])
        status=d.selectbox('Status', ['All','Pending','Win','Loss','Push','Void'])
        with st.expander('More filters'):
            st.caption('Filters apply to the selected ledger only. No external odds or sportsbook feed is connected.')
        filtered=df.copy()
        if isinstance(date_range,(list,tuple)) and len(date_range)==2:
            filtered=filtered[(filtered['date'].astype(str)>=date_range[0].isoformat()) & (filtered['date'].astype(str)<=date_range[1].isoformat())]
        if sport!='All': filtered=filtered[filtered['sport']==sport]
        if market!='All': filtered=filtered[filtered['market']==market]
        if status!='All': filtered=filtered[filtered['result']==status]

    s=stats(filtered)
    risk=number(s.get('risk'))
    net=number(s.get('net'))
    roi=s.get('roi')
    settled=int(filtered['result'].isin(['Win','Loss','Push','Void']).sum())
    pending=int(filtered['result'].eq('Pending').sum())
    graded=filtered[filtered['result'].isin(['Win','Loss'])]
    win_rate=100*sum(graded['result']=='Win')/len(graded) if len(graded) else None
    with st.container(border=True):
        header('Performance Summary', 'Filtered pick-level ledger • not simulated sample data')
        a,b,c,d,e,f=st.columns(6)
        a.metric('Total picks',len(filtered));b.metric('Profit / Loss',f'{net:+.2f}u');c.metric('ROI',f'{roi:.1f}%' if roi is not None else '—')
        d.metric('Settled',settled);e.metric('Pending',pending);f.metric('Total staked',f'{risk:.2f}u')
        a,b,c,d,e,f=st.columns(6)
        a.metric('W–L–P',format_record(s))
        b.metric('Win rate',f'{win_rate:.1f}%' if win_rate is not None else '—')
        aodds=pd.to_numeric(filtered['odds'],errors='coerce').dropna()
        c.metric('Avg odds',f'{aodds.mean():+.0f}' if len(aodds) else '—')
        probs=pd.to_numeric(filtered['estimated_prob'],errors='coerce')
        bes=pd.to_numeric(filtered['odds'],errors='coerce').map(odds_break_even)
        valid=probs.notna() & (probs>0) & bes.notna()
        d.metric('Avg model edge',f'{(probs[valid]*100-bes[valid]).mean():+.1f} pp' if valid.any() else '—')
        gains=[profit(r) for _,r in filtered.iterrows()]; gains=[x for x in gains if x is not None]
        e.metric('Best profit',f'{max(gains):+.2f}u' if gains else '—')
        f.metric('Worst loss',f'{min(gains):+.2f}u' if gains else '—')
    hist=baseline() if kind=='Hypothetical' else None
    if hist:
        st.info(f"Historical aggregate baseline through {hist['as_of']}: {hist['wins']}–{hist['losses']}–{hist['pushes']}, {hist['net']:+.2f}u. Not included in filtered charts or pick-level totals to avoid implying reconstructed data.")

    with st.container(border=True):
        header('Performance by Sport')
        cols=st.columns(6)
        for col, name, emoji in zip(cols, SPORTS, SPORT_ICONS):
            q=filtered[filtered['sport']==name]
            sq=stats(q)
            sr=sq.get('roi')
            col.markdown(f'''<div class="sportcard"><div class="name">{emoji} {name}</div>
            <div class="detail">Picks <strong>{len(q)}</strong><br>W–L–P <strong>{format_record(sq)}</strong><br>
            Profit <strong>{number(sq.get('net')):+.2f}u</strong><br>ROI <strong>{f'{sr:.1f}%' if sr is not None else '—'}</strong></div></div>''', unsafe_allow_html=True)

    with st.container(border=True):
        header('Bet Status & Performance')
        left,right=st.columns([1,2])
        counts=filtered['result'].value_counts().reindex(['Win','Loss','Push','Void','Pending'],fill_value=0)
        with left:
            st.markdown('**Bet Status Distribution**')
            if counts.sum():
                import altair as alt
                chartdata=pd.DataFrame({'Result':counts.index,'Picks':counts.values})
                donut=alt.Chart(chartdata).mark_arc(innerRadius=65,outerRadius=108).encode(
                    theta='Picks:Q',color=alt.Color('Result:N',scale=alt.Scale(domain=['Win','Loss','Push','Void','Pending'],range=['#13c98b','#ef5261','#388bff','#8595aa','#f7b733'])),
                    tooltip=['Result','Picks']).properties(height=265)
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
        header("Today's Top 5 Picks",f'{TODAY.isoformat()} • {kind} ledger')
        today_df=df[df['date'].astype(str)==TODAY.isoformat()].sort_values('rank',na_position='last').head(5)
        if today_df.empty: st.info('No picks entered for today. Use Add pick to enter sourced selections. Automatic picks are not enabled yet.')
        else:
            if len(today_df)!=5 or sorted(today_df['rank'].dropna().astype(int).tolist())!=[1,2,3,4,5]:
                st.warning('Top 5 card is incomplete: five unique ranks (1–5) are required.')
            st.dataframe(make_table(today_df),hide_index=True,use_container_width=True)
    with st.container(border=True):
        yesterday=TODAY-timedelta(days=1)
        header("Yesterday's Recap",yesterday.isoformat())
        y=df[df['date'].astype(str)==yesterday.isoformat()]
        if y.empty: st.caption('No recorded picks for yesterday.')
        else:
            st.write('**Result:**',format_record(stats(y)))
            st.dataframe(make_table(y),hide_index=True,use_container_width=True)
            if y['result'].eq('Pending').any(): st.warning('Some results are pending verification.')
    with st.expander('All recorded picks'):
        st.dataframe(filtered,hide_index=True,use_container_width=True)

elif page=='Top 5 card':
    header('Daily Top 5 Card')
    day=st.date_input('Card date',TODAY).isoformat()
    card=df[df['date'].astype(str)==day].sort_values('rank',na_position='last')
    if card.empty: st.info('No picks for this day. Add verified, sourced picks first.')
    else:
        if len(card)!=5 or sorted(card['rank'].dropna().astype(int).tolist())!=[1,2,3,4,5]:
            st.warning('A complete card needs five unique ranks from 1 to 5.')
        st.dataframe(make_table(card),hide_index=True,use_container_width=True)
        for r in card.itertuples():
            with st.expander(f"#{getattr(r,'rank','—')} • {r.sport} • {r.selection}"):
                st.write('Matchup:',r.matchup,'| Start (PT):',r.start_pt or 'Not sourced')
                st.write('Source:',r.source or 'Not supplied')
                st.write('Notes:',r.notes or 'Not supplied')

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
- **This app is a manual cloud ledger.** Automated prediction, odds feeds, daily pick generation and auto-grading are not implemented.
''')
