"""
app/metrics.py — GET /stores/{id}/metrics

Computes real-time store metrics from the events table.
All queries are scoped to today (UTC) so the numbers are always fresh.

Key design choices:
- Conversion is computed by correlating billing-zone presence with POS
  transactions in a 5-minute look-back window (per spec).
- Staff events (is_staff=True) are excluded from every customer metric.
- Zero-traffic stores return zeros, never null or 500.
"""

import logging
from datetime import datetime, date, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import StoreMetricsResponse, ZoneDwellMetric

logger = logging.getLogger(__name__)
router = APIRouter()

POS_WINDOW_MINUTES = 5   # visitor in billing zone within 5 min before txn = converted


@router.get(
    "/stores/{store_id}/metrics",
    response_model=StoreMetricsResponse,
    summary="Real-time store metrics for today",
)
async def get_metrics(
    store_id: str,
    date_str: Optional[str] = Query(None, alias="date", description="YYYY-MM-DD (default: today UTC)"),
    db: AsyncSession = Depends(get_db),
) -> StoreMetricsResponse:
    """
    Returns today's metrics for a store:
    - unique_visitors: distinct visitor_ids with ENTRY events (non-staff)
    - conversion_rate: visitors who were in billing zone ≤5 min before a POS txn
    - avg_dwell_per_zone: mean dwell_ms per zone from ZONE_DWELL + ZONE_EXIT events
    - current_queue_depth: latest queue_depth value from BILLING_QUEUE_JOIN events
    - abandonment_rate: BILLING_QUEUE_ABANDON / (BILLING_QUEUE_JOIN + BILLING_QUEUE_ABANDON)
    """
    try:
        target_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    day_start = datetime(target_date.year, target_date.month, target_date.day,
                         tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    try:
        # ----------------------------------------------------------------
        # 1. Unique customer visitors
        # Primary: distinct visitor_ids with ENTRY or REENTRY events (non-staff)
        # Fallback: if ENTRY count is very low (entry camera misconfigured or
        #           footage angle issue), use any visitor seen in zone events.
        #           This is a graceful degradation — documented in DESIGN.md.
        # ----------------------------------------------------------------
        uv_result = await db.execute(text("""
            SELECT COUNT(DISTINCT visitor_id) AS unique_visitors
            FROM events
            WHERE store_id = :store_id
              AND event_type IN ('ENTRY', 'REENTRY')
              AND is_staff = FALSE
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        unique_visitors = uv_result.scalar() or 0

        # Fallback: count any visitor who appeared in any event if ENTRY is sparse
        if unique_visitors < 2:
            fallback = await db.execute(text("""
                SELECT COUNT(DISTINCT visitor_id) AS unique_visitors
                FROM events
                WHERE store_id = :store_id
                  AND is_staff = FALSE
                  AND timestamp >= :day_start
                  AND timestamp < :day_end
            """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
            fallback_count = fallback.scalar() or 0
            if fallback_count > unique_visitors:
                unique_visitors = fallback_count

        # ----------------------------------------------------------------
        # 2. Conversion rate via POS correlation
        #    A visitor is "converted" if they had a ZONE_ENTER/ZONE_DWELL
        #    in a billing zone within POS_WINDOW_MINUTES before any txn.
        # ----------------------------------------------------------------
        # Use Python-computed window_end per event to stay SQLite + PG compatible.
        # We fetch billing-zone events and POS txns separately, then correlate in Python.
        billing_events = await db.execute(text("""
            SELECT DISTINCT visitor_id, timestamp
            FROM events
            WHERE store_id = :store_id
              AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL', 'BILLING_QUEUE_JOIN')
              AND zone_id IN ('BILLING', 'BILLING_QUEUE')
              AND is_staff = FALSE
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        billing_rows = billing_events.fetchall()

        pos_txns = await db.execute(text("""
            SELECT timestamp FROM pos_transactions
            WHERE store_id = :store_id
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        txn_times = [r.timestamp for r in pos_txns.fetchall()]

        # Ensure all timestamps are tz-aware for comparison.
        # SQLite returns timestamps as strings when inserted via raw SQL.
        def _tz(dt):
            if dt is None:
                return dt
            if isinstance(dt, str):
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            if hasattr(dt, 'tzinfo') and dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt

        txn_times = [_tz(t) for t in txn_times]
        converted_visitors = set()
        pos_window = timedelta(minutes=POS_WINDOW_MINUTES)
        for row in billing_rows:
            ts = _tz(row.timestamp)
            for txn_ts in txn_times:
                if timedelta(0) <= (txn_ts - ts) <= pos_window:
                    converted_visitors.add(row.visitor_id)
                    break

        conv_result_scalar = len(converted_visitors)
        # Wrap in a simple namespace so the code below works unchanged
        class _R:
            def scalar(self): return conv_result_scalar
        conv_result = _R()
        converted = conv_result.scalar() or 0
        conversion_rate = round(converted / unique_visitors, 4) if unique_visitors > 0 else 0.0

        # ----------------------------------------------------------------
        # 3. Avg dwell per zone (from ZONE_DWELL + ZONE_EXIT with dwell_ms > 0)
        # ----------------------------------------------------------------
        dwell_result = await db.execute(text("""
            SELECT zone_id,
                   AVG(dwell_ms)   AS avg_dwell_ms,
                   COUNT(*)        AS visit_count
            FROM events
            WHERE store_id = :store_id
              AND event_type IN ('ZONE_DWELL', 'ZONE_EXIT')
              AND zone_id IS NOT NULL
              AND dwell_ms > 0
              AND is_staff = FALSE
              AND timestamp >= :day_start
              AND timestamp < :day_end
            GROUP BY zone_id
            ORDER BY avg_dwell_ms DESC
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        dwell_rows = dwell_result.fetchall()
        avg_dwell_per_zone = [
            ZoneDwellMetric(
                zone_id=row.zone_id,
                avg_dwell_ms=round(float(row.avg_dwell_ms), 1),
                visit_count=int(row.visit_count),
            )
            for row in dwell_rows
        ]

        # ----------------------------------------------------------------
        # 4. Current queue depth (most recent BILLING_QUEUE_JOIN queue_depth)
        # ----------------------------------------------------------------
        queue_result = await db.execute(text("""
            SELECT queue_depth
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND queue_depth IS NOT NULL
              AND timestamp >= :day_start
              AND timestamp < :day_end
            ORDER BY timestamp DESC
            LIMIT 1
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        queue_row = queue_result.fetchone()
        current_queue_depth = int(queue_row.queue_depth) if queue_row else 0

        # ----------------------------------------------------------------
        # 5. Abandonment rate
        # ----------------------------------------------------------------
        abandon_result = await db.execute(text("""
            SELECT
                SUM(CASE WHEN event_type = 'BILLING_QUEUE_ABANDON' THEN 1 ELSE 0 END) AS abandons,
                SUM(CASE WHEN event_type = 'BILLING_QUEUE_JOIN'    THEN 1 ELSE 0 END) AS joins
            FROM events
            WHERE store_id = :store_id
              AND event_type IN ('BILLING_QUEUE_ABANDON', 'BILLING_QUEUE_JOIN')
              AND is_staff = FALSE
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        ab_row = abandon_result.fetchone()
        abandons = int(ab_row.abandons or 0)
        joins = int(ab_row.joins or 0)
        total_queue = joins + abandons
        abandonment_rate = round(abandons / total_queue, 4) if total_queue > 0 else 0.0

        # ----------------------------------------------------------------
        # 6. Total POS transactions today
        # ----------------------------------------------------------------
        txn_result = await db.execute(text("""
            SELECT COUNT(*) AS total
            FROM pos_transactions
            WHERE store_id = :store_id
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        total_transactions = txn_result.scalar() or 0

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"metrics query failed for {store_id}: {e}")
        raise HTTPException(
            status_code=503,
            detail={"error": "database_unavailable", "message": "Metrics query failed"},
        )

    return StoreMetricsResponse(
        store_id=store_id,
        date=target_date.isoformat(),
        unique_visitors=unique_visitors,
        conversion_rate=conversion_rate,
        avg_dwell_per_zone=avg_dwell_per_zone,
        current_queue_depth=current_queue_depth,
        abandonment_rate=abandonment_rate,
        total_transactions=total_transactions,
    )
