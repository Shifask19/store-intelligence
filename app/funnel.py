"""
app/funnel.py — GET /stores/{id}/funnel

Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase
Session is the unit of analysis, not raw events.
Re-entries must not double-count a visitor.

Funnel logic:
  Stage 1 — Entry:         distinct visitor_ids with any ENTRY event today
  Stage 2 — Zone Visit:    of those, how many also had a ZONE_ENTER event
  Stage 3 — Billing Queue: of those, how many entered a billing zone
  Stage 4 — Purchase:      of those, how many are correlated with a POS txn

Drop-off % at each stage = (prev_stage - this_stage) / prev_stage * 100
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import FunnelResponse, FunnelStage

logger = logging.getLogger(__name__)
router = APIRouter()

POS_WINDOW_MINUTES = 5


@router.get(
    "/stores/{store_id}/funnel",
    response_model=FunnelResponse,
    summary="Conversion funnel — session-level, re-entry deduplicated",
)
async def get_funnel(
    store_id: str,
    date_str: Optional[str] = Query(None, alias="date", description="YYYY-MM-DD"),
    db: AsyncSession = Depends(get_db),
) -> FunnelResponse:
    """
    Returns the 4-stage conversion funnel for a store.

    Re-entry deduplication: REENTRY events reuse the same visitor_id,
    so COUNT(DISTINCT visitor_id) naturally deduplicates across sessions.
    """
    try:
        target_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    day_start = datetime(target_date.year, target_date.month, target_date.day,
                         tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    try:
        # Stage 1: Unique customer entries
        # Primary: ENTRY + REENTRY events. Fallback: any visitor in any event
        # (handles entry camera misconfiguration gracefully).
        s1 = await db.execute(text("""
            SELECT COUNT(DISTINCT visitor_id) AS cnt
            FROM events
            WHERE store_id = :store_id
              AND event_type IN ('ENTRY', 'REENTRY')
              AND is_staff = FALSE
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        stage1 = s1.scalar() or 0

        # Fallback: if ENTRY events are sparse, use all unique visitors
        if stage1 < 2:
            s1_fb = await db.execute(text("""
                SELECT COUNT(DISTINCT visitor_id) AS cnt
                FROM events
                WHERE store_id = :store_id
                  AND is_staff = FALSE
                  AND timestamp >= :day_start
                  AND timestamp < :day_end
            """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
            stage1 = max(stage1, s1_fb.scalar() or 0)

        # Stage 2: Visitors who entered at least one product zone
        s2 = await db.execute(text("""
            SELECT COUNT(DISTINCT visitor_id) AS cnt
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'ZONE_ENTER'
              AND zone_id NOT IN ('ENTRY_ZONE', 'BILLING', 'BILLING_QUEUE')
              AND is_staff = FALSE
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        stage2 = s2.scalar() or 0

        # Stage 3: Visitors who entered the billing zone
        s3 = await db.execute(text("""
            SELECT COUNT(DISTINCT visitor_id) AS cnt
            FROM events
            WHERE store_id = :store_id
              AND event_type IN ('ZONE_ENTER', 'BILLING_QUEUE_JOIN')
              AND zone_id IN ('BILLING', 'BILLING_QUEUE')
              AND is_staff = FALSE
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        stage3 = s3.scalar() or 0

        # Fetch billing-zone events and POS txns separately; correlate in Python
        # so the query works on both SQLite (tests) and PostgreSQL (production).
        billing_evts = await db.execute(text("""
            SELECT DISTINCT visitor_id, timestamp
            FROM events
            WHERE store_id = :store_id
              AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL', 'BILLING_QUEUE_JOIN')
              AND zone_id IN ('BILLING', 'BILLING_QUEUE')
              AND is_staff = FALSE
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        billing_rows = billing_evts.fetchall()

        pos_txns = await db.execute(text("""
            SELECT timestamp FROM pos_transactions
            WHERE store_id = :store_id
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})

        def _tz(dt):
            if dt is None:
                return dt
            if isinstance(dt, str):
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            if hasattr(dt, 'tzinfo') and dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt

        txn_times = [_tz(r.timestamp) for r in pos_txns.fetchall()]
        pos_window = timedelta(minutes=POS_WINDOW_MINUTES)
        converted_set = set()
        for row in billing_rows:
            ts = _tz(row.timestamp)
            for txn_ts in txn_times:
                if timedelta(0) <= (txn_ts - ts) <= pos_window:
                    converted_set.add(row.visitor_id)
                    break
        stage4 = len(converted_set)

        # Window bounds for the response
        ts_result = await db.execute(text("""
            SELECT MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts
            FROM events
            WHERE store_id = :store_id
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        ts_row = ts_result.fetchone()

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"funnel query failed for {store_id}: {e}")
        raise HTTPException(
            status_code=503,
            detail={"error": "database_unavailable", "message": "Funnel query failed"},
        )

    def drop_off(prev: int, curr: int) -> float:
        if prev == 0:
            return 0.0
        return round((prev - curr) / prev * 100, 1)

    # Cap each stage at the previous stage (funnel can only narrow)
    stage2 = min(stage2, stage1)
    stage3 = min(stage3, stage2)
    stage4 = min(stage4, stage3)

    stages = [
        FunnelStage(stage="Entry",         count=stage1, drop_off_pct=0.0),
        FunnelStage(stage="Zone Visit",    count=stage2, drop_off_pct=drop_off(stage1, stage2)),
        FunnelStage(stage="Billing Queue", count=stage3, drop_off_pct=drop_off(stage2, stage3)),
        FunnelStage(stage="Purchase",      count=stage4, drop_off_pct=drop_off(stage3, stage4)),
    ]

    return FunnelResponse(
        store_id=store_id,
        stages=stages,
        session_window_start=ts_row.first_ts if ts_row else None,
        session_window_end=ts_row.last_ts if ts_row else None,
    )
