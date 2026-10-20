"""Reusable, documented analytics library for self-service quality analytics.

Every function takes a SQLAlchemy engine (or connection URL) and returns a
tidy DataFrame, so the same calls back the API, the dashboard, notebooks,
and Tableau/Power BI extracts.

>>> from aeroq import analytics
>>> analytics.first_pass_yield(engine, by="program")
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

_GROUPS = {
    "program": "w.program",
    "work_center": "wc.work_center_name",
    "part": "p.description",
    "material": "p.material",
    "month": "(d.year * 100 + d.month)",  # yyyymm, portable across SQL dialects
}


def _q(engine: Engine, sql: str, **params) -> pd.DataFrame:
    with engine.connect() as c:
        return pd.read_sql(text(sql), c, params=params)


def first_pass_yield(engine: Engine, by: str = "program") -> pd.DataFrame:
    """First-pass yield (share of work orders with zero failed inspections)."""
    g = _GROUPS[by]
    return _q(engine, f"""
        SELECT {g} AS {by},
               COUNT(*) AS work_orders,
               ROUND(AVG(CASE WHEN w.first_pass THEN 1.0 ELSE 0.0 END), 4) AS fpy
        FROM fact_work_order w
        JOIN dim_part p ON p.part_key = w.part_key
        JOIN dim_work_center wc ON wc.work_center_key = w.work_center_key
        JOIN dim_date d ON d.date_key = w.opened_date_key
        GROUP BY {g} ORDER BY {by}
    """)


def defect_pareto(engine: Engine, top: int = 10) -> pd.DataFrame:
    """Defect counts by code with cumulative % (classic 80/20 Pareto)."""
    df = _q(engine, """
        SELECT defect_code, defect_desc, COUNT(*) AS defects
        FROM fact_inspection WHERE NOT passed
        GROUP BY defect_code, defect_desc ORDER BY defects DESC
    """).head(top)
    df["cum_pct"] = (df["defects"].cumsum() / df["defects"].sum()).round(4)
    return df


def weekly_defect_rate(engine: Engine) -> pd.DataFrame:
    """Inspections, defects, and defect rate per work center per ISO week."""
    df = _q(engine, """
        SELECT wc.work_center_name AS work_center,
               d.year, d.iso_week,
               COUNT(*) AS inspections,
               SUM(CASE WHEN i.passed THEN 0 ELSE 1 END) AS defects
        FROM fact_inspection i
        JOIN fact_work_order w ON w.work_order_id = i.work_order_id
        JOIN dim_work_center wc ON wc.work_center_key = w.work_center_key
        JOIN dim_date d ON d.date_key = i.inspected_date_key
        GROUP BY wc.work_center_name, d.year, d.iso_week
        ORDER BY work_center, d.year, d.iso_week
    """)
    df["week"] = df["year"].astype(str) + "-W" + df["iso_week"].astype(str).str.zfill(2)
    df["defect_rate"] = (df["defects"] / df["inspections"]).round(4)
    return df


def process_capability(engine: Engine, characteristic: str | None = None) -> pd.DataFrame:
    """Cp / Cpk per characteristic: min(USL-μ, μ-LSL) / 3σ. Cpk ≥ 1.33 is the usual aerospace target."""
    df = _q(engine, "SELECT characteristic, measured_value, lsl, usl FROM fact_inspection WHERE characteristic != 'visual'")
    if characteristic:
        df = df[df["characteristic"] == characteristic]
    rows = []
    for ch, g in df.groupby("characteristic"):
        mu, sd = g["measured_value"].mean(), g["measured_value"].std(ddof=1)
        lsl, usl = g["lsl"].iloc[0], g["usl"].iloc[0]
        rows.append({
            "characteristic": ch, "n": len(g), "mean": round(mu, 4), "std": round(sd, 4),
            "cp": round((usl - lsl) / (6 * sd), 3), "cpk": round(min(usl - mu, mu - lsl) / (3 * sd), 3),
        })
    return pd.DataFrame(rows)


def cycle_time_stats(engine: Engine) -> pd.DataFrame:
    """Cycle-time distribution vs. standard hours by work center."""
    df = _q(engine, """
        SELECT wc.work_center_name AS work_center, w.cycle_hours
        FROM fact_work_order w JOIN dim_work_center wc ON wc.work_center_key = w.work_center_key
        WHERE w.cycle_hours IS NOT NULL
    """)
    return (
        df.groupby("work_center")["cycle_hours"]
        .agg(n="count", mean="mean", p50="median", p90=lambda s: np.percentile(s, 90))
        .round(2).reset_index()
    )


def disposition_mix(engine: Engine) -> pd.DataFrame:
    """Share of nonconformances by disposition and severity (cost-of-quality proxy)."""
    return _q(engine, """
        SELECT disposition, severity, COUNT(*) AS n
        FROM fact_inspection WHERE NOT passed
        GROUP BY disposition, severity ORDER BY n DESC
    """)


def kpis(engine: Engine) -> dict:
    """Headline KPIs for the dashboard banner."""
    r = _q(engine, """
        SELECT (SELECT COUNT(*) FROM fact_work_order) AS work_orders,
               (SELECT COUNT(*) FROM fact_inspection) AS inspections,
               (SELECT AVG(CASE WHEN first_pass THEN 1.0 ELSE 0.0 END) FROM fact_work_order) AS fpy,
               (SELECT AVG(CASE WHEN passed THEN 0.0 ELSE 1.0 END) FROM fact_inspection) AS defect_rate,
               (SELECT COUNT(*) FROM fact_inspection WHERE disposition = 'SCRAP') AS scrapped
    """).iloc[0]
    rates = {"fpy", "defect_rate"}
    return {k: (round(float(v), 4) if k in rates else int(v)) for k, v in r.items()}
