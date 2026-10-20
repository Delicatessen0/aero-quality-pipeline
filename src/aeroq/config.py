"""Central configuration for the AeroQ pipeline.

All paths and connection strings are resolved from environment variables so the
same code runs locally (SQLite) and against SQL Server / Azure SQL / Databricks
SQL in a real deployment, e.g.:

    AEROQ_DB_URL="mssql+pyodbc://user:pass@server/aeroq?driver=ODBC+Driver+18+for+SQL+Server"
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    raw_dir: Path = field(default_factory=lambda: Path(os.getenv("AEROQ_RAW_DIR", PROJECT_ROOT / "data" / "raw")))
    db_url: str = field(default_factory=lambda: os.getenv("AEROQ_DB_URL", f"sqlite:///{PROJECT_ROOT / 'data' / 'warehouse.db'}"))
    reports_dir: Path = field(default_factory=lambda: Path(os.getenv("AEROQ_REPORTS_DIR", PROJECT_ROOT / "reports")))
    seed: int = int(os.getenv("AEROQ_SEED", "42"))
    # Data-quality gate: the pipeline fails if any rule with severity="error" fails.
    fail_on_error: bool = os.getenv("AEROQ_FAIL_ON_ERROR", "1") == "1"


settings = Settings()
