"""Applied analytics: find process shifts and outlier builds.

Two complementary methods:

1. **Statistical Process Control (p-chart)** on weekly defect rate per work
   center. Control limits come from a baseline window, so a later drift shows
   up as points above the UCL — explainable to a quality engineer.
2. **Isolation Forest** (scikit-learn) on per-work-order autoclave cure
   profiles (mean/max/slope of temperature, pressure variability). Catches
   multivariate outliers that a single-metric chart would miss.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from sqlalchemy import text
from sqlalchemy.engine import Engine

from . import analytics
from .models import AnomalyEvent


def spc_pchart(weekly: pd.DataFrame, baseline_weeks: int = 12, sigma: float = 3.0) -> pd.DataFrame:
    """Add p-bar, UCL, LCL and an ``out_of_control`` flag per work center/week."""
    out = []
    for wc, g in weekly.sort_values(["year", "iso_week"]).groupby("work_center"):
        g = g.copy()
        base = g.head(baseline_weeks)
        pbar = base["defects"].sum() / base["inspections"].sum()
        se = np.sqrt(pbar * (1 - pbar) / g["inspections"])
        g["pbar"] = round(pbar, 4)
        g["ucl"] = (pbar + sigma * se).clip(upper=1).round(4)
        g["lcl"] = (pbar - sigma * se).clip(lower=0).round(4)
        g["out_of_control"] = g["defect_rate"] > g["ucl"]
        out.append(g)
    return pd.concat(out, ignore_index=True)


def cure_profile_features(engine: Engine) -> pd.DataFrame:
    with engine.connect() as c:
        s = pd.read_sql(text("SELECT work_order_id, ts, sensor, value FROM fact_sensor_reading"), c, parse_dates=["ts"])
    if s.empty:
        return pd.DataFrame()
    s = s.sort_values("ts")
    feats = []
    for wo, g in s.groupby("work_order_id"):
        t = g[g["sensor"] == "autoclave_temp_c"]
        p = g[g["sensor"] == "autoclave_pressure_psi"]
        minutes = (t["ts"] - t["ts"].min()).dt.total_seconds() / 60
        slope = np.polyfit(minutes, t["value"], 1)[0] if len(t) > 2 else 0.0
        feats.append({
            "work_order_id": wo,
            "temp_mean": t["value"].mean(),
            "temp_max": t["value"].max(),
            "temp_slope": slope,
            "pressure_std": p["value"].std(),
            "opened_at": g["ts"].min(),
        })
    return pd.DataFrame(feats)


def isolation_forest(features: pd.DataFrame, contamination: float = 0.05, seed: int = 42) -> pd.DataFrame:
    cols = ["temp_mean", "temp_max", "temp_slope", "pressure_std"]
    X = StandardScaler().fit_transform(features[cols].fillna(0))
    model = IsolationForest(n_estimators=200, contamination=contamination, random_state=seed).fit(X)
    out = features.copy()
    out["anomaly_score"] = -model.score_samples(X)
    out["is_anomaly"] = model.predict(X) == -1
    return out


def detect(engine: Engine, run_id: str) -> dict[str, pd.DataFrame]:
    spc = spc_pchart(analytics.weekly_defect_rate(engine))
    iso = isolation_forest(cure_profile_features(engine))
    events = []
    for _, r in spc[spc["out_of_control"]].iterrows():
        events.append(dict(run_id=run_id, method="SPC p-chart", entity=r["work_center"], period=r["week"],
                           metric="defect_rate", value=float(r["defect_rate"]), threshold=float(r["ucl"]),
                           detail=f"{int(r['defects'])}/{int(r['inspections'])} failed; baseline p̄={r['pbar']:.3f}"))
    thr = float(iso.loc[iso["is_anomaly"], "anomaly_score"].min()) if iso["is_anomaly"].any() else 0.0
    for _, r in iso[iso["is_anomaly"]].iterrows():
        events.append(dict(run_id=run_id, method="IsolationForest", entity=r["work_order_id"],
                           period=r["opened_at"].strftime("%Y-%m-%d"), metric="anomaly_score",
                           value=float(r["anomaly_score"]), threshold=thr,
                           detail=f"temp_mean={r['temp_mean']:.1f}C max={r['temp_max']:.1f}C slope={r['temp_slope']:.3f}C/min"))
    if events:
        with engine.begin() as c:
            c.execute(AnomalyEvent.__table__.insert(), events)
    return {"spc": spc, "isolation_forest": iso, "events": pd.DataFrame(events)}
