"""FastAPI service: governed, ad-hoc query access to the quality warehouse.

    uvicorn aeroq.api:app --reload     # docs at http://localhost:8000/docs

Callers identify a role with the ``X-Role`` header (engineer | analyst |
external). In production this would come from Azure AD / Entra ID claims;
column-level filtering is applied from ``governance.ROLE_DENY``.
"""
from __future__ import annotations

from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import text

from . import analytics, governance
from .models import get_engine

app = FastAPI(
    title="AeroQ Quality Data API",
    version="1.0.0",
    description="Self-service access to manufacturing quality KPIs, nonconformances, and anomaly events.",
)
_engine = None


def engine():
    global _engine
    if _engine is None:
        _engine = get_engine()
    return _engine


def role(x_role: str = Header("external")) -> str:
    if x_role not in governance.ROLE_DENY:
        raise HTTPException(403, f"Unknown role '{x_role}'")
    return x_role


def _df(df):
    return df.astype(object).where(df.notna(), None).to_dict("records")


@app.get("/health")
def health():
    with engine().connect() as c:
        c.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/kpis")
def kpis():
    return analytics.kpis(engine())


@app.get("/metrics/fpy")
def fpy(by: Literal["program", "work_center", "part", "material", "month"] = "program"):
    return _df(analytics.first_pass_yield(engine(), by))


@app.get("/metrics/pareto")
def pareto(top: int = Query(10, ge=1, le=50)):
    return _df(analytics.defect_pareto(engine(), top))


@app.get("/metrics/capability")
def capability(characteristic: str | None = None):
    return _df(analytics.process_capability(engine(), characteristic))


@app.get("/anomalies")
def anomalies(method: str | None = None, limit: int = Query(50, ge=1, le=500)):
    sql = "SELECT method, entity, period, metric, value, threshold, detail FROM anomaly_event"
    params = {"limit": limit}
    if method:
        sql += " WHERE method = :method"
        params["method"] = method
    sql += " ORDER BY value DESC LIMIT :limit"
    with engine().connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), params)]


@app.get("/nonconformances")
def nonconformances(
    program: str | None = None,
    severity: Literal["MINOR", "MAJOR", "CRITICAL"] | None = None,
    limit: int = Query(100, ge=1, le=1000),
    r: str = Depends(role),
):
    """Failed inspections joined to work-order context. Columns filtered by role."""
    sql = """
        SELECT i.inspection_id, i.work_order_id, w.program, w.serial_number, p.part_number,
               i.characteristic, i.measured_value, i.defect_code, i.defect_desc,
               i.severity, i.disposition, i.inspector_hash, i.inspected_at
        FROM fact_inspection i
        JOIN fact_work_order w ON w.work_order_id = i.work_order_id
        JOIN dim_part p ON p.part_key = w.part_key
        WHERE NOT i.passed
    """
    params: dict = {"limit": limit}
    if program:
        sql += " AND w.program = :program"
        params["program"] = program.upper()
    if severity:
        sql += " AND i.severity = :severity"
        params["severity"] = severity
    sql += " ORDER BY i.inspected_at DESC LIMIT :limit"
    with engine().connect() as c:
        rows = [dict(x._mapping) for x in c.execute(text(sql), params)]
    return governance.filter_columns(rows, r)


@app.get("/dq/latest")
def dq_latest():
    sql = """SELECT dataset, rule, severity, passed, failed_rows, total_rows FROM dq_result
             WHERE run_id = (SELECT run_id FROM dq_result ORDER BY run_at DESC LIMIT 1)"""
    with engine().connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql))]
