"""Read-only temperature-calibration audit using strictly earlier out-of-sample folds.

No production model changes, sportsbook requests, or database writes.
Run from repository root: python sports_betting_cloud/calibration_audit.py
"""
import json
import logging
import math
import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from weekly_trainer import SPORTS, dataset, predict, train_candidate

LOG = logging.getLogger("calibration_audit")
EPS = 1e-9
TEMPERATURES = [round(0.5 + i * 0.05, 2) for i in range(41)]  # 0.50 to 2.50

def clip(p):
    return min(1 - EPS, max(EPS, float(p)))

def logloss(ys, ps):
    return sum(-y * math.log(clip(p)) - (1-y)*math.log(1-clip(p))
               for y,p in zip(ys,ps)) / len(ys)

def brier(ys, ps):
    return sum((p-y)**2 for y,p in zip(ys,ps)) / len(ys)

def temperature_scale(p, t):
    p = clip(p)
    logit = math.log(p/(1-p)) / t
    return 1/(1+math.exp(-max(-30,min(30,logit))))

def bins_ece(ys, ps):
    n=len(ys)
    bins=[]
    for k in range(10):
        vals=[(y,p) for y,p in zip(ys,ps) if min(9,int(p*10)) == k]
        if vals:
            count=len(vals)
            bins.append({"range":[k/10,(k+1)/10], "count":count,
                         "predicted":sum(p for y,p in vals)/count,
                         "observed":sum(y for y,p in vals)/count})
    return sum(b["count"]*abs(b["predicted"]-b["observed"]) for b in bins)/n, bins

def summarize(ys, ps):
    ece, bins = bins_ece(ys,ps)
    return {"games":len(ys),"log_loss":logloss(ys,ps),
            "brier_score":brier(ys,ps),"expected_calibration_error":ece,
            "calibration_bins":bins}

def date_boundaries(data):
    n=len(data)
    out=[]
    for i in range(5):
        target=int(n*(0.4+0.6*i/4))
        if i==4:
            out.append(n)
            continue
        while 0 < target < n and data[target-1][0].date()==data[target][0].date():
            target+=1
        out.append(target)
    return out

def evaluate(rows, sport):
    data, unique_games = dataset(rows,sport)
    data=sorted(data,key=lambda r:r[0])
    bounds=date_boundaries(data)
    folds=[]
    for i in range(4):
        start,end=bounds[i:i+2]
        train,test=data[:start],data[start:end]
        if len(train)<200 or len(test)<25:
            return {"sport":sport,"status":"insufficient_data"}
        if train[-1][0].date() >= test[0][0].date():
            raise ValueError("Train/test dates overlap")
        weights=train_candidate(train,sport,{})
        ps=[clip(p) for p in predict(weights,test)]
        ys=[int(r[3]) for r in test]
        folds.append({"ys":ys,"ps":ps,"start":test[0][0].isoformat(),
                      "end":test[-1][0].isoformat()})
        LOG.info("%s fold %s: %s predictions",sport,i+1,len(ys))
    # Calibrator uses only OOS predictions from earlier three folds.
    cal_y=sum((f["ys"] for f in folds[:3]),[])
    cal_p=sum((f["ps"] for f in folds[:3]),[])
    candidates=[(logloss(cal_y,[temperature_scale(p,t) for p in cal_p]),t)
                for t in TEMPERATURES]
    _,best_t=min(candidates)
    final=folds[3]
    test_y,test_p=final["ys"],final["ps"]
    adjusted=[temperature_scale(p,best_t) for p in test_p]
    raw=summarize(test_y,test_p)
    calibrated=summarize(test_y,adjusted)
    improvement=raw["log_loss"]-calibrated["log_loss"]
    # Conservative gate: no automatic promotion; calibration must improve log loss
    # and Brier score on untouched final fold to warrant manual review.
    return {"sport":sport,"status":"evaluated","archived_unique_games":unique_games,
            "calibration_training_games":len(cal_y),
            "calibration_training_period":[folds[0]["start"],folds[2]["end"]],
            "untouched_final_test_period":[final["start"],final["end"]],
            "temperature":best_t,"raw":raw,"calibrated":calibrated,
            "final_fold_logloss_improvement":improvement,
            "final_fold_brier_improvement":raw["brier_score"]-calibrated["brier_score"],
            "passes_review_gate":improvement>0 and calibrated["brier_score"]<raw["brier_score"],
            "notes":"Diagnostic only. Final fold never used to fit temperature. Earlier OOS folds may overlap later model training, but not the final test labels."}

def main():
    logging.basicConfig(level=logging.INFO,format="%(levelname)s %(message)s")
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL secret is required")
    report={"generated_at":datetime.now(timezone.utc).isoformat(),
            "method":"temperature scaling fit on first 3 out-of-sample folds; untouched fourth-fold evaluation",
            "limitations":["No historical sportsbook prices or betting ROI.",
                           "Calibration applies to game winner home-win predictions, not spread/total or edge probabilities.",
                           "Candidate retraining does not establish performance of currently promoted models.",
                           "No calibrators are deployed or saved to the database."],
            "sports":[]}
    with psycopg.connect(os.environ["DATABASE_URL"],row_factory=dict_row) as conn:
        for sport in SPORTS:
            LOG.info("Loading %s",sport)
            rows=conn.execute("""SELECT event_id,payload FROM historical_snapshots
                WHERE source='espn' AND sport=%s AND category='game'
                ORDER BY collected_at DESC LIMIT 30000""",(sport,)).fetchall()
            report["sports"].append(evaluate(rows,sport))
    with open("sports_edge_calibration_audit.json","w",encoding="utf-8") as f:
        json.dump(report,f,indent=2)
    for s in report["sports"]:
        if s["status"]=="evaluated":
            LOG.info("%s: temperature=%.2f raw=%.5f calibrated=%.5f gate=%s",
                     s["sport"],s["temperature"],s["raw"]["log_loss"],
                     s["calibrated"]["log_loss"],s["passes_review_gate"])
    LOG.info("Saved sports_edge_calibration_audit.json")

if __name__=="__main__":
    main()
