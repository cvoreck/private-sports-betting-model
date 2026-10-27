from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import io
import pandas as pd
import streamlit as st
from storage import init,add,all_picks,grade,baseline,set_baseline,import_rows
from analytics import stats,format_record

st.set_page_config(page_title='Private Top 5',page_icon='📊',layout='wide')
if 'DATABASE_URL' not in st.secrets:
    st.error('Configure DATABASE_URL in Streamlit App settings → Secrets before running this app.')
    st.stop()
try:
    init()
except Exception as exc:
    st.error('Cannot connect to the Supabase database. Check the DATABASE_URL secret and project status.')
    st.stop()
st.title('📊 Private Top 5')
st.caption('Cloud sports betting research ledger • persistent Supabase storage • no live data or automatic bets')
page=st.sidebar.radio('Pages',['Dashboard','Top 5 card','Add pick','Grade results','Import / backup','Rules'])
kind=st.sidebar.radio('Ledger',['Hypothetical','Actual'],index=0)
all_df=all_picks()
df=all_df[all_df.kind==kind].copy()

if page=='Dashboard':
    st.subheader(f'{kind} running record')
    s=stats(df); b=baseline() if kind=='Hypothetical' else None
    if b:
        st.info(f"Historical baseline through {b['as_of']}: {b['wins']}–{b['losses']}–{b['pushes']}, {b['net']:+.2f}u. Baseline is not a pick-level ledger; do not import its constituent picks again without removing the baseline.")
        w=s['w']+b['wins']; l=s['l']+b['losses']; p=s['p']+b['pushes']; net=s['net']+b['net']
        risk=s['risk']+b['risked'] if b['risked'] is not None else None
    else:
        w=s['w'];l=s['l'];p=s['p'];net=s['net'];risk=s['risk']
    roi=100*net/risk if risk and not s['unknown'] else None
    a,c,d,e=st.columns(4)
    a.metric('W–L–P',f'{w}–{l}–{p}'); c.metric('Net units',f'{net:+.2f}u')
    d.metric('ROI',f'{roi:.2f}%' if roi is not None else 'Unknown');e.metric('Pending',int((df.result=='Pending').sum()))
    if b and b['risked'] is None: st.warning('Combined ROI cannot be calculated until baseline total units risked is known. Do not infer it from rounded historical ROI.')
    st.subheader('Sport-by-sport results (imported/entered picks only)')
    rows=[]
    for sport in ['NFL','NBA','MLB','College Football','NHL','College Basketball']:
        q=stats(df[df.sport==sport]);rows.append({'Sport':sport,'W–L–P':format_record(q),'Net units':round(q['net'],2),'ROI':round(q['roi'],2) if q['roi'] is not None else None})
    st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)
    yesterday=(datetime.now(ZoneInfo("America/Los_Angeles")).date()-timedelta(days=1)).isoformat()
    st.subheader(f'Previous-day recap ({yesterday})')
    y=df[df.date==yesterday]
    if y.empty: st.write('No recorded picks for this date.')
    else:
        st.write('Yesterday:',format_record(stats(y)))
        st.dataframe(y[['sport','matchup','selection','odds','result','stake']],hide_index=True,use_container_width=True)
        if (y.result=='Pending').any():st.warning('Some results remain unverified and pending.')
    st.subheader('Ledger')
    st.dataframe(df,use_container_width=True,hide_index=True)

elif page=='Top 5 card':
    st.subheader('Daily Top 5 card')
    day=st.date_input('Card date',datetime.now(ZoneInfo("America/Los_Angeles")).date()).isoformat()
    card=df[df.date==day].sort_values('rank',na_position='last')
    if card.empty: st.info('No picks for this day. Add sourced picks in Add pick. The MVP does not fabricate selections.')
    else:
        if len(card)!=5 or sorted(card['rank'].dropna().astype(int).tolist())!=[1,2,3,4,5]:
            st.warning('A complete Top 5 requires exactly five picks with unique ranks 1–5.')
        for p in card.itertuples():
            st.markdown(f"**#{p.rank if pd.notna(p.rank) else '?'} · {p.sport} · {p.matchup}**")
            st.write(f'{p.selection} ({p.odds:+d}) · {p.start_pt or "Start time not sourced"} · {p.result}')
            if pd.notna(p.odds):
                breakeven=100/(1+p.odds/100) if p.odds>0 else 100*abs(p.odds)/(100+abs(p.odds))
                st.write(f'Break-even: {breakeven:.1f}%' + (f' · Estimated win probability: {100*p.estimated_prob:.1f}% · Edge: {100*p.estimated_prob-breakeven:+.1f} percentage points' if pd.notna(p.estimated_prob) else ' · Win probability not estimated'))
            st.caption(f'Source: {p.source or "Not provided"} · Notes: {p.notes or "Not provided"}')

elif page=='Add pick':
    st.subheader('Add a sourced pick')
    with st.form('new'):
        a,b,c=st.columns(3)
        day=a.date_input('Date',datetime.now(ZoneInfo("America/Los_Angeles")).date());sport=b.selectbox('Sport',['NFL','NBA','MLB','College Football','NHL','College Basketball']);rank=c.selectbox('Top 5 rank (optional)',['None',1,2,3,4,5])
        matchup=st.text_input('Matchup');selection=st.text_input('Selection (e.g. Team -3.5)')
        a,b,c=st.columns(3)
        market=a.selectbox('Market',['Moneyline','Spread','Total','Player prop','Other'])
        odds=b.number_input('American odds',value=-110,step=5)
        stake=c.number_input('Units risked',value=1.0,min_value=0.01,step=0.25)
        a,b=st.columns(2)
        start=a.text_input('Start time PT (from source)');prob=b.number_input('Estimated win probability % (0 = unknown)',min_value=0.,max_value=100.,value=0.)
        source=st.text_input('Odds/stat source URL');notes=st.text_area('Recent form, injury context, rationale and verification notes')
        submit=st.form_submit_button('Save pick')
    if submit:
        if not matchup.strip() or not selection.strip() or -100<int(odds)<100:
            st.error('Enter matchup and selection, and valid American odds (≤ -100 or ≥ +100).')
        elif rank!='None' and ((df.date==day.isoformat()) & (df['rank']==rank)).any():
            st.error('That rank already exists for this date and ledger.')
        else:
            add(dict(date=day.isoformat(),sport=sport,matchup=matchup,market=market,selection=selection,odds=int(odds),stake=stake,kind=kind,result='Pending',source=source,result_source='',notes=notes,rank=None if rank=='None' else rank,start_pt=start,estimated_prob=prob/100 if prob else None))
            st.success('Saved to Supabase.');st.rerun()

elif page=='Grade results':
    st.subheader('Grade verified outcomes')
    pending=df[df.result=='Pending']
    if pending.empty:st.info('No pending picks in this ledger.')
    else:
        ids=pending.id.tolist();pid=st.selectbox('Pick',ids,format_func=lambda x: f"#{x} · {pending[pending.id==x].iloc[0]['date']} · {pending[pending.id==x].iloc[0]['selection']}")
        result=st.selectbox('Verified result',['Win','Loss','Push','Void']);url=st.text_input('Result verification URL')
        if st.button('Settle'):
            if not url.strip():st.error('Provide a source URL before grading; otherwise keep the pick pending.')
            else:grade(pid,result,url);st.success('Settled.');st.rerun()

elif page=='Import / backup':
    st.subheader('Import your existing pick-level record')
    st.write('CSV columns: date,sport,matchup,market,selection,odds,stake,kind,result. Optional: source,result_source,notes,rank,start_pt,estimated_prob (0–1).')
    st.warning('Imports append rows; no automatic duplicate detection. Back up your database first. Do not import historical picks also represented in the baseline.')
    upload=st.file_uploader('Pick ledger CSV',type='csv')
    if upload:
        try:
            incoming=pd.read_csv(upload,keep_default_na=False)
            st.dataframe(incoming.head(10))
            if st.button('Import rows'):
                n=import_rows(incoming);st.success(f'Imported {n} rows.');st.rerun()
        except Exception as exc:st.error(str(exc))
    st.download_button('Export all picks CSV',all_df.to_csv(index=False),'picks_backup.csv','text/csv')
    st.caption('Cloud database backups should be managed in Supabase. The CSV export above backs up your pick-level ledger.')
    st.divider();st.subheader('Optional historical aggregate baseline')
    st.write('Use only if the original individual picks are unavailable. A baseline preserves totals but cannot reconstruct individual picks, exact ROI, or sport-by-sport details.')
    old=baseline()
    with st.form('baseline'):
        asof=st.text_input('As-of date (YYYY-MM-DD)',value=old['as_of'] if old else '')
        a,b,c=st.columns(3)
        w=a.number_input('Wins',min_value=0,value=old['wins'] if old else 0)
        l=b.number_input('Losses',min_value=0,value=old['losses'] if old else 0)
        p=c.number_input('Pushes',min_value=0,value=old['pushes'] if old else 0)
        net=st.number_input('Net units',value=float(old['net']) if old else 0.,format='%.2f')
        risked=st.text_input('Total units risked (blank if unknown)',value=str(old['risked']) if old and old['risked'] is not None else '')
        note=st.text_area('Provenance / uncertainty',value=old['note'] if old else '')
        save=st.form_submit_button('Save baseline')
    if save:
        try:
            if pd.isna(pd.to_datetime(asof,errors='coerce')):raise ValueError('Enter a valid as-of date')
            risk=float(risked) if risked.strip() else None
            if risk is not None and risk<=0:raise ValueError('Risk must be positive')
            set_baseline(dict(as_of=asof,wins=w,losses=l,pushes=p,net=net,risked=risk,note=note))
            st.success('Historical aggregate saved.');st.rerun()
        except Exception as exc:st.error(str(exc))

else:
    st.subheader('Top 5 rules (policy for future research automation)')
    st.markdown('''
- **Exactly five ranked picks** when five evidence-backed opportunities exist. Never invent a pick to fill a card.
- **Priority:** NFL > NBA > MLB > College Football > NHL > college basketball. No preseason.
- College Football and College Basketball only if at least one team is **AP Top 25** at the relevant time.
- NHL: moneyline, spread and total markets before player props.
- Display sportsbook American odds, Pacific start time, past-10 form (or actual smaller sample), break-even %, estimated win probability, edge, injury/lineup context, and rationale.
- Verify yesterday's picks before updating the running record. Unverified picks remain pending.
- Keep hypothetical recommendations and actual wagers separate. Do not double-count parlay legs as independent stakes.
- No fabricated data, results, sources, rankings, or model probabilities.

**MVP scope:** This version is a manual, cloud PostgreSQL ledger and Top 5 display. It does **not** automatically research, generate picks, fetch live odds, grade results, or use Ollama yet. Ollama will be added after reliable data feeds are configured; an LLM alone cannot supply verified current statistics.
''')
