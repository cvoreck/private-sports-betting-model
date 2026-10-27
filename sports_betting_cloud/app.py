
def make_table(data):
    rows = []

    for _, r in data.iterrows():
        be = odds_break_even(r.get('odds'))
        prob = number(r.get('estimated_prob'), None)
        edge = (
            prob * 100 - be
            if prob is not None and be is not None and prob > 0
            else None
        )
        p = profit(r)

        row = {
            '#': int(r['rank']) if pd.notna(r.get('rank')) else '—',
            'Sport': r.get('sport', ''),
            'Matchup': r.get('matchup', ''),
            'Pick': r.get('selection', ''),
            'Odds': f"{int(r['odds']):+d}" if pd.notna(r.get('odds')) else '—',
            'Stake': f"{number(r.get('stake')):.2f}u",
            'Win %': f'{prob * 100:.1f}%' if prob is not None else '—',
            'Break-even %': f'{be:.1f}%' if be is not None else '—',
            'Edge': f'{edge:+.1f} pp' if edge is not None else '—',
            'Result': r.get('result', 'Pending'),
            'P/L': f'{p:+.2f}u' if p is not None else '—',
            'Time (PT)': r.get('start_pt') or '—',
            'Notes': r.get('notes') or '',
        }

        rows.append(row)

    return pd.DataFrame(rows)

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
