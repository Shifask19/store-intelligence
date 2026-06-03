"""
app/heatmap.py — GET /stores/{id}/heatmap

Zone visit frequency + avg dwell, normalised 0–100.
data_confidence flag is False when fewer than 20 sessions exist in the window
(too little data to trust the heatmap).
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import HeatmapResponse, HeatmapZone

logger = logging.getLogger(__name__)
router = APIRouter()

LOW_CONFIDENCE_THRESHOLD = 20  # sessions below this → data_confidence=False


@router.get(
    "/stores/{store_id}/heatmap",
    response_model=HeatmapResponse,
    summary="Zone visit frequency heatmap, normalised 0–100",
)
async def get_heatmap(
    store_id: str,
    date_str: Optional[str] = Query(None, alias="date", description="YYYY-MM-DD"),
    db: AsyncSession = Depends(get_db),
) -> HeatmapResponse:
    """
    Returns per-zone visit frequency and avg dwell, normalised to 0–100.

    Normalisation: score = (zone_visits / max_zone_visits) * 100
    This makes the heatmap relative — the busiest zone always scores 100.
    """
    try:
        target_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    day_start = datetime(target_date.year, target_date.month, target_date.day,
                         tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    try:
        # Total unique sessions for confidence flag
        session_result = await db.execute(text("""
            SELECT COUNT(DISTINCT visitor_id) AS sessions
            FROM events
            WHERE store_id = :store_id
              AND is_staff = FALSE
              AND timestamp >= :day_start
              AND timestamp < :day_end
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        total_sessions = session_result.scalar() or 0
        data_confidence = total_sessions >= LOW_CONFIDENCE_THRESHOLD

        # Per-zone stats: visit count + avg dwell
        zone_result = await db.execute(text("""
            SELECT
                zone_id,
                MAX(sku_zone)       AS sku_zone,
                COUNT(*)            AS visit_count,
                AVG(dwell_ms)       AS avg_dwell_ms
            FROM events
            WHERE store_id = :store_id
              AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL', 'ZONE_EXIT')
              AND zone_id IS NOT NULL
              AND zone_id NOT IN ('ENTRY_ZONE')
              AND is_staff = FALSE
              AND timestamp >= :day_start
              AND timestamp < :day_end
            GROUP BY zone_id
            ORDER BY visit_count DESC
        """), {"store_id": store_id, "day_start": day_start, "day_end": day_end})
        rows = zone_result.fetchall()

        if not rows:
            return HeatmapResponse(store_id=store_id, zones=[], data_confidence=data_confidence)

        max_visits = max(int(r.visit_count) for r in rows)

        zones = []
        for row in rows:
            visit_count = int(row.visit_count)
            avg_dwell = float(row.avg_dwell_ms or 0)
            normalised = round((visit_count / max_visits) * 100, 1) if max_visits > 0 else 0.0
            zones.append(
                HeatmapZone(
                    zone_id=row.zone_id,
                    sku_zone=row.sku_zone,
                    visit_frequency=visit_count,
                    avg_dwell_ms=round(avg_dwell, 1),
                    normalised_score=normalised,
                    data_confidence=data_confidence,
                )
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"heatmap query failed for {store_id}: {e}")
        raise HTTPException(
            status_code=503,
            detail={"error": "database_unavailable", "message": "Heatmap query failed"},
        )

    return HeatmapResponse(
        store_id=store_id,
        zones=zones,
        data_confidence=data_confidence,
    )
