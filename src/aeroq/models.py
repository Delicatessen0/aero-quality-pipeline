"""Physical data model (star schema) defined with SQLAlchemy 2.0 ORM.

                    dim_part        dim_work_center      dim_date
                        \\                |                 /
   fact_work_order ------+---------------+----------------+
          |
   fact_inspection   fact_sensor_reading

   dq_result  (data-quality audit log)     anomaly_event  (analytics output)

Dialect-agnostic: runs on SQLite locally and SQL Server / Oracle / Azure SQL by
swapping ``AEROQ_DB_URL``.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    create_engine,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import settings


class Base(DeclarativeBase):
    pass


# ----------------------------- Dimensions -----------------------------
class DimPart(Base):
    __tablename__ = "dim_part"
    part_key: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    part_number: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(100))
    material: Mapped[str] = mapped_column(String(40))
    std_hours: Mapped[float] = mapped_column(Float)


class DimWorkCenter(Base):
    __tablename__ = "dim_work_center"
    work_center_key: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_center_code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    work_center_name: Mapped[str] = mapped_column(String(60))


class DimDate(Base):
    __tablename__ = "dim_date"
    date_key: Mapped[int] = mapped_column(Integer, primary_key=True)  # yyyymmdd
    full_date: Mapped[date] = mapped_column(Date, unique=True)
    year: Mapped[int] = mapped_column(Integer)
    quarter: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    iso_week: Mapped[int] = mapped_column(Integer)
    weekday: Mapped[int] = mapped_column(Integer)


# ------------------------------- Facts --------------------------------
class FactWorkOrder(Base):
    __tablename__ = "fact_work_order"
    work_order_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    program: Mapped[str] = mapped_column(String(30), index=True)
    part_key: Mapped[int] = mapped_column(ForeignKey("dim_part.part_key"))
    work_center_key: Mapped[int] = mapped_column(ForeignKey("dim_work_center.work_center_key"))
    opened_date_key: Mapped[int] = mapped_column(ForeignKey("dim_date.date_key"))
    serial_number: Mapped[str] = mapped_column(String(20))
    qty: Mapped[int] = mapped_column(Integer)
    opened_at: Mapped[datetime] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cycle_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(12))
    first_pass: Mapped[bool] = mapped_column(Boolean)


class FactInspection(Base):
    __tablename__ = "fact_inspection"
    inspection_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    work_order_id: Mapped[str] = mapped_column(ForeignKey("fact_work_order.work_order_id"), index=True)
    inspected_date_key: Mapped[int] = mapped_column(ForeignKey("dim_date.date_key"))
    inspected_at: Mapped[datetime] = mapped_column(DateTime)
    inspector_hash: Mapped[str] = mapped_column(String(16))  # PII masked
    characteristic: Mapped[str] = mapped_column(String(30))
    measured_value: Mapped[float] = mapped_column(Float)
    nominal: Mapped[float] = mapped_column(Float)
    lsl: Mapped[float] = mapped_column(Float)
    usl: Mapped[float] = mapped_column(Float)
    passed: Mapped[bool] = mapped_column(Boolean)
    defect_code: Mapped[str | None] = mapped_column(String(5), nullable=True)
    defect_desc: Mapped[str | None] = mapped_column(String(60), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(10), nullable=True)
    disposition: Mapped[str | None] = mapped_column(String(12), nullable=True)


class FactSensorReading(Base):
    __tablename__ = "fact_sensor_reading"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_order_id: Mapped[str] = mapped_column(ForeignKey("fact_work_order.work_order_id"))
    ts: Mapped[datetime] = mapped_column(DateTime)
    sensor: Mapped[str] = mapped_column(String(40))
    value: Mapped[float] = mapped_column(Float)

    __table_args__ = (Index("ix_sensor_wo_sensor", "work_order_id", "sensor"),)


# ------------------------ Ops / analytics output ----------------------
class DQResult(Base):
    __tablename__ = "dq_result"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime)
    dataset: Mapped[str] = mapped_column(String(40))
    rule: Mapped[str] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(10))
    passed: Mapped[bool] = mapped_column(Boolean)
    failed_rows: Mapped[int] = mapped_column(Integer)
    total_rows: Mapped[int] = mapped_column(Integer)


class AnomalyEvent(Base):
    __tablename__ = "anomaly_event"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    method: Mapped[str] = mapped_column(String(30))
    entity: Mapped[str] = mapped_column(String(40))
    period: Mapped[str] = mapped_column(String(20))
    metric: Mapped[str] = mapped_column(String(40))
    value: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    detail: Mapped[str] = mapped_column(String(200))


def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or settings.db_url, future=True)


def create_schema(engine: Engine, drop: bool = False) -> None:
    if drop:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
