"""Persistent PostgreSQL storage for Streamlit Community Cloud + Supabase."""
import pandas as pd
import psycopg
from psycopg.rows import dict_row
import streamlit as st

COLUMNS = ['date','sport','matchup','market','selection','odds','stake','kind','result','source','result_source','notes','rank','start_pt','estimated_prob']

def connect():
    return psycopg.connect(st.secrets['DATABASE_URL'], row_factory=dict_row, connect_timeout=12)

def init():
    with connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS picks (
          id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, date TEXT NOT NULL, sport TEXT NOT NULL,
          matchup TEXT NOT NULL, market TEXT NOT NULL, selection TEXT NOT NULL,
          odds INTEGER, stake DOUBLE PRECISION NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('Hypothetical','Actual')),
          result TEXT NOT NULL DEFAULT 'Pending' CHECK(result IN ('Pending','Win','Loss','Push','Void')),
          source TEXT, result_source TEXT, notes TEXT, rank INTEGER, start_pt TEXT,
          estimated_prob DOUBLE PRECISION, created_at TIMESTAMPTZ DEFAULT NOW())''')
        c.execute('''CREATE TABLE IF NOT EXISTS baseline (
          id INTEGER PRIMARY KEY CHECK(id=1), as_of TEXT, wins INTEGER, losses INTEGER,
          pushes INTEGER, net DOUBLE PRECISION, risked DOUBLE PRECISION, note TEXT)''')

def profit(result, odds, stake):
    if result == 'Loss': return -stake
    if result in ('Push','Void'): return 0.0
    if result == 'Win' and odds is not None:
        return stake*odds/100 if odds>0 else stake*100/abs(odds) if odds<0 else None
    return None

def add(p):
    keys=COLUMNS
    with connect() as c:
        c.execute(f"INSERT INTO picks ({','.join(keys)}) VALUES ({','.join('%s' for _ in keys)})",[p.get(k) for k in keys])

def all_picks():
    with connect() as c:
        rows=c.execute('SELECT * FROM picks ORDER BY date DESC,id DESC').fetchall()
    return pd.DataFrame(rows,columns=['id']+COLUMNS+['created_at'])

def grade(pid,result,url):
    with connect() as c:
        c.execute('UPDATE picks SET result=%s,result_source=%s WHERE id=%s AND result=%s',(result,url,int(pid),'Pending'))

def baseline():
    with connect() as c:
        return c.execute('SELECT * FROM baseline WHERE id=1').fetchone()

def set_baseline(b):
    with connect() as c:
        c.execute('''INSERT INTO baseline (id,as_of,wins,losses,pushes,net,risked,note)
          VALUES (1,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET
          as_of=excluded.as_of,wins=excluded.wins,losses=excluded.losses,
          pushes=excluded.pushes,net=excluded.net,risked=excluded.risked,note=excluded.note''',
          (b['as_of'],b['wins'],b['losses'],b['pushes'],b['net'],b['risked'],b['note']))

def import_rows(df):
    required={'date','sport','matchup','market','selection','odds','stake','kind','result'}
    missing=required-set(df.columns)
    if missing: raise ValueError('Missing columns: '+', '.join(sorted(missing)))
    clean=[]
    valid_sports={'NFL','NBA','MLB','College Football','NHL','College Basketball'}
    for n,r in df.iterrows():
        p={k:(None if k not in df.columns or pd.isna(r.get(k)) or r.get(k)=='' else r.get(k)) for k in COLUMNS}
        p['date']=str(p['date'])[:10]
        if pd.isna(pd.to_datetime(p['date'],errors='coerce')): raise ValueError(f'Row {n+2}: invalid date')
        if p['kind'] not in ('Hypothetical','Actual'): raise ValueError(f'Row {n+2}: invalid kind')
        if p['result'] not in ('Pending','Win','Loss','Push','Void'): raise ValueError(f'Row {n+2}: invalid result')
        aliases={'CFB':'College Football','CBB':'College Basketball'}
        p['sport']=aliases.get(p['sport'],p['sport'])
        if p['sport'] not in valid_sports: raise ValueError(f'Row {n+2}: invalid sport')
        p['stake']=float(p['stake'])
        if p['stake']<=0: raise ValueError(f'Row {n+2}: stake must be positive')
        p['odds']=int(p['odds']) if p['odds'] is not None else None
        if p['odds'] is not None and -100<p['odds']<100: raise ValueError(f'Row {n+2}: invalid American odds')
        if p['result']=='Win' and p['odds'] is None: raise ValueError(f'Row {n+2}: winning pick needs odds')
        if p['rank'] is not None: p['rank']=int(p['rank'])
        if p['estimated_prob'] is not None: p['estimated_prob']=float(p['estimated_prob'])
        clean.append(p)
    with connect() as c:
        c.executemany(f"INSERT INTO picks ({','.join(COLUMNS)}) VALUES ({','.join('%s' for _ in COLUMNS)})",[[p[k] for k in COLUMNS] for p in clean])
    return len(clean)
