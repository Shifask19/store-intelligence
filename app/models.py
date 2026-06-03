"""
app/models.py — SQLAlchemy ORM models + Pydantic request/response schemas

Two layers:
  1. ORM (SQLAlchemy) — what lives in PostgreSQL
  2. Pydantic schemas — what the API accepts and returns

Keeping them separate avoids the "fat model" anti-pattern and makes
the API contract explicit and independently versioned.
"""

from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase


# ---------------------------------------------------------------------------
# SQLAlchemy base
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# ORM: events table
# ---------------------------------------------------------------------------
class EventORM(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(36), nullable=False)       # UUID-v4
    store_id = Column(String(64), nullable=False)
    camera_id = Column(String(64), nullable=False)
    visitor_id = Column(String(32), nullable=False)
    event_type = Column(String(32), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    zone_id = Column(String(64), nullable=True)
    dwell_ms = Column(Integer, nullable=False, default=0)
    is_staff = Column(Boolean, nullable=False, default=False)
    confidence = Column(Float, nullable=False)
    queue_depth = Column(Integer, nullable=True)
    sku_zone = Column(String(64), nullable=True)
    session_seq = Column(Integer, nullable=False, default=0)
    ingested_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Primary deduplication index — idempotency guarantee
        UniqueConstraint("event_id", name="uq_events_event_id"),
        # Query patterns: store + time range is the most common access pattern
        Index("ix_events_store_ts", "store_id", "timestamp"),
        Index("ix_events_visitor", "visitor_id"),
        Index("ix_events_type", "event_type"),
        Index("ix_events_zone", "store_id", "zone_id"),
    )


# ---------------------------------------------------------------------------
# ORM: POS transactions table
# ---------------------------------------------------------------------------
class POSTransactionORM(Base):
    __tablename__ = "pos_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    store_id = Column(String(64), nullable=False)
    transaction_id = Column(String(64), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    basket_value_inr = Column(Float, nullable=False, default=0.0)

    __table_args__ = (
        UniqueConstraint("transaction_id", name="uq_pos_txn_id"),
        Index("ix_pos_store_ts", "store_id", "timestamp"),
    )


# ---------------------------------------------------------------------------
# Pydantic: inbound event schema (mirrors pipeline/emit.py StoreEvent)
# ---------------------------------------------------------------------------
class EventTypeEnum(str, PyEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class EventMetadataSchema(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


class EventIngest(BaseModel):
    """Single event as accepted by POST /events/ingest."""

    event_id: str = Field(..., min_length=1)
    store_id: str = Field(..., min_length=1)
    camera_id: str = Field(..., min_length=1)
    visitor_id: str = Field(..., min_length=1)
    event_type: EventTypeEnum
    timestamp: datetime
    zone_id: Optional[str] = None
    dwell_ms: int = Field(0, ge=0)
    is_staff: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: EventMetadataSchema = Field(default_factory=EventMetadataSchema)

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, v):
        if isinstance(v, str):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v


class IngestBatch(BaseModel):
    """Batch payload for POST /events/ingest (up to 500 events)."""

    events: list[EventIngest] = Field(..., min_length=1, max_length=500)


# ---------------------------------------------------------------------------
# Pydantic: response schemas
# ---------------------------------------------------------------------------
class IngestResult(BaseModel):
    accepted: int
    duplicate: int
    rejected: int
    errors: list[dict] = []


class ZoneDwellMetric(BaseModel):
    zone_id: str
    avg_dwell_ms: float
    visit_count: int


class StoreMetricsResponse(BaseModel):
    store_id: str
    date: str                          # YYYY-MM-DD
    unique_visitors: int
    conversion_rate: float             # 0.0–1.0
    avg_dwell_per_zone: list[ZoneDwellMetric]
    current_queue_depth: int
    abandonment_rate: float            # 0.0–1.0
    total_transactions: int


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float                # % who dropped off before this stage


class FunnelResponse(BaseModel):
    store_id: str
    stages: list[FunnelStage]
    session_window_start: Optional[datetime]
    session_window_end: Optional[datetime]


class HeatmapZone(BaseModel):
    zone_id: str
    sku_zone: Optional[str]
    visit_frequency: int
    avg_dwell_ms: float
    normalised_score: float            # 0–100
    data_confidence: bool              # False if < 20 sessions


class HeatmapResponse(BaseModel):
    store_id: str
    zones: list[HeatmapZone]
    data_confidence: bool              # False if total sessions < 20


class AnomalyItem(BaseModel):
    anomaly_type: str
    severity: str                      # INFO | WARN | CRITICAL
    description: str
    suggested_action: str
    detected_at: datetime
    zone_id: Optional[str] = None
    value: Optional[float] = None
    threshold: Optional[float] = None


class AnomaliesResponse(BaseModel):
    store_id: str
    anomalies: list[AnomalyItem]
    checked_at: datetime


class StoreHealth(BaseModel):
    store_id: str
    status: str                        # OK | STALE_FEED | NO_DATA
    last_event_at: Optional[datetime]
    lag_minutes: Optional[float]
    warning: Optional[str]


class HealthResponse(BaseModel):
    status: str                        # healthy | degraded
    stores: list[StoreHealth]
    checked_at: datetime
    db_status: str                     # ok | unavailable
