"""Data governance helpers: classification, PII masking, and column-level access.

Every column that leaves the raw zone carries a classification. Personally
identifiable data (inspector names) is replaced with a salted, irreversible
hash before it reaches the warehouse, so analysts can still group by inspector
without seeing who it is. The API applies role-based column filtering on top.
"""
from __future__ import annotations

import hashlib
import os

import pandas as pd

SALT = os.getenv("AEROQ_PII_SALT", "local-dev-salt")  # injected from a secret store in prod

CLASSIFICATION: dict[str, dict[str, str]] = {
    "fact_inspection": {
        "inspector_hash": "PII-MASKED",
        "measured_value": "CUI",  # Controlled Unclassified — engineering data
        "defect_code": "INTERNAL",
        "disposition": "INTERNAL",
    },
    "fact_work_order": {
        "serial_number": "CUI",
        "program": "INTERNAL",
    },
    "fact_sensor_reading": {"value": "CUI"},
}

# Columns each role may NOT see through the API
ROLE_DENY: dict[str, set[str]] = {
    "engineer": set(),
    "analyst": {"serial_number"},
    "external": {"serial_number", "measured_value", "inspector_hash", "program"},
}


def mask(value: str | None) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "UNKNOWN"
    return hashlib.sha256(f"{SALT}:{value}".encode()).hexdigest()[:16]


def mask_column(df: pd.DataFrame, col: str, new_col: str | None = None) -> pd.DataFrame:
    df = df.copy()
    df[new_col or col] = df[col].map(mask)
    if new_col and new_col != col:
        df = df.drop(columns=[col])
    return df


def filter_columns(records: list[dict], role: str) -> list[dict]:
    deny = ROLE_DENY.get(role, ROLE_DENY["external"])
    return [{k: v for k, v in r.items() if k not in deny} for r in records]
