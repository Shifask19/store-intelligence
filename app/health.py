"""
app/health.py — GET /health

Service health endpoint — the first thing an on-call engineer checks.

Returns:
  - Overall status: healthy | degraded
  - Per-store: last event timestamp, lag in minutes, STALE_FEED warning if >10 min
  - DB connectivity status
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import check_db_health, get_db
from app.models import HealthResponse, StoreHealth

logger = logging.getLogger(__name__)
router = APIRouter()

STALE_FEED_MINUTES = 10  # lag threshold for STALE_FEED warning


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health — DB status + per-store feed freshness",
)
async def get_health(db: AsyncSession = Depends(get_db)) -> HealthResponse:
    """
    Checks:
    1. DB connectivity (SELECT 1)
    2. For each store with events in the last 24h: last event timestamp + lag
    3. Flags STALE_FEED if any store's last event is >10 minutes old
    """
    now = datetime.now(timezone.utc)
    db_ok = await check_db_health()
    db_status = "ok" if db_ok else "unavailable"

    store_healths: list[StoreHealth] = []
    overall_status = "healthy"

    if db_ok:
        try:
            # Get last event timestamp per store (last 24h window)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            result = await db.execute(text("""
                SELECT store_id, MAX(timestamp) AS last_ts
                FROM events
                WHERE timestamp >= :cutoff
                GROUP BY store_id
                ORDER BY store_id
            """), {"cutoff": cutoff})
            rows = result.fetchall()

            for row in rows:
                last_ts: datetime = row.last_ts
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)

                lag_minutes = (now - last_ts).total_seconds() / 60

                if lag_minutes > STALE_FEED_MINUTES:
                    status = "STALE_FEED"
                    warning = f"No events received for {lag_minutes:.1f} minutes (threshold: {STALE_FEED_MINUTES} min)"
                    overall_status = "degraded"
                else:
                    status = "OK"
                    warning = None

                store_healths.append(StoreHealth(
                    store_id=row.store_id,
                    status=status,
                    last_event_at=last_ts,
                    lag_minutes=round(lag_minutes, 1),
                    warning=warning,
                ))

            if not store_healths:
                # No stores have sent events in 24h — still healthy, just no data
                overall_status = "healthy"

        except Exception as e:
            logger.error(f"health check query failed: {e}")
            overall_status = "degraded"
    else:
        overall_status = "degraded"

    return HealthResponse(
        status=overall_status,
        stores=store_healths,
        checked_at=now,
        db_status=db_status,
    )
