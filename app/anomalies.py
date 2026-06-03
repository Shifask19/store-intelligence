"""
app/anomalies.py — GET /stores/{id}/anomalies

Detects three classes of operational anomalies:

1. BILLING_QUEUE_SPIKE   — current queue depth > 2× the day's average
2. CONVERSION_DROP       — today's conversion rate < 70% of 7-day rolling avg
3. DEAD_ZONE             — a product zone with zero visits in the last 30 minutes
                           (only flagged if the store has had traffic today)

Severity mapping:
  INFO     — notable but not urgent
  WARN     — needs attention within the hour
  CRITICAL — needs immediate action
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AnomaliesResponse, AnomalyItem

logger = logging.getLogger(__name__)
router = APIRouter()

# Thresholds — could be moved to config/env vars
QUEUE_SPIKE_MULTIPLIER = 2.0      # current > 2× avg → spike
CONVERSION_DROP_THRESHOLD = 0.70  # today < 70% of 7-day avg → drop
DEAD_ZONE_MINUTES = 30            # no visits in 30 min → dead zone

# Product zones to monitor for dead-zone detection (exclude entry/billing)
PRODUCT_ZONES = {
    "SKINCARE", "MAKEUP", "HAIRCARE", "FRAGRANCE",
    "PERSONAL_CARE", "WELLNESS", "ACCESSORIES",
}


@router.get(
    "/stores/{store_id}/anomalies",
    response_model=AnomaliesResponse,
    summary="Active operational anomalies with severity and suggested actions",
)
async def get_anomalies(
    store_id: str,
    date_str: Optional[str] = Query(None, alias="date", description="YYYY-MM-DD"),
    db: AsyncSession = Depends(get_db),
) -> AnomaliesResponse:
    try:
        target_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    now = datetime.now(timezone.utc)
    day_start = datetime(target_date.year, target_date.month, target_date.day,
                         tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    anomalies: list[AnomalyItem] = []

    try:
        # ----------------------------------------------------------------
        # 1. BILLING_QUEUE_SPIKE
        # ----------------------------------------------------------------
        # Current queue depth (most recent BILLING_QUEUE_JOIN)
        curr_q = await db.execute(text("""
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
        curr_q_row = curr_q.fetchone()
        current_queue = int(curr_q_row.queue_depth) if curr_q_row else 0

        # Average queue depth today
        avg_q = await db.execute(text("""
            SELECT AVG(queue_depth) AS avg_q
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND queue_depth IS NOT NULL
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        avg_queue = float(avg_q.scalar() or 0)

        if avg_queue > 0 and current_queue >= QUEUE_SPIKE_MULTIPLIER * avg_queue:
            severity = "CRITICAL" if current_queue >= 3 * avg_queue else "WARN"
            anomalies.append(AnomalyItem(
                anomaly_type="BILLING_QUEUE_SPIKE",
                severity=severity,
                description=f"Queue depth {current_queue} is {current_queue/avg_queue:.1f}× the daily average ({avg_queue:.1f})",
                suggested_action="Open an additional billing counter or redirect customers to self-checkout.",
                detected_at=now,
                zone_id="BILLING",
                value=float(current_queue),
                threshold=QUEUE_SPIKE_MULTIPLIER * avg_queue,
            ))

        # ----------------------------------------------------------------
        # 2. CONVERSION_DROP vs 7-day rolling average
        # ----------------------------------------------------------------
        # Today's conversion
        today_uv = await db.execute(text("""
            SELECT COUNT(DISTINCT visitor_id) AS uv
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'ENTRY'
              AND is_staff = FALSE
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        today_visitors = today_uv.scalar() or 0

        # Fetch billing events + POS txns; correlate in Python (SQLite + PG compatible)
        today_billing = await db.execute(text("""
            SELECT DISTINCT visitor_id, timestamp
            FROM events
            WHERE store_id = :store_id
              AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL', 'BILLING_QUEUE_JOIN')
              AND zone_id IN ('BILLING', 'BILLING_QUEUE')
              AND is_staff = FALSE
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        today_billing_rows = today_billing.fetchall()

        today_pos = await db.execute(text("""
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

        pos_window = timedelta(minutes=5)
        today_txn_times = [_tz(r.timestamp) for r in today_pos.fetchall()]
        today_converted_set = set()
        for row in today_billing_rows:
            ts = _tz(row.timestamp)
            for txn_ts in today_txn_times:
                if timedelta(0) <= (txn_ts - ts) <= pos_window:
                    today_converted_set.add(row.visitor_id)
                    break
        today_converted = len(today_converted_set)
        today_rate = today_converted / today_visitors if today_visitors > 0 else None

        # 7-day rolling average (excluding today) — use DATE() which works in SQLite
        week_start = day_start - timedelta(days=7)
        week_uv = await db.execute(text("""
            SELECT
                DATE(timestamp) AS day,
                COUNT(DISTINCT visitor_id) AS uv
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'ENTRY'
              AND is_staff = FALSE
              AND timestamp >= :week_start
              AND timestamp < :day_start
            GROUP BY 1
        """), {"store_id": store_id, "week_start": week_start, "day_start": day_start})
        week_rows = week_uv.fetchall()

        # For 7-day conversion, fetch all billing events + txns in the window
        week_billing = await db.execute(text("""
            SELECT DISTINCT visitor_id, timestamp, DATE(timestamp) AS day
            FROM events
            WHERE store_id = :store_id
              AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL', 'BILLING_QUEUE_JOIN')
              AND zone_id IN ('BILLING', 'BILLING_QUEUE')
              AND is_staff = FALSE
              AND timestamp >= :week_start
              AND timestamp < :day_start
        """), {"store_id": store_id, "week_start": week_start, "day_start": day_start})
        week_billing_rows = week_billing.fetchall()

        week_pos = await db.execute(text("""
            SELECT timestamp FROM pos_transactions
            WHERE store_id = :store_id
              AND timestamp >= :week_start
              AND timestamp < :day_start
        """), {"store_id": store_id, "week_start": week_start, "day_start": day_start})
        week_txn_times = [_tz(r.timestamp) for r in week_pos.fetchall()]

        # Group billing events by day
        from collections import defaultdict
        billing_by_day: dict = defaultdict(list)
        for row in week_billing_rows:
            billing_by_day[str(row.day)].append((_tz(row.timestamp), row.visitor_id))

        uv_by_day = {str(r.day): int(r.uv) for r in week_rows}
        daily_rates = []
        for d, uv in uv_by_day.items():
            if uv > 0:
                conv_set = set()
                for ts, vid in billing_by_day.get(d, []):
                    for txn_ts in week_txn_times:
                        if timedelta(0) <= (txn_ts - ts) <= pos_window:
                            conv_set.add(vid)
                            break
                daily_rates.append(len(conv_set) / uv)

        if daily_rates and today_rate is not None:
            avg_7day = sum(daily_rates) / len(daily_rates)
            if avg_7day > 0 and today_rate < CONVERSION_DROP_THRESHOLD * avg_7day:
                drop_pct = round((1 - today_rate / avg_7day) * 100, 1)
                anomalies.append(AnomalyItem(
                    anomaly_type="CONVERSION_DROP",
                    severity="WARN" if drop_pct < 40 else "CRITICAL",
                    description=f"Today's conversion rate ({today_rate:.1%}) is {drop_pct}% below the 7-day average ({avg_7day:.1%})",
                    suggested_action="Check for staff shortages, product availability issues, or pricing anomalies. Review billing queue abandonment.",
                    detected_at=now,
                    value=today_rate,
                    threshold=CONVERSION_DROP_THRESHOLD * avg_7day,
                ))

        # ----------------------------------------------------------------
        # 3. DEAD_ZONE — product zone with no visits in last 30 minutes
        #    Only flag if the store has had traffic in the last 30 min
        # ----------------------------------------------------------------
        window_start = now - timedelta(minutes=DEAD_ZONE_MINUTES)

        # Check if store has any traffic in the window
        traffic_check = await db.execute(text("""
            SELECT COUNT(*) AS cnt
            FROM events
            WHERE store_id = :store_id
              AND is_staff = FALSE
              AND timestamp >= :window_start
        """), {"store_id": store_id, "window_start": window_start})
        has_traffic = (traffic_check.scalar() or 0) > 0

        if has_traffic:
            # Zones that had at least one visit in the window
            active_zones = await db.execute(text("""
                SELECT DISTINCT zone_id
                FROM events
                WHERE store_id = :store_id
                  AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')
                  AND zone_id IS NOT NULL
                  AND is_staff = FALSE
                  AND timestamp >= :window_start
            """), {"store_id": store_id, "window_start": window_start})
            active_zone_ids = {r.zone_id for r in active_zones.fetchall()}

            # Zones that existed in the store today (to avoid flagging zones
            # that simply don't exist in this store's layout)
            known_zones = await db.execute(text("""
                SELECT DISTINCT zone_id
                FROM events
                WHERE store_id = :store_id
                  AND zone_id IS NOT NULL
                  AND timestamp >= :day_start
                  AND timestamp < :day_end
            """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
            known_zone_ids = {r.zone_id for r in known_zones.fetchall()} & PRODUCT_ZONES

            dead_zones = known_zone_ids - active_zone_ids
            for zone in sorted(dead_zones):
                anomalies.append(AnomalyItem(
                    anomaly_type="DEAD_ZONE",
                    severity="INFO",
                    description=f"Zone '{zone}' has had no customer visits in the last {DEAD_ZONE_MINUTES} minutes.",
                    suggested_action=f"Check if zone '{zone}' is accessible and properly stocked. Consider repositioning staff to guide customers.",
                    detected_at=now,
                    zone_id=zone,
                    value=0.0,
                    threshold=float(DEAD_ZONE_MINUTES),
                ))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"anomalies query failed for {store_id}: {e}")
        raise HTTPException(
            status_code=503,
            detail={"error": "database_unavailable", "message": "Anomaly detection failed"},
        )

    return AnomaliesResponse(
        store_id=store_id,
        anomalies=anomalies,
        checked_at=now,
    )
