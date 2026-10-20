"""Synthetic source-system generator.

Simulates three disconnected operational systems found on an aircraft
production floor, each in a different format (the "multiple data sources" the
ETL has to reconcile):

* MES work orders           -> CSV   (think Solumina / MES export)
* Quality inspections & NCRs -> JSON  (think TIPQA / CPATS feed)
* Torque/cure sensor readings -> CSV  (machine historian export)

The data deliberately contains real-world mess: duplicate rows, inconsistent
casing, missing values, out-of-range readings, and orphaned keys, so the
transform and data-quality layers have something to catch. Inspector names are
included so the governance layer can demonstrate PII masking.

No real program, part, or personnel data is used — everything is generated.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import settings

PROGRAMS = ["UAV-A", "UAV-B", "BOMBER-SUBASSY", "SAT-BUS"]
WORK_CENTERS = {
    "WC-COMP-LAYUP": "Composite Layup",
    "WC-AUTOCLAVE": "Autoclave Cure",
    "WC-CNC-5AX": "5-Axis CNC Machining",
    "WC-DRILL-FILL": "Drill & Fastener Install",
    "WC-NDI": "Non-Destructive Inspection",
    "WC-FINAL-ASSY": "Final Assembly",
}
PARTS = [
    ("PN-1001", "Wing Skin Panel", "Composite", 2.50),
    ("PN-1002", "Spar Cap", "Titanium", 0.80),
    ("PN-1003", "Bulkhead Frame", "Aluminum 7075", 1.20),
    ("PN-1004", "Access Door", "Composite", 1.00),
    ("PN-1005", "Engine Mount Bracket", "Inconel 718", 0.50),
    ("PN-1006", "Inlet Duct Section", "Composite", 2.00),
    ("PN-1007", "Fuselage Longeron", "Aluminum 7075", 1.50),
    ("PN-1008", "Control Surface Hinge", "Titanium", 0.30),
]
DEFECT_CODES = {
    "DEL": "Delamination",
    "POR": "Porosity",
    "DIM": "Dimensional Out-of-Tolerance",
    "FOD": "Foreign Object Debris",
    "SUR": "Surface Finish",
    "FAS": "Fastener Torque",
    "DOC": "Documentation Error",
}
INSPECTORS = ["J. Alvarez", "M. Chen", "S. Patel", "R. Okafor", "K. Nguyen", "T. Brooks"]


def generate(out_dir: Path | None = None, n_work_orders: int = 2500, seed: int | None = None) -> dict[str, Path]:
    out_dir = Path(out_dir or settings.raw_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed if seed is not None else settings.seed)
    start = datetime(2026, 1, 5)

    # ---------------- MES work orders (CSV) ----------------
    wo_rows = []
    for i in range(n_work_orders):
        pn, _, material, _ = PARTS[rng.integers(len(PARTS))]
        wc = list(WORK_CENTERS)[rng.integers(len(WORK_CENTERS))]
        opened = start + timedelta(hours=float(rng.uniform(0, 24 * 270)))
        cycle_hrs = float(rng.gamma(3, 2.5 if "COMP" in wc or "AUTOCLAVE" in wc else 1.2))
        wo_rows.append(
            {
                "work_order_id": f"WO-{100000 + i}",
                "program": PROGRAMS[rng.integers(len(PROGRAMS))],
                "part_number": pn,
                "serial_number": f"SN{rng.integers(10**6, 10**7)}",
                "work_center": wc if rng.random() > 0.03 else wc.lower(),  # casing drift
                "qty": int(rng.integers(1, 6)),
                "opened_at": opened.isoformat(timespec="seconds"),
                "closed_at": (opened + timedelta(hours=cycle_hrs)).isoformat(timespec="seconds")
                if rng.random() > 0.04
                else "",  # still open / missing
                "status": "CLOSED" if rng.random() > 0.04 else "IN_WORK",
            }
        )
    wo = pd.DataFrame(wo_rows)
    wo = pd.concat([wo, wo.sample(40, random_state=1)])  # duplicate export rows

    # ---------------- Quality inspections (JSON) ----------------
    # Defect probability drifts upward on one work center mid-year to create a
    # detectable process shift for the anomaly layer.
    insp = []
    for _, r in wo.drop_duplicates("work_order_id").iterrows():
        opened = datetime.fromisoformat(r["opened_at"])
        base_p = {"Composite": 0.09, "Titanium": 0.05, "Aluminum 7075": 0.04, "Inconel 718": 0.06}[
            next(p[2] for p in PARTS if p[0] == r["part_number"])
        ]
        if r["work_center"].upper() == "WC-AUTOCLAVE" and opened > start + timedelta(days=150):
            base_p *= 3.2  # process drift (e.g., degraded thermocouple)
        n_insp = int(rng.integers(1, 4))
        for k in range(n_insp):
            defect = rng.random() < base_p
            code = rng.choice(list(DEFECT_CODES)) if defect else None
            insp.append(
                {
                    "inspection_id": f"IN-{len(insp) + 1:07d}",
                    "work_order_id": r["work_order_id"],
                    "inspected_at": (opened + timedelta(hours=float(rng.uniform(0.5, 30)))).isoformat(timespec="seconds"),
                    "inspector": INSPECTORS[rng.integers(len(INSPECTORS))],
                    "characteristic": rng.choice(["thickness_mm", "hole_dia_mm", "flatness_mm", "visual"]),
                    "measured_value": round(float(rng.normal(5.0, 0.028 if not defect else 0.18)), 4),
                    "nominal": 5.0,
                    "lsl": 4.85,
                    "usl": 5.15,
                    "result": "FAIL" if defect else ("PASS" if rng.random() > 0.01 else "pass"),
                    "defect_code": code,
                    "severity": (rng.choice(["MINOR", "MAJOR", "CRITICAL"], p=[0.7, 0.25, 0.05]) if defect else None),
                    "disposition": (rng.choice(["REWORK", "USE_AS_IS", "SCRAP", "RTV"], p=[0.55, 0.25, 0.15, 0.05]) if defect else None),
                }
            )
    # Orphan records referencing a work order that does not exist
    for j in range(12):
        insp.append({**insp[j], "inspection_id": f"IN-ORPH{j:03d}", "work_order_id": f"WO-9{j:05d}"})

    # ---------------- Sensor readings (CSV) ----------------
    sens = []
    autoclave_wos = wo[wo["work_center"].str.upper() == "WC-AUTOCLAVE"].drop_duplicates("work_order_id")
    for _, r in autoclave_wos.iterrows():
        opened = datetime.fromisoformat(r["opened_at"])
        drift = 6.0 if opened > start + timedelta(days=150) else 0.0
        for m in range(0, 240, 20):
            temp = rng.normal(177 + drift * (m / 240), 1.8)
            if rng.random() < 0.004:
                temp = 999.0  # sensor glitch
            sens.append(
                {
                    "work_order_id": r["work_order_id"],
                    "ts": (opened + timedelta(minutes=m)).isoformat(timespec="seconds"),
                    "sensor": "autoclave_temp_c",
                    "value": round(float(temp), 2),
                }
            )
            sens.append(
                {
                    "work_order_id": r["work_order_id"],
                    "ts": (opened + timedelta(minutes=m)).isoformat(timespec="seconds"),
                    "sensor": "autoclave_pressure_psi",
                    "value": round(float(rng.normal(85, 1.2)), 2),
                }
            )

    paths = {
        "work_orders": out_dir / "mes_work_orders.csv",
        "inspections": out_dir / "quality_inspections.json",
        "sensors": out_dir / "autoclave_sensors.csv",
        "parts": out_dir / "part_master.csv",
    }
    wo.to_csv(paths["work_orders"], index=False)
    paths["inspections"].write_text(json.dumps({"source": "QMS", "records": insp}, indent=0))
    pd.DataFrame(sens).to_csv(paths["sensors"], index=False)
    pd.DataFrame(PARTS, columns=["part_number", "description", "material", "std_hours"]).to_csv(paths["parts"], index=False)
    return paths


if __name__ == "__main__":
    for name, p in generate().items():
        print(f"{name:12s} -> {p}")
