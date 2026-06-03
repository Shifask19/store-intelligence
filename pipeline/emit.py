"""
pipeline/emit.py — Event schema (Pydantic) + JSONL emission

Matches the Purplle sample_events.jsonl schema exactly.
Two event families:
  1. Entry/Exit events  — id_token, store_code, gender_pred, age_pred, group_id
  2. Zone events        — track_id, store_id, zone_id, zone_name, zone_type
  3. Queue events       — queue_event_id, queue_join_ts, wait_seconds, abandoned
"""

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Event type catalogue
# ---------------------------------------------------------------------------
class EventType(str, Enum):
    ENTRY           = "entry"
    EXIT            = "exit"
    ZONE_ENTERED    = "zone_entered"
    ZONE_EXITED     = "zone_exited"
    QUEUE_COMPLETED = "queue_completed"
    QUEUE_ABANDONED = "queue_abandoned"
    # Keep legacy types for internal API compatibility
    ENTRY_UPPER          = "ENTRY"
    EXIT_UPPER           = "EXIT"
    ZONE_ENTER           = "ZONE_ENTER"
    ZONE_EXIT            = "ZONE_EXIT"
    ZONE_DWELL           = "ZONE_DWELL"
    BILLING_QUEUE_JOIN   = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON= "BILLING_QUEUE_ABANDON"
    REENTRY              = "REENTRY"


# ---------------------------------------------------------------------------
# Entry / Exit event (Purplle native schema)
# ---------------------------------------------------------------------------
class EntryExitEvent(BaseModel):
    event_type: str                          # "entry" | "exit"
    id_token: str                            # Re-ID token e.g. ID_60001
    store_code: str                          # e.g. store_1076
    camera_id: str
    event_timestamp: str                     # ISO-8601 with microseconds
    is_staff: bool = False
    gender_pred: Optional[str] = None        # "M" | "F" | null
    age_pred: Optional[int] = None
    age_bucket: Optional[str] = None         # "18-24", "25-34", etc.
    is_face_hidden: bool = False
    group_id: Optional[str] = None
    group_size: Optional[int] = None
    confidence: float = 0.5

    def to_jsonl_line(self) -> str:
        d = self.model_dump()
        return json.dumps(d, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Zone event (Purplle native schema)
# ---------------------------------------------------------------------------
class ZoneEvent(BaseModel):
    event_type: str                          # "zone_entered" | "zone_exited"
    track_id: int
    store_id: str
    camera_id: str
    zone_id: str
    zone_name: str
    zone_type: str                           # "SHELF" | "DISPLAY" | "BILLING"
    is_revenue_zone: str = "Yes"             # "Yes" | "No"
    event_time: str                          # ISO-8601
    zone_hotspot_x: Optional[float] = None
    zone_hotspot_y: Optional[float] = None
    gender: Optional[str] = None
    age: Optional[int] = None
    age_bucket: Optional[str] = None
    dwell_seconds: Optional[float] = None    # only on zone_exited
    confidence: float = 0.5

    def to_jsonl_line(self) -> str:
        d = self.model_dump()
        return json.dumps(d, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Queue event (Purplle native schema)
# ---------------------------------------------------------------------------
class QueueEvent(BaseModel):
    queue_event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str                          # "queue_completed" | "queue_abandoned"
    track_id: int
    store_id: str
    camera_id: str
    zone_id: str
    zone_name: str = "Billing Counter Queue"
    zone_type: str = "BILLING"
    is_revenue_zone: str = "Yes"
    queue_join_ts: str
    queue_served_ts: Optional[str] = None
    queue_exit_ts: Optional[str] = None
    wait_seconds: Optional[int] = None
    queue_position_at_join: int = 1
    abandoned: bool = False
    zone_hotspot_x: Optional[float] = None
    zone_hotspot_y: Optional[float] = None
    gender: Optional[str] = None
    age: Optional[int] = None
    age_bucket: Optional[str] = None

    def to_jsonl_line(self) -> str:
        d = self.model_dump()
        return json.dumps(d, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Legacy StoreEvent — kept for API ingest compatibility
# ---------------------------------------------------------------------------
class EventMetadata(BaseModel):
    queue_depth: Optional[int] = Field(None)
    sku_zone: Optional[str] = Field(None)
    session_seq: int = Field(0, ge=0)


class StoreEvent(BaseModel):
    """Legacy schema — used internally by the API."""
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: str
    timestamp: datetime
    zone_id: Optional[str] = None
    dwell_ms: int = Field(0, ge=0)
    is_staff: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("timestamp", mode="before")
    @classmethod
    def ensure_utc(cls, v):
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if isinstance(v, datetime) and v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v

    @model_validator(mode="after")
    def zone_id_rules(self):
        entry_exit = {"ENTRY", "EXIT", "REENTRY", "entry", "exit"}
        zone_required = {
            "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
            "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON",
            "zone_entered", "zone_exited",
        }
        if self.event_type in zone_required and not self.zone_id:
            raise ValueError(f"zone_id required for event_type={self.event_type}")
        return self

    def to_jsonl_line(self) -> str:
        d = self.model_dump()
        d["timestamp"] = self.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        return json.dumps(d, ensure_ascii=False)


# ---------------------------------------------------------------------------
# JSONL writer — append-safe
# ---------------------------------------------------------------------------
class EventEmitter:
    def __init__(self, output_path: str):
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.output_path.open("a", encoding="utf-8")
        self._count = 0

    def emit(self, event) -> None:
        """Accept EntryExitEvent, ZoneEvent, QueueEvent, or StoreEvent."""
        self._fh.write(event.to_jsonl_line() + "\n")
        self._fh.flush()
        self._count += 1

    def emit_raw(self, data: dict) -> None:
        self._fh.write(json.dumps(data, ensure_ascii=False) + "\n")
        self._fh.flush()
        self._count += 1

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @property
    def count(self) -> int:
        return self._count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_visitor_id() -> str:
    """VIS_ + 6 hex chars — legacy format."""
    return "VIS_" + uuid.uuid4().hex[:6]


def make_id_token(counter: int) -> str:
    """Purplle native format: ID_XXXXX."""
    return f"ID_{60000 + counter}"


def age_bucket(age: Optional[int]) -> Optional[str]:
    if age is None:
        return None
    if age < 18:   return "Under 18"
    if age < 25:   return "18-24"
    if age < 35:   return "25-34"
    if age < 45:   return "35-44"
    if age < 55:   return "45-54"
    return "55+"


def load_events_jsonl(path: str) -> list:
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events
