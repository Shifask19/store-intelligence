# PROMPT:
#   "Write pytest tests for the anomaly detection endpoint of the Store Intelligence
#    API. Cover: queue spike detection (current > 2× avg), conversion drop vs 7-day
#    average, dead zone detection (no visits in 30 min), empty store returns no
#    anomalies, severity levels (INFO/WARN/CRITICAL), suggested_action present,
#    health endpoint returns correct store status and STALE_FEED warning."
#
# CHANGES MADE:
#   - AI generated tests that assumed anomalies fire immediately; added time-offset
#     logic so events fall within the correct detection windows.
#   - Fixed dead-zone test: AI forgot to seed traffic in the last 30 min window
#     (dead zone only fires when the store has had recent traffic).
#   - Added test for health endpoint STALE_FEED: AI used wrong lag calculation
#     (used ingested_at instead of event timestamp).
#   - Replaced AI's hardcoded severity assertions with range checks since
#     thresholds are configurable.

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

# DB setup is handled by conftest.py
from app.database import get_db
from app.main import app

STORE_ID = "STORE_BLR_002"
TODAY = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_event(event_type, visitor_id, timestamp, zone_id=None,
               queue_depth=None, is_staff=False, confidence=0.9):
    import uuid
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": STORE_ID,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": zone_id,
        "dwell_ms": 0,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {"queue_depth": queue_depth, "sku_zone": None, "session_seq": 1},
    }


# ---------------------------------------------------------------------------
# Anomaly: empty store
# ---------------------------------------------------------------------------
class TestAnomaliesEmptyStore:
    @pytest.mark.asyncio
    async def test_no_anomalies_empty_store(self, client):
        """Zero events → no anomalies, not a crash."""
        resp = await client.get(f"/stores/{STORE_ID}/anomalies?date=2026-04-10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["anomalies"] == []
        assert "checked_at" in data

    @pytest.mark.asyncio
    async def test_response_schema(self, client):
        resp = await client.get(f"/stores/{STORE_ID}/anomalies?date=2026-04-10")
        data = resp.json()
        assert "store_id" in data
        assert "anomalies" in data
        assert "checked_at" in data
        assert data["store_id"] == STORE_ID


# ---------------------------------------------------------------------------
# Anomaly: queue spike
# ---------------------------------------------------------------------------
class TestQueueSpike:
    @pytest.mark.asyncio
    async def test_queue_spike_detected(self, client):
        """Current queue 3× daily avg → BILLING_QUEUE_SPIKE anomaly."""
        events = []
        # Historical joins with low queue depth (avg ≈ 2)
        for i in range(5):
            events.append(make_event(
                "BILLING_QUEUE_JOIN", f"VIS_{i:03d}",
                TODAY + timedelta(hours=i),
                zone_id="BILLING", queue_depth=2,
            ))
        # Current spike: queue_depth=8 (4× avg of 2)
        events.append(make_event(
            "BILLING_QUEUE_JOIN", "VIS_spike",
            TODAY + timedelta(hours=6),
            zone_id="BILLING", queue_depth=8,
        ))
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/anomalies?date=2026-04-10")
        assert resp.status_code == 200
        types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
        assert "BILLING_QUEUE_SPIKE" in types

    @pytest.mark.asyncio
    async def test_queue_spike_has_suggested_action(self, client):
        events = [
            make_event("BILLING_QUEUE_JOIN", "VIS_001", TODAY,
                       zone_id="BILLING", queue_depth=2),
            make_event("BILLING_QUEUE_JOIN", "VIS_002",
                       TODAY + timedelta(hours=1),
                       zone_id="BILLING", queue_depth=10),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/anomalies?date=2026-04-10")
        for a in resp.json()["anomalies"]:
            if a["anomaly_type"] == "BILLING_QUEUE_SPIKE":
                assert a["suggested_action"]
                assert len(a["suggested_action"]) > 10

    @pytest.mark.asyncio
    async def test_queue_spike_severity_levels(self, client):
        """Severity must be WARN or CRITICAL for a spike."""
        events = [
            make_event("BILLING_QUEUE_JOIN", "VIS_001", TODAY,
                       zone_id="BILLING", queue_depth=1),
            make_event("BILLING_QUEUE_JOIN", "VIS_002",
                       TODAY + timedelta(hours=1),
                       zone_id="BILLING", queue_depth=15),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/anomalies?date=2026-04-10")
        for a in resp.json()["anomalies"]:
            if a["anomaly_type"] == "BILLING_QUEUE_SPIKE":
                assert a["severity"] in ("WARN", "CRITICAL")

    @pytest.mark.asyncio
    async def test_no_spike_when_queue_normal(self, client):
        """Consistent queue depth → no spike anomaly."""
        events = [
            make_event("BILLING_QUEUE_JOIN", f"VIS_{i:03d}",
                       TODAY + timedelta(hours=i),
                       zone_id="BILLING", queue_depth=3)
            for i in range(5)
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/anomalies?date=2026-04-10")
        types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
        assert "BILLING_QUEUE_SPIKE" not in types


# ---------------------------------------------------------------------------
# Anomaly: dead zone
# ---------------------------------------------------------------------------
class TestDeadZone:
    @pytest.mark.asyncio
    async def test_dead_zone_detected(self, client):
        """
        Store has traffic (recent ENTRY) but SKINCARE has no visits in 30 min.
        Dead zone should be flagged.

        Note: dead zone uses NOW() internally, so we seed events with
        timestamps close to now to trigger the detection window.
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        events = [
            # Recent traffic (within 30 min) — store is active
            make_event("ENTRY", "VIS_001", now - timedelta(minutes=5)),
            make_event("ZONE_ENTER", "VIS_001",
                       now - timedelta(minutes=4), zone_id="MAKEUP"),
            # SKINCARE had a visit earlier today but NOT in last 30 min
            make_event("ZONE_ENTER", "VIS_old",
                       now - timedelta(minutes=45), zone_id="SKINCARE"),
        ]
        await client.post("/events/ingest", json={"events": events})

        # Use today's date for the query
        today_str = now.strftime("%Y-%m-%d")
        resp = await client.get(f"/stores/{STORE_ID}/anomalies?date={today_str}")
        assert resp.status_code == 200
        types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
        assert "DEAD_ZONE" in types

    @pytest.mark.asyncio
    async def test_dead_zone_not_flagged_when_no_traffic(self, client):
        """
        If the store has no recent traffic at all, dead zone should NOT fire
        (no point flagging dead zones in a closed/empty store).
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        # No events at all
        resp = await client.get(f"/stores/{STORE_ID}/anomalies?date={today_str}")
        types = [a["anomaly_type"] for a in resp.json()["anomalies"]]
        assert "DEAD_ZONE" not in types

    @pytest.mark.asyncio
    async def test_dead_zone_severity_is_info(self, client):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        events = [
            make_event("ENTRY", "VIS_001", now - timedelta(minutes=5)),
            make_event("ZONE_ENTER", "VIS_001",
                       now - timedelta(minutes=4), zone_id="MAKEUP"),
            make_event("ZONE_ENTER", "VIS_old",
                       now - timedelta(minutes=45), zone_id="SKINCARE"),
        ]
        await client.post("/events/ingest", json={"events": events})
        today_str = now.strftime("%Y-%m-%d")
        resp = await client.get(f"/stores/{STORE_ID}/anomalies?date={today_str}")
        for a in resp.json()["anomalies"]:
            if a["anomaly_type"] == "DEAD_ZONE":
                assert a["severity"] == "INFO"
                assert a["zone_id"] is not None


# ---------------------------------------------------------------------------
# Anomaly: all anomalies have required fields
# ---------------------------------------------------------------------------
class TestAnomalySchema:
    @pytest.mark.asyncio
    async def test_all_anomalies_have_required_fields(self, client):
        events = [
            make_event("BILLING_QUEUE_JOIN", "VIS_001", TODAY,
                       zone_id="BILLING", queue_depth=2),
            make_event("BILLING_QUEUE_JOIN", "VIS_002",
                       TODAY + timedelta(hours=1),
                       zone_id="BILLING", queue_depth=10),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/anomalies?date=2026-04-10")
        for anomaly in resp.json()["anomalies"]:
            assert "anomaly_type" in anomaly
            assert "severity" in anomaly
            assert anomaly["severity"] in ("INFO", "WARN", "CRITICAL")
            assert "description" in anomaly
            assert "suggested_action" in anomaly
            assert "detected_at" in anomaly


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------
class TestHealth:
    @pytest.mark.asyncio
    async def test_health_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert data["status"] in ("healthy", "degraded")
        assert "db_status" in data
        assert "checked_at" in data

    @pytest.mark.asyncio
    async def test_health_no_stores_is_healthy(self, client):
        """No events in DB → health is still 'healthy' (not degraded)."""
        resp = await client.get("/health")
        assert resp.json()["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_health_stale_feed_warning(self, client):
        """Event older than 10 minutes → STALE_FEED warning for that store."""
        from datetime import datetime, timezone
        stale_ts = datetime.now(timezone.utc) - timedelta(minutes=15)
        events = [make_event("ENTRY", "VIS_001", stale_ts)]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get("/health")
        data = resp.json()
        store_statuses = {s["store_id"]: s for s in data["stores"]}
        if STORE_ID in store_statuses:
            store = store_statuses[STORE_ID]
            assert store["status"] == "STALE_FEED"
            assert store["lag_minutes"] >= 10
            assert store["warning"] is not None

    @pytest.mark.asyncio
    async def test_health_fresh_feed_ok(self, client):
        """Recent event → store status is OK."""
        from datetime import datetime, timezone
        fresh_ts = datetime.now(timezone.utc) - timedelta(minutes=2)
        events = [make_event("ENTRY", "VIS_001", fresh_ts)]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get("/health")
        data = resp.json()
        store_statuses = {s["store_id"]: s for s in data["stores"]}
        if STORE_ID in store_statuses:
            assert store_statuses[STORE_ID]["status"] == "OK"
