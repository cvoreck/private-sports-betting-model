from storage import profit

def stats(df):
    s=df[df.result.isin(['Win','Loss','Push','Void'])]
    w=int((s.result=='Win').sum()); l=int((s.result=='Loss').sum()); p=int((s.result=='Push').sum())
    net=0.0; risk=0.0; unknown=0
    for r in s.itertuples():
        v=profit(r.result,r.odds,r.stake)
        if v is None: unknown+=1
        else: net+=v
        if r.result in ('Win','Loss','Push'): risk+=r.stake
    return dict(w=w,l=l,p=p,net=net,risk=risk,roi=(100*net/risk if risk and not unknown else None),unknown=unknown)

def format_record(s): return f"{s['w']}–{s['l']}–{s['p']}"
