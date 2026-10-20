"""Dagster software-defined assets wrapping the AeroQ pipeline.

    pip install dagster dagster-webserver
    dagster dev -f orchestration/dagster_defs.py

Each pipeline stage becomes an asset with explicit lineage, so the Dagster UI
shows the full graph (raw files -> cleaned tables -> DQ gate -> warehouse ->
anomalies -> dashboard) and re-materializes only what is stale. A daily
schedule runs the graph at 05:00 Pacific, ahead of first shift.
"""

import uuid

from dagster import (
    AssetCheckResult,
    AssetExecutionContext,
    Definitions,
    MetadataValue,
    ScheduleDefinition,
    asset,
    asset_check,
    define_asset_job,
)

from aeroq import anomaly, etl, generate, quality, report
from aeroq.models import create_schema, get_engine


@asset(group_name="raw", description="Source extracts from MES, QMS and the autoclave historian.")
def raw_sources(context: AssetExecutionContext) -> dict:
    paths = generate.generate()
    context.add_output_metadata({k: MetadataValue.path(str(v)) for k, v in paths.items()})
    return etl.extract()


@asset(group_name="staging", description="Cleaned, conformed, PII-masked star-schema tables.")
def conformed_tables(context: AssetExecutionContext, raw_sources: dict) -> dict:
    t = etl.transform(raw_sources)
    context.add_output_metadata({
        "rows_fact_inspection": len(t["fact_inspection"]),
        "quarantined": len(t["quarantine_inspection"]) + len(t["quarantine_sensor"]),
    })
    return t


@asset_check(asset=conformed_tables, blocking=True, description="Error-severity data-quality rules must pass.")
def dq_gate(conformed_tables: dict) -> AssetCheckResult:
    results = quality.run_checks(conformed_tables)
    errors = [r for r in results if not r.passed and r.severity == "error"]
    return AssetCheckResult(
        passed=not errors,
        metadata={"rules": len(results), "errors": len(errors), "warnings": sum(1 for r in results if not r.passed and r.severity == "warn")},
    )


@asset(group_name="warehouse", description="Star schema loaded into the SQL warehouse (SQL Server / Azure SQL / SQLite).")
def warehouse(context: AssetExecutionContext, conformed_tables: dict) -> str:
    engine = get_engine()
    create_schema(engine, drop=True)
    run_id = context.run_id or uuid.uuid4().hex
    quality.persist(engine, run_id, quality.run_checks(conformed_tables))
    counts = etl.load(engine, conformed_tables)
    context.add_output_metadata(counts)
    return run_id


@asset(group_name="analytics", description="SPC + Isolation Forest anomaly events.")
def anomaly_events(context: AssetExecutionContext, warehouse: str) -> dict:
    found = anomaly.detect(get_engine(), warehouse)
    context.add_output_metadata({"events": len(found["events"])})
    return found


@asset(group_name="analytics", description="Stakeholder HTML dashboard + Tableau/Power BI CSV extracts.")
def dashboard(anomaly_events: dict, warehouse: str, conformed_tables: dict) -> str:
    return str(report.build_dashboard(get_engine(), anomaly_events, quality.run_checks(conformed_tables), warehouse))


daily_job = define_asset_job("aeroq_daily", selection="*")

defs = Definitions(
    assets=[raw_sources, conformed_tables, warehouse, anomaly_events, dashboard],
    asset_checks=[dq_gate],
    jobs=[daily_job],
    schedules=[ScheduleDefinition(job=daily_job, cron_schedule="0 5 * * *", execution_timezone="America/Los_Angeles")],
)
