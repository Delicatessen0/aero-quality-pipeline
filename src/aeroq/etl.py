"""Extract -> Transform -> Load.

Each step is a pure function over DataFrames so it can be unit-tested and
wrapped by any orchestrator (the built-in runner in ``pipeline.py`` or the
Dagster asset graph in ``orchestration/dagster_defs.py``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from sqlalchemy import delete
from sqlalchemy.engine import Engine

from . import governance
from .config import settings
from .generate import DEFECT_CODES, WORK_CENTERS
from .models import (
    DimDate,
    DimPart,
    DimWorkCenter,
    FactInspection,
    FactSensorReading,
    FactWorkOrder,
)


# =============================== EXTRACT ===============================
def extract(raw_dir: Path | None = None) -> dict[str, pd.DataFrame]:
    raw_dir = Path(raw_dir or settings.raw_dir)
    inspections = json.loads((raw_dir / "quality_inspections.json").read_text())["records"]
    return {
        "work_orders": pd.read_csv(raw_dir / "mes_work_orders.csv", dtype=str, keep_default_na=False),
        "inspections": pd.DataFrame(inspections),
        "sensors": pd.read_csv(raw_dir / "autoclave_sensors.csv"),
        "parts": pd.read_csv(raw_dir / "part_master.csv"),
    }


# ============================== TRANSFORM ==============================
def clean_work_orders(wo: pd.DataFrame) -> pd.DataFrame:
    wo = wo.drop_duplicates(subset="work_order_id", keep="last").copy()
    wo["work_center"] = wo["work_center"].str.strip().str.upper()
    wo["program"] = wo["program"].str.strip().str.upper()
    wo["qty"] = pd.to_numeric(wo["qty"], errors="coerce").fillna(1).astype(int)
    wo["opened_at"] = pd.to_datetime(wo["opened_at"])
    wo["closed_at"] = pd.to_datetime(wo["closed_at"].replace("", None), errors="coerce")
    wo["cycle_hours"] = (wo["closed_at"] - wo["opened_at"]).dt.total_seconds() / 3600
    wo["status"] = wo["status"].str.upper()
    return wo.reset_index(drop=True)


def clean_inspections(insp: pd.DataFrame, valid_wo: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (clean, quarantined). Orphans are quarantined, not dropped silently."""
    insp = insp.drop_duplicates("inspection_id").copy()
    insp["result"] = insp["result"].str.upper()
    insp["passed"] = insp["result"].eq("PASS")
    insp["inspected_at"] = pd.to_datetime(insp["inspected_at"])
    insp["defect_desc"] = insp["defect_code"].map(DEFECT_CODES)
    insp = governance.mask_column(insp, "inspector", "inspector_hash")
    orphan = ~insp["work_order_id"].isin(valid_wo)
    return insp[~orphan].reset_index(drop=True), insp[orphan].reset_index(drop=True)


def clean_sensors(s: pd.DataFrame, valid_wo: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame]:
    s = s.copy()
    s["ts"] = pd.to_datetime(s["ts"])
    physical_limits = {"autoclave_temp_c": (0, 400), "autoclave_pressure_psi": (0, 300)}
    lo = s["sensor"].map(lambda k: physical_limits.get(k, (-1e9, 1e9))[0])
    hi = s["sensor"].map(lambda k: physical_limits.get(k, (-1e9, 1e9))[1])
    bad = (s["value"] < lo) | (s["value"] > hi) | ~s["work_order_id"].isin(valid_wo)
    return s[~bad].reset_index(drop=True), s[bad].reset_index(drop=True)


def build_dim_date(dates: pd.Series) -> pd.DataFrame:
    d = pd.Series(pd.to_datetime(dates).dt.normalize().unique()).sort_values()
    rng = pd.date_range(d.min(), d.max(), freq="D")
    return pd.DataFrame(
        {
            "date_key": rng.strftime("%Y%m%d").astype(int),
            "full_date": rng.date,
            "year": rng.year,
            "quarter": rng.quarter,
            "month": rng.month,
            "iso_week": rng.isocalendar().week.astype(int).values,
            "weekday": rng.weekday,
        }
    )


def transform(raw: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    wo = clean_work_orders(raw["work_orders"])
    insp, insp_q = clean_inspections(raw["inspections"], wo["work_order_id"])
    sens, sens_q = clean_sensors(raw["sensors"], wo["work_order_id"])

    dim_part = raw["parts"].reset_index(drop=True)
    dim_part.insert(0, "part_key", range(1, len(dim_part) + 1))
    dim_wc = pd.DataFrame(
        {"work_center_code": list(WORK_CENTERS), "work_center_name": list(WORK_CENTERS.values())}
    )
    dim_wc.insert(0, "work_center_key", range(1, len(dim_wc) + 1))
    dim_date = build_dim_date(pd.concat([wo["opened_at"], insp["inspected_at"]]))

    # first-pass yield flag: work order had zero failed inspections
    failed_wo = set(insp.loc[~insp["passed"], "work_order_id"])
    wo["first_pass"] = ~wo["work_order_id"].isin(failed_wo)

    fact_wo = (
        wo.merge(dim_part[["part_key", "part_number"]], on="part_number", how="left")
        .merge(dim_wc[["work_center_key", "work_center_code"]], left_on="work_center", right_on="work_center_code", how="left")
        .assign(opened_date_key=lambda d: d["opened_at"].dt.strftime("%Y%m%d").astype(int))
    )[[
        "work_order_id", "program", "part_key", "work_center_key", "opened_date_key", "serial_number",
        "qty", "opened_at", "closed_at", "cycle_hours", "status", "first_pass",
    ]]
    fact_insp = insp.assign(inspected_date_key=lambda d: d["inspected_at"].dt.strftime("%Y%m%d").astype(int))[[
        "inspection_id", "work_order_id", "inspected_date_key", "inspected_at", "inspector_hash", "characteristic",
        "measured_value", "nominal", "lsl", "usl", "passed", "defect_code", "defect_desc", "severity", "disposition",
    ]]
    return {
        "dim_part": dim_part,
        "dim_work_center": dim_wc,
        "dim_date": dim_date,
        "fact_work_order": fact_wo,
        "fact_inspection": fact_insp,
        "fact_sensor_reading": sens[["work_order_id", "ts", "sensor", "value"]],
        "quarantine_inspection": insp_q,
        "quarantine_sensor": sens_q,
    }


# ================================ LOAD =================================
LOAD_ORDER = [
    (DimPart, "dim_part"),
    (DimWorkCenter, "dim_work_center"),
    (DimDate, "dim_date"),
    (FactWorkOrder, "fact_work_order"),
    (FactInspection, "fact_inspection"),
    (FactSensorReading, "fact_sensor_reading"),
]


def _records(df: pd.DataFrame) -> list[dict]:
    df = df.astype(object).where(pd.notna(df), None)
    out = df.to_dict("records")
    for r in out:
        for k, v in r.items():
            if isinstance(v, pd.Timestamp):
                r[k] = v.to_pydatetime()
    return out


def load(engine: Engine, tables: dict[str, pd.DataFrame]) -> dict[str, int]:
    """Idempotent full-refresh load inside a single transaction."""
    counts: dict[str, int] = {}
    with engine.begin() as conn:
        for model, _ in reversed(LOAD_ORDER):
            conn.execute(delete(model))
        for model, name in LOAD_ORDER:
            rows = _records(tables[name])
            for i in range(0, len(rows), 5000):
                conn.execute(model.__table__.insert(), rows[i : i + 5000])
            counts[name] = len(rows)
    return counts
