"""Pipeline runner: a small dependency-ordered DAG with logging and timing.

    python -m aeroq.pipeline            # generate -> extract -> transform -> DQ -> load -> anomaly -> report
    python -m aeroq.pipeline --no-generate

The same steps are exposed as Dagster software-defined assets in
``orchestration/dagster_defs.py`` for scheduled production runs.
"""
from __future__ import annotations

import argparse
import logging
import time
import uuid
from datetime import datetime, timezone

from . import anomaly, etl, generate, quality, report
from .config import settings
from .models import create_schema, get_engine

log = logging.getLogger("aeroq")


def run(regenerate: bool = True, db_url: str | None = None) -> dict:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    engine = get_engine(db_url)
    timings: dict[str, float] = {}

    def step(name, fn, *a, **kw):
        t0 = time.perf_counter()
        out = fn(*a, **kw)
        timings[name] = round(time.perf_counter() - t0, 2)
        log.info("✓ %-10s %6.2fs", name, timings[name])
        return out

    log.info("run %s -> %s", run_id, engine.url.render_as_string(hide_password=True))
    if regenerate:
        step("generate", generate.generate)
    create_schema(engine, drop=True)
    raw = step("extract", etl.extract)
    tables = step("transform", etl.transform, raw)
    results = step("quality", quality.run_checks, tables)
    quality.persist(engine, run_id, results)
    for r in results:
        log.info("  DQ %-5s %-22s %-38s failed=%d/%d", "PASS" if r.passed else r.severity.upper(),
                 r.dataset, r.rule, r.failed_rows, r.total_rows)
    if settings.fail_on_error:
        quality.gate(results)
    counts = step("load", etl.load, engine, tables)
    log.info("  loaded %s", counts)
    found = step("anomaly", anomaly.detect, engine, run_id)
    log.info("  anomalies: %d events", len(found["events"]))
    path = step("report", report.build_dashboard, engine, found, results, run_id)
    log.info("dashboard -> %s", path)
    return {"run_id": run_id, "counts": counts, "timings": timings, "dashboard": str(path),
            "anomalies": len(found["events"]), "quarantined": len(tables["quarantine_inspection"]) + len(tables["quarantine_sensor"])}


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the AeroQ ETL pipeline")
    ap.add_argument("--no-generate", action="store_true", help="reuse existing raw files")
    ap.add_argument("--db-url", default=None)
    a = ap.parse_args()
    run(regenerate=not a.no_generate, db_url=a.db_url)


if __name__ == "__main__":
    main()
