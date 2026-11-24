from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import DOUBLE
from sqlalchemy.orm import Mapped, mapped_column

from src.constants.enums import BatchStatus, RunStatus
from src.models.base import Base, utc_now

MEASURE = Float().with_variant(DOUBLE(), 'mysql')


class Observation(Base):
    __tablename__ = 'observations'
    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(96), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class CorrectionBatch(Base):
    __tablename__ = 'correction_batches'
    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(96), unique=True)
    title: Mapped[str] = mapped_column(String(120))
    note: Mapped[str] = mapped_column(String(300), default='')
    actor: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(16), default=BatchStatus.PENDING, index=True)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    verified_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class CorrectionItem(Base):
    __tablename__ = 'correction_items'
    __table_args__ = (UniqueConstraint('batch_id', 'desk', 'scan'), UniqueConstraint('effective_key'))
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('correction_batches.id'), index=True)
    desk: Mapped[str] = mapped_column(String(300))
    scan: Mapped[str] = mapped_column(String(300))
    amplitude: Mapped[float] = mapped_column(MEASURE)
    streak: Mapped[float] = mapped_column(MEASURE)
    old_amplitude: Mapped[float | None] = mapped_column(MEASURE, nullable=True)
    old_streak: Mapped[float | None] = mapped_column(MEASURE, nullable=True)
    observation_id: Mapped[int | None] = mapped_column(ForeignKey('observations.id'), nullable=True)
    effective_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AnalysisRun(Base):
    __tablename__ = 'analysis_runs'
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    owner: Mapped[str] = mapped_column(String(60))
    parameters: Mapped[dict] = mapped_column(JSON)
    last_observation_id: Mapped[int] = mapped_column(Integer)
    sample_size: Mapped[int] = mapped_column(Integer)
    observation_ids: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.QUEUED, index=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AnalysisSnapshot(Base):
    """发起分析时固化的有效测次和用到的补正批次。"""
    __tablename__ = 'analysis_snapshots'
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), unique=True)
    rows: Mapped[list] = mapped_column(JSON)
    batches: Mapped[list] = mapped_column(JSON, default=list)


class Review(Base):
    __tablename__ = 'reviews'
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), unique=True)
    decision: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Schedule(Base):
    __tablename__ = 'schedules'
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), index=True)
    recipient: Mapped[str] = mapped_column(String(100))
    interval_minutes: Mapped[int] = mapped_column(Integer)
    next_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Delivery(Base):
    __tablename__ = 'deliveries'
    __table_args__ = (UniqueConstraint('schedule_id', 'scheduled_at'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey('schedules.id'))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    filename: Mapped[str] = mapped_column(String(120))
    delivered_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AuditEvent(Base):
    __tablename__ = 'audit_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
