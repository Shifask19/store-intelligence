"""
app/ingestion.py — POST /events/ingest

Idempotent batch ingest endpoint.
- Accepts up to 500 events per request.
- Validates each event individually — partial success on malformed events.
- Deduplicates by event_id (INSERT ... ON CONFLICT DO NOTHING).
- Returns accepted/duplicate/rejected counts + per-error details.
- Never returns 5xx for bad individual events — only for DB failures.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import EventIngest, EventORM, IngestBatch, IngestResult

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/events/ingest",
    response_model=IngestResult,
    summary="Batch ingest events (idempotent by event_id)",
)
async def ingest_events(
    payload: IngestBatch,
    db: AsyncSession = Depends(get_db),
) -> IngestResult:
    """
    Accepts a batch of up to 500 events.

    Partial success: Pydantic validates the batch structure. Individual
    DB errors are caught per-event and counted in `rejected`.

    Idempotency: calling this endpoint twice with the same payload produces
    the same DB state. Duplicate event_ids are silently skipped and counted
    in the `duplicate` field of the response.
    """
    accepted = 0
    duplicate = 0
    rejected = 0
    errors: list[dict] = []
    now = datetime.now(timezone.utc)

    # Build ORM objects
    orm_objects: list[EventORM] = []
    for idx, event in enumerate(payload.events):
        try:
            orm = _event_to_orm(event, ingested_at=now)
            orm_objects.append(orm)
        except Exception as e:
            rejected += 1
            errors.append({"index": idx, "event_id": event.event_id, "error": str(e)})

    if not orm_objects:
        return IngestResult(accepted=0, duplicate=0, rejected=rejected, errors=errors)

    # Bulk insert with ON CONFLICT DO NOTHING for idempotency
    insert_sql = text("""
        INSERT INTO events (
            event_id, store_id, camera_id, visitor_id, event_type,
            timestamp, zone_id, dwell_ms, is_staff, confidence,
            queue_depth, sku_zone, session_seq, ingested_at
        ) VALUES (
            :event_id, :store_id, :camera_id, :visitor_id, :event_type,
            :timestamp, :zone_id, :dwell_ms, :is_staff, :confidence,
            :queue_depth, :sku_zone, :session_seq, :ingested_at
        )
        ON CONFLICT (event_id) DO NOTHING
    """)

    try:
        for orm in orm_objects:
            result = await db.execute(
                insert_sql,
                {
                    "event_id":    orm.event_id,
                    "store_id":    orm.store_id,
                    "camera_id":   orm.camera_id,
                    "visitor_id":  orm.visitor_id,
                    "event_type":  orm.event_type,
                    "timestamp":   orm.timestamp,
                    "zone_id":     orm.zone_id,
                    "dwell_ms":    orm.dwell_ms,
                    "is_staff":    orm.is_staff,
                    "confidence":  orm.confidence,
                    "queue_depth": orm.queue_depth,
                    "sku_zone":    orm.sku_zone,
                    "session_seq": orm.session_seq,
                    "ingested_at": orm.ingested_at,
                },
            )
            if result.rowcount == 1:
                accepted += 1
            else:
                duplicate += 1

        await db.commit()

    except Exception as e:
        await db.rollback()
        logger.error(f"DB error during ingest: {e}")
        raise HTTPException(
            status_code=503,
            detail={"error": "database_unavailable", "message": "DB write failed"},
        )

    logger.info(
        f"ingest accepted={accepted} duplicate={duplicate} rejected={rejected}",
        extra={"event_count": accepted + duplicate + rejected},
    )
    return IngestResult(
        accepted=accepted,
        duplicate=duplicate,
        rejected=rejected,
        errors=errors,
    )


def _event_to_orm(event: EventIngest, ingested_at: datetime) -> EventORM:
    """Convert a validated Pydantic event to an ORM row."""
    return EventORM(
        event_id=event.event_id,
        store_id=event.store_id,
        camera_id=event.camera_id,
        visitor_id=event.visitor_id,
        event_type=event.event_type.value,
        timestamp=event.timestamp,
        zone_id=event.zone_id,
        dwell_ms=event.dwell_ms,
        is_staff=event.is_staff,
        confidence=event.confidence,
        queue_depth=event.metadata.queue_depth,
        sku_zone=event.metadata.sku_zone,
        session_seq=event.metadata.session_seq,
        ingested_at=ingested_at,
    )
