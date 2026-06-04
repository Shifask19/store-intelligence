#!/bin/bash
# startup.sh — Render/Railway startup: seed SQLite DB, then start API
set -e

export DATABASE_URL="sqlite+aiosqlite:///./demo.db"
export DB_NULL_POOL="false"

echo "=== Store Intelligence API — startup ==="
echo "DATABASE_URL: $DATABASE_URL"

python - <<'PYEOF'
import asyncio, json, os, sys
sys.path.insert(0, '.')

DB_URL = "sqlite+aiosqlite:///./demo.db"

async def seed():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from app.models import Base
    import datetime

    engine = create_async_engine(DB_URL, echo=False)

    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Tables created.")

    # Check if already seeded
    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT COUNT(*) FROM events"))).scalar()
        if count > 0:
            print(f"Already seeded ({count} events). Skipping.")
            await engine.dispose()
            return

    # Seed events
    events_file = "data/events_final.jsonl"
    if not os.path.exists(events_file):
        print("No events file, starting empty.")
        await engine.dispose()
        return

    lines = [l.strip() for l in open(events_file) if l.strip()]
    print(f"Seeding {len(lines)} events...")
    now = str(datetime.datetime.utcnow())

    sql = text("""
        INSERT OR IGNORE INTO events (
            event_id, store_id, camera_id, visitor_id, event_type,
            timestamp, zone_id, dwell_ms, is_staff, confidence,
            queue_depth, sku_zone, session_seq, ingested_at
        ) VALUES (
            :event_id,:store_id,:camera_id,:visitor_id,:event_type,
            :timestamp,:zone_id,:dwell_ms,:is_staff,:confidence,
            :queue_depth,:sku_zone,:session_seq,:ingested_at
        )
    """)

    async with engine.begin() as conn:
        for line in lines:
            try:
                e = json.loads(line)
                m = e.get("metadata") or {}
                await conn.execute(sql, {
                    "event_id":    e.get("event_id",""),
                    "store_id":    e.get("store_id",""),
                    "camera_id":   e.get("camera_id",""),
                    "visitor_id":  e.get("visitor_id",""),
                    "event_type":  e.get("event_type",""),
                    "timestamp":   e.get("timestamp",""),
                    "zone_id":     e.get("zone_id"),
                    "dwell_ms":    e.get("dwell_ms",0),
                    "is_staff":    1 if e.get("is_staff") else 0,
                    "confidence":  e.get("confidence",0.5),
                    "queue_depth": m.get("queue_depth"),
                    "sku_zone":    m.get("sku_zone"),
                    "session_seq": m.get("session_seq",0),
                    "ingested_at": now,
                })
            except Exception as ex:
                pass
    print("Events seeded.")

    # Seed demo POS transactions for non-zero conversion rate
    pos_sql = text("""
        INSERT OR IGNORE INTO pos_transactions
            (store_id, transaction_id, timestamp, basket_value_inr)
        VALUES (:s,:t,:ts,:a)
    """)
    demo = [
        ("STORE_BLR_002","DEMO_TXN_001","2026-04-10T10:03:00",850.0),
        ("STORE_BLR_002","DEMO_TXN_002","2026-04-10T10:03:45",1240.0),
        ("STORE_BLR_002","DEMO_TXN_003","2026-04-10T10:04:30",560.0),
    ]
    async with engine.begin() as conn:
        for s,t,ts,a in demo:
            try:
                await conn.execute(pos_sql,{"s":s,"t":t,"ts":ts,"a":a})
            except:
                pass
    print("POS transactions seeded.")
    await engine.dispose()

asyncio.run(seed())
PYEOF

echo "=== Starting uvicorn on port ${PORT:-8000} ==="
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1 \
    --log-level info
