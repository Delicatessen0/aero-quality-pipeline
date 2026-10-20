"""Lightweight declarative data-quality framework.

Rules are plain Python objects, so they are easy to review in a PR and reuse
across datasets. Results are written to ``dq_result`` for trend monitoring and
the pipeline fails fast if any ``error``-severity rule fails.

    suite = Suite("fact_inspection", [
        not_null("inspection_id"),
        unique("inspection_id"),
        in_range("measured_value", 0, 100),
    ])
    results = suite.run(df)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

import pandas as pd
from sqlalchemy.engine import Engine

from .models import DQResult


@dataclass
class Rule:
    name: str
    check: Callable[[pd.DataFrame], pd.Series]  # returns boolean Series, True = row FAILS
    severity: str = "error"  # "error" | "warn"


@dataclass
class RuleResult:
    dataset: str
    rule: str
    severity: str
    passed: bool
    failed_rows: int
    total_rows: int


class DataQualityError(RuntimeError):
    pass


# ----------------------------- rule factory -----------------------------
def not_null(col: str, severity: str = "error") -> Rule:
    return Rule(f"not_null({col})", lambda d: d[col].isna(), severity)


def unique(col: str, severity: str = "error") -> Rule:
    return Rule(f"unique({col})", lambda d: d[col].duplicated(keep=False), severity)


def in_range(col: str, lo: float, hi: float, severity: str = "error") -> Rule:
    return Rule(f"in_range({col},{lo},{hi})", lambda d: d[col].notna() & ~d[col].between(lo, hi), severity)


def in_set(col: str, allowed: Iterable, severity: str = "error") -> Rule:
    allowed = set(allowed)
    return Rule(f"in_set({col})", lambda d: d[col].notna() & ~d[col].isin(allowed), severity)


def foreign_key(col: str, parent: pd.Series, severity: str = "error") -> Rule:
    keys = set(parent)
    return Rule(f"fk({col})", lambda d: ~d[col].isin(keys), severity)


def expression(name: str, fails: Callable[[pd.DataFrame], pd.Series], severity: str = "warn") -> Rule:
    return Rule(name, fails, severity)


# -------------------------------- suite --------------------------------
@dataclass
class Suite:
    dataset: str
    rules: list[Rule]

    def run(self, df: pd.DataFrame) -> list[RuleResult]:
        out = []
        for r in self.rules:
            failed = int(r.check(df).sum())
            out.append(RuleResult(self.dataset, r.name, r.severity, failed == 0, failed, len(df)))
        return out


def default_suites(t: dict[str, pd.DataFrame]) -> list[tuple[Suite, pd.DataFrame]]:
    wo, insp, sens = t["fact_work_order"], t["fact_inspection"], t["fact_sensor_reading"]
    return [
        (
            Suite("fact_work_order", [
                not_null("work_order_id"),
                unique("work_order_id"),
                not_null("part_key"),
                not_null("work_center_key"),
                in_set("status", {"CLOSED", "IN_WORK"}),
                in_range("qty", 1, 1000),
                expression("closed_after_opened", lambda d: d["cycle_hours"].notna() & (d["cycle_hours"] < 0), "error"),
                expression("closed_wo_has_close_date", lambda d: (d["status"] == "CLOSED") & d["closed_at"].isna(), "warn"),
            ]),
            wo,
        ),
        (
            Suite("fact_inspection", [
                not_null("inspection_id"),
                unique("inspection_id"),
                foreign_key("work_order_id", wo["work_order_id"]),
                in_range("measured_value", 0, 50),
                expression("fail_has_defect_code", lambda d: (~d["passed"]) & d["defect_code"].isna(), "error"),
                expression("lsl_below_usl", lambda d: d["lsl"] >= d["usl"], "error"),
                expression("out_of_spec_marked_pass", lambda d: d["passed"] & ~d["measured_value"].between(d["lsl"], d["usl"]), "warn"),
            ]),
            insp,
        ),
        (
            Suite("fact_sensor_reading", [
                foreign_key("work_order_id", wo["work_order_id"]),
                expression("temp_physical_range", lambda d: (d["sensor"] == "autoclave_temp_c") & ~d["value"].between(0, 400), "error"),
            ]),
            sens,
        ),
        (
            Suite("quarantine_inspection", [
                expression("orphan_inspections_present", lambda d: pd.Series([True] * len(d), dtype=bool), "warn"),
            ]),
            t["quarantine_inspection"],
        ),
    ]


def run_checks(tables: dict[str, pd.DataFrame]) -> list[RuleResult]:
    results: list[RuleResult] = []
    for suite, df in default_suites(tables):
        results.extend(suite.run(df))
    return results


def persist(engine: Engine, run_id: str, results: list[RuleResult]) -> None:
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            DQResult.__table__.insert(),
            [dict(run_id=run_id, run_at=now, **r.__dict__) for r in results],
        )


def gate(results: list[RuleResult]) -> None:
    failures = [r for r in results if not r.passed and r.severity == "error"]
    if failures:
        msg = "; ".join(f"{r.dataset}.{r.rule} ({r.failed_rows} rows)" for r in failures)
        raise DataQualityError(f"Data-quality gate failed: {msg}")
