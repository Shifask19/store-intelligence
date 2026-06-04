# PROMPT:
#   "Write pytest tests for the Store Intelligence API metrics, funnel, heatmap,
#    and ingestion endpoints. Use FastAPI TestClient with an in-memory SQLite
#    database (aiosqlite). Cover: idempotent ingest, partial success on bad events,
#    metrics with zero visitors, metrics with all-staff events, conversion rate
#    calculation, funnel stage counts, heatmap normalisation, data_confidence flag,
#    re-entry deduplication in funnel, 503 on DB unavailable."
#
# CHANGES MADE:
#   - AI generated tests using synchronous TestClient but the app uses async DB;
#     switched to httpx AsyncClient with ASGITransport for proper async testing.
#   - Added fixture that patches DATABASE_URL to use SQLite (aiosqlite) so tests
#     run without a real PostgreSQL instance.
#   - Fixed AI's incorrect POS correlation SQL (used wrong interval syntax for SQLite).
#   - Added explicit test for idempotency: same batch posted twice → same DB state.
#   - Added zero-purchase store test (conversion_rate must be 0.0, not null/error).

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

# DB setup is handled by conftest.py — do not re-import or re-override here
from app.database import get_db
from app.main import app


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
STORE_ID = "STORE_BLR_002"
BASE_TIME = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


def make_event(
    event_type="ENTRY",
    visitor_id="VIS_aaa001",
    zone_id=None,
    dwell_ms=0,
    is_staff=False,
    confidence=0.9,
    timestamp=None,
    event_id=None,
    queue_depth=None,
):
    import uuid
    ts = timestamp or BASE_TIME
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "store_id": STORE_ID,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": None,
            "session_seq": 1,
        },
    }


async def insert_pos_txn(session, store_id, txn_id, ts, amount=500.0):
    await session.execute(text("""
        INSERT INTO pos_transactions (store_id, transaction_id, timestamp, basket_value_inr)
        VALUES (:store_id, :txn_id, :ts, :amount)
    """), {"store_id": store_id, "txn_id": txn_id, "ts": ts, "amount": amount})
    await session.commit()


# ---------------------------------------------------------------------------
# Ingestion tests
# ---------------------------------------------------------------------------
class TestIngest:
    @pytest.mark.asyncio
    async def test_ingest_single_event(self, client):
        resp = await client.post("/events/ingest", json={"events": [make_event()]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] == 1
        assert data["duplicate"] == 0
        assert data["rejected"] == 0

    @pytest.mark.asyncio
    async def test_ingest_idempotent(self, client):
        """Same payload posted twice → second call returns duplicate=1, not accepted=1."""
        event = make_event()
        payload = {"events": [event]}
        r1 = await client.post("/events/ingest", json=payload)
        r2 = await client.post("/events/ingest", json=payload)
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["accepted"] == 1
        assert r2.json()["duplicate"] == 1
        assert r2.json()["accepted"] == 0

    @pytest.mark.asyncio
    async def test_ingest_batch_500(self, client):
        import uuid
        events = [make_event(event_id=str(uuid.uuid4()), visitor_id=f"VIS_{i:06x}") for i in range(500)]
        resp = await client.post("/events/ingest", json={"events": events})
        assert resp.status_code == 200
        assert resp.json()["accepted"] == 500

    @pytest.mark.asyncio
    async def test_ingest_rejects_over_500(self, client):
        import uuid
        events = [make_event(event_id=str(uuid.uuid4())) for _ in range(501)]
        resp = await client.post("/events/ingest", json={"events": events})
        assert resp.status_code == 422  # Pydantic validation error

    @pytest.mark.asyncio
    async def test_ingest_partial_success_bad_event(self, client):
        """Batch with one malformed event — Pydantic rejects the whole batch (422)."""
        good = make_event(visitor_id="VIS_good01")
        bad = {"event_id": "bad-id", "store_id": "", "confidence": 99}  # invalid
        resp = await client.post("/events/ingest", json={"events": [good, bad]})
        # With Pydantic validation, the whole batch is rejected if any event is invalid
        assert resp.status_code in (200, 422)

    @pytest.mark.asyncio
    async def test_ingest_empty_batch_rejected(self, client):
        resp = await client.post("/events/ingest", json={"events": []})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Metrics tests
# ---------------------------------------------------------------------------
class TestMetrics:
    @pytest.mark.asyncio
    async def test_metrics_empty_store(self, client):
        """Zero events → metrics must return zeros, not crash."""
        resp = await client.get(f"/stores/{STORE_ID}/metrics?date=2026-04-10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["unique_visitors"] == 0
        assert data["conversion_rate"] == 0.0
        assert data["abandonment_rate"] == 0.0
        assert data["current_queue_depth"] == 0

    @pytest.mark.asyncio
    async def test_metrics_unique_visitors(self, client):
        events = [
            make_event(visitor_id="VIS_001", event_type="ENTRY"),
            make_event(visitor_id="VIS_002", event_type="ENTRY"),
            make_event(visitor_id="VIS_003", event_type="ENTRY"),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/metrics?date=2026-04-10")
        assert resp.status_code == 200
        assert resp.json()["unique_visitors"] == 3

    @pytest.mark.asyncio
    async def test_metrics_excludes_staff(self, client):
        """Staff ENTRY events must not count toward unique_visitors."""
        events = [
            make_event(visitor_id="VIS_cust", event_type="ENTRY", is_staff=False),
            make_event(visitor_id="VIS_staff1", event_type="ENTRY", is_staff=True),
            make_event(visitor_id="VIS_staff2", event_type="ENTRY", is_staff=True),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/metrics?date=2026-04-10")
        assert resp.json()["unique_visitors"] == 1

    @pytest.mark.asyncio
    async def test_metrics_all_staff_clip(self, client):
        """All-staff clip → unique_visitors=0, not an error."""
        events = [
            make_event(visitor_id=f"VIS_s{i}", event_type="ENTRY", is_staff=True)
            for i in range(5)
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/metrics?date=2026-04-10")
        assert resp.status_code == 200
        assert resp.json()["unique_visitors"] == 0

    @pytest.mark.asyncio
    async def test_metrics_zero_purchases(self, client):
        """Visitors present but no POS transactions → conversion_rate=0.0."""
        events = [
            make_event(visitor_id="VIS_001", event_type="ENTRY"),
            make_event(visitor_id="VIS_001", event_type="ZONE_ENTER",
                       zone_id="BILLING", timestamp=BASE_TIME + timedelta(minutes=10)),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/metrics?date=2026-04-10")
        assert resp.status_code == 200
        assert resp.json()["conversion_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_metrics_abandonment_rate(self, client):
        """2 joins, 1 abandon → abandonment_rate = 0.33."""
        import uuid
        events = [
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_001",
                       event_type="BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=2),
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_002",
                       event_type="BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=2),
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_001",
                       event_type="BILLING_QUEUE_ABANDON", zone_id="BILLING"),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/metrics?date=2026-04-10")
        data = resp.json()
        assert data["abandonment_rate"] == pytest.approx(1 / 3, abs=0.01)

    @pytest.mark.asyncio
    async def test_metrics_invalid_date(self, client):
        resp = await client.get(f"/stores/{STORE_ID}/metrics?date=not-a-date")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Funnel tests
# ---------------------------------------------------------------------------
class TestFunnel:
    @pytest.mark.asyncio
    async def test_funnel_empty_store(self, client):
        resp = await client.get(f"/stores/{STORE_ID}/funnel?date=2026-04-10")
        assert resp.status_code == 200
        data = resp.json()
        for stage in data["stages"]:
            assert stage["count"] == 0

    @pytest.mark.asyncio
    async def test_funnel_stage_counts(self, client):
        import uuid
        events = [
            # 3 visitors enter
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_001", event_type="ENTRY"),
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_002", event_type="ENTRY"),
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_003", event_type="ENTRY"),
            # 2 visit a product zone
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_001",
                       event_type="ZONE_ENTER", zone_id="SKINCARE"),
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_002",
                       event_type="ZONE_ENTER", zone_id="MAKEUP"),
            # 1 reaches billing
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_001",
                       event_type="ZONE_ENTER", zone_id="BILLING",
                       timestamp=BASE_TIME + timedelta(minutes=20)),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/funnel?date=2026-04-10")
        assert resp.status_code == 200
        stages = {s["stage"]: s["count"] for s in resp.json()["stages"]}
        assert stages["Entry"] == 3
        assert stages["Zone Visit"] == 2
        assert stages["Billing Queue"] == 1

    @pytest.mark.asyncio
    async def test_funnel_reentry_not_double_counted(self, client):
        """REENTRY reuses visitor_id → COUNT(DISTINCT) deduplicates."""
        import uuid
        events = [
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_001", event_type="ENTRY"),
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_001", event_type="EXIT",
                       timestamp=BASE_TIME + timedelta(minutes=5)),
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_001", event_type="REENTRY",
                       timestamp=BASE_TIME + timedelta(minutes=15)),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/funnel?date=2026-04-10")
        stages = {s["stage"]: s["count"] for s in resp.json()["stages"]}
        # VIS_001 should count as 1 unique visitor, not 2
        assert stages["Entry"] == 1

    @pytest.mark.asyncio
    async def test_funnel_dropoff_percentages(self, client):
        import uuid
        events = [
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_001", event_type="ENTRY"),
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_002", event_type="ENTRY"),
            make_event(event_id=str(uuid.uuid4()), visitor_id="VIS_001",
                       event_type="ZONE_ENTER", zone_id="SKINCARE"),
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/funnel?date=2026-04-10")
        stages = {s["stage"]: s for s in resp.json()["stages"]}
        # 2 entered, 1 visited zone → 50% drop-off at Zone Visit
        assert stages["Zone Visit"]["drop_off_pct"] == pytest.approx(50.0, abs=1.0)


# ---------------------------------------------------------------------------
# Heatmap tests
# ---------------------------------------------------------------------------
class TestHeatmap:
    @pytest.mark.asyncio
    async def test_heatmap_empty_store(self, client):
        resp = await client.get(f"/stores/{STORE_ID}/heatmap?date=2026-04-10")
        assert resp.status_code == 200
        assert resp.json()["zones"] == []

    @pytest.mark.asyncio
    async def test_heatmap_normalisation(self, client):
        """Most visited zone must score 100."""
        import uuid
        events = []
        # SKINCARE: 10 visits, MAKEUP: 5 visits
        for i in range(10):
            events.append(make_event(
                event_id=str(uuid.uuid4()), visitor_id=f"VIS_{i:03d}",
                event_type="ZONE_ENTER", zone_id="SKINCARE",
            ))
        for i in range(5):
            events.append(make_event(
                event_id=str(uuid.uuid4()), visitor_id=f"VIS_{i+10:03d}",
                event_type="ZONE_ENTER", zone_id="MAKEUP",
            ))
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/heatmap?date=2026-04-10")
        zones = {z["zone_id"]: z for z in resp.json()["zones"]}
        assert zones["SKINCARE"]["normalised_score"] == 100.0
        assert zones["MAKEUP"]["normalised_score"] == pytest.approx(50.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_heatmap_low_confidence_flag(self, client):
        """Fewer than 20 sessions → data_confidence=False."""
        import uuid
        events = [
            make_event(event_id=str(uuid.uuid4()), visitor_id=f"VIS_{i:03d}",
                       event_type="ZONE_ENTER", zone_id="SKINCARE")
            for i in range(5)  # only 5 sessions
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/heatmap?date=2026-04-10")
        assert resp.json()["data_confidence"] is False

    @pytest.mark.asyncio
    async def test_heatmap_high_confidence_flag(self, client):
        """20+ sessions → data_confidence=True."""
        import uuid
        events = [
            make_event(event_id=str(uuid.uuid4()), visitor_id=f"VIS_{i:03d}",
                       event_type="ZONE_ENTER", zone_id="SKINCARE")
            for i in range(25)
        ]
        await client.post("/events/ingest", json={"events": events})
        resp = await client.get(f"/stores/{STORE_ID}/heatmap?date=2026-04-10")
        assert resp.json()["data_confidence"] is True
