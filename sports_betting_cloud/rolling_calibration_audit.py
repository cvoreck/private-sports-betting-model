"""Read-only rolling-origin probability calibration stability audit.

Uses four date-grouped chronological OOS prediction folds. For each fold after
the first, selects a temperature using ONLY earlier OOS folds and evaluates
the untouched next fold. No production changes, odds API calls or DB writes.
"""
import json
import logging
import math
import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from weekly_trainer import SPORTS, dataset, predict, train_candidate

LOG = logging.getLogger("rolling_calibration")
EPS = 1e-9
TEMPERATURES = [round(0.5 + 0.05*i,2) for i in range(41)]

def clip(p):
    return min(1-EPS,max(EPS,float(p)))

def log_loss(y,p):
    return sum(-v*math.log(clip(q))-(1-v)*math.log(1-clip(q))
               for v,q in zip(y,p))/len(y)

def brier(y,p):
    return sum((v-q)**2 for v,q in zip(y,p))/len(y)

def temp_scale(p,t):
    p=clip(p)
    logit=math.log(p/(1-p))/t
    return 1/(1+math.exp(-max(-30,min(30,logit))))

def calibration_bins(y,p):
    bins=[]
    for i in range(10):
        pairs=[(v,q) for v,q in zip(y,p) if min(9,int(q*10))==i]
        if pairs:
            n=len(pairs)
            bins.append({"bin":i,"games":n,
                         "predicted":sum(q for v,q in pairs)/n,
                         "observed":sum(v for v,q in pairs)/n})
    ece=sum(b["games"]*abs(b["predicted"]-b["observed"]) for b in bins)/len(y)
    return ece,bins

def summary(y,p):
    ece,bins=calibration_bins(y,p)
    return {"games":len(y),"log_loss":log_loss(y,p),
            "brier_score":brier(y,p),"ece_10_bins":ece,
            "calibration_bins":bins}

def boundaries(rows):
    n=len(rows)
    targets=[int(n*(0.4+0.6*i/4)) for i in range(5)]
    targets[-1]=n
    for i in range(1,4):
        k=targets[i]
        while k<n and rows[k-1][0].date()==rows[k][0].date():
            k+=1
        targets[i]=k
    return targets

def select_temperature(y,p):
    return min(TEMPERATURES,
               key=lambda t:(log_loss(y,[temp_scale(q,t) for q in p]),abs(t-1)))

def audit_sport(rows,sport):
    data,unique_games=dataset(rows,sport)
    data=sorted(data,key=lambda r:r[0])
    cut=boundaries(data)
    oos=[]
    for i in range(4):
        start,end=cut[i],cut[i+1]
        train,test=data[:start],data[start:end]
        if len(train)<200 or len(test)<25:
            return {"sport":sport,"status":"insufficient_data",
                    "pregame_examples":len(data)}
        if train[-1][0].date()>=test[0][0].date():
            raise ValueError(f"{sport}: train/test dates overlap")
        weights=train_candidate(train,sport,{})
        oos.append({"fold":i+1,"start":test[0][0].isoformat(),
                    "end":test[-1][0].isoformat(),
                    "y":[int(r[3]) for r in test],
                    "p":[clip(q) for q in predict(weights,test)]})
        LOG.info("%s OOS fold %d: %d games",sport,i+1,len(test))
    windows=[]
    all_y,all_raw,all_cal=[],[],[]
    for i in range(1,len(oos)):
        history=oos[:i]
        cal_y=[v for f in history for v in f["y"]]
        cal_p=[v for f in history for v in f["p"]]
        t=select_temperature(cal_y,cal_p)
        target=oos[i]
        y,p=target["y"],target["p"]
        adjusted=[temp_scale(q,t) for q in p]
        raw,cal=summary(y,p),summary(y,adjusted)
        windows.append({"test_fold":target["fold"],
                        "calibration_fit_games":len(cal_y),
                        "calibration_fit_end":history[-1]["end"],
                        "test_start":target["start"],"test_end":target["end"],
                        "temperature":t,
                        "raw":{k:v for k,v in raw.items() if k!="calibration_bins"},
                        "calibrated":{k:v for k,v in cal.items() if k!="calibration_bins"},
                        "logloss_gain":raw["log_loss"]-cal["log_loss"],
                        "brier_gain":raw["brier_score"]-cal["brier_score"],
                        "ece_gain":raw["ece_10_bins"]-cal["ece_10_bins"]})
        all_y.extend(y)
        all_raw.extend(p)
        all_cal.extend(adjusted)
    pooled_raw=summary(all_y,all_raw)
    pooled_cal=summary(all_y,all_cal)
    both=sum(w["logloss_gain"]>0 and w["brier_gain"]>0 for w in windows)
    # A strict manual-review flag, NOT a deployment decision.
    stable=(both==len(windows) and
            pooled_cal["log_loss"]<pooled_raw["log_loss"] and
            pooled_cal["brier_score"]<pooled_raw["brier_score"])
    return {"sport":sport,"status":"evaluated",
            "archived_unique_games":unique_games,
            "pregame_examples":len(data),
            "rolling_test_games":len(all_y),
            "rolling_windows":windows,
            "windows_better_on_both":both,
            "windows_total":len(windows),
            "pooled_raw":pooled_raw,"pooled_calibrated":pooled_cal,
            "stable_for_manual_review":stable,
            "deployment_authorized":False}

def main():
    logging.basicConfig(level=logging.INFO,format="%(levelname)s %(message)s")
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is required")
    output={"generated_at":datetime.now(timezone.utc).isoformat(),
            "method":"4 date-grouped chronological OOS folds; fit temperature on prior OOS folds; evaluate next 3 OOS folds",
            "limitations":[
                "No historical pregame sportsbook odds; cannot test market edge, returns, or unit-staking effectiveness.",
                "Moneyline home-win outcome only. Spreads and totals are not evaluated.",
                "College-sport AP Top 25 eligibility is not reconstructed historically.",
                "Re-trained candidate predictions may differ from currently promoted production models.",
                "Rolling folds share historical training examples; window results are not independent.",
                "This is a diagnostic report only: no deployment, database writes, or production changes."
            ],"sports":[]}
    with psycopg.connect(os.environ["DATABASE_URL"],row_factory=dict_row,
                         connect_timeout=20) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            for sport in SPORTS:
                rows=conn.execute("""SELECT event_id,payload FROM historical_snapshots
                    WHERE source='espn' AND sport=%s AND category='game'
                    ORDER BY collected_at DESC LIMIT 30000""",(sport,)).fetchall()
                output["sports"].append(audit_sport(rows,sport))
    path="sports_edge_rolling_calibration_report.json"
    with open(path,"w",encoding="utf-8") as f:
        json.dump(output,f,indent=2)
    for sport in output["sports"]:
        if sport["status"]=="evaluated":
            LOG.info("%s: stable=%s windows_better=%d/%d",
                     sport["sport"],sport["stable_for_manual_review"],
                     sport["windows_better_on_both"],sport["windows_total"])
    LOG.info("Report written: %s",path)

if __name__=="__main__":
    main()
