#!/bin/bash
# startup.sh — Initialize DB, seed events, start API
set -e

export DATABASE_URL="sqlite+aiosqlite:///./demo.db"

echo "=== Seeding database with demo events ==="

# Seed events via Python (faster than HTTP ingest on startup)
python -c "
import asyncio, json, os, sys
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text
sys.path.insert(0, '.')

DATABASE_URL = 'sqlite+aiosqlite:///./demo.db'

async def seed():
    engine = create_async_engine(DATABASE_URL, echo=False)
    from app.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.connect() as conn:
        # Check if already seeded
        result = await conn.execute(text('SELECT COUNT(*) FROM events'))
        count = result.scalar()
        if count > 0:
            print(f'DB already has {count} events, skipping seed.')
            await engine.dispose()
            return

    # Load and insert events
    events_file = 'data/events_final.jsonl'
    if not os.path.exists(events_file):
        print('No events file found, starting empty.')
        await engine.dispose()
        return

    lines = [l.strip() for l in open(events_file) if l.strip()]
    print(f'Seeding {len(lines)} events...')

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    insert_sql = text('''
        INSERT OR IGNORE INTO events (
            event_id, store_id, camera_id, visitor_id, event_type,
            timestamp, zone_id, dwell_ms, is_staff, confidence,
            queue_depth, sku_zone, session_seq, ingested_at
        ) VALUES (
            :event_id, :store_id, :camera_id, :visitor_id, :event_type,
            :timestamp, :zone_id, :dwell_ms, :is_staff, :confidence,
            :queue_depth, :sku_zone, :session_seq, :ingested_at
        )
    ''')

    async with engine.begin() as conn:
        for line in lines:
            try:
                e = json.loads(line)
                meta = e.get('metadata', {}) or {}
                await conn.execute(insert_sql, {
                    'event_id':   e.get('event_id', ''),
                    'store_id':   e.get('store_id', ''),
                    'camera_id':  e.get('camera_id', ''),
                    'visitor_id': e.get('visitor_id', ''),
                    'event_type': e.get('event_type', ''),
                    'timestamp':  e.get('timestamp', ''),
                    'zone_id':    e.get('zone_id'),
                    'dwell_ms':   e.get('dwell_ms', 0),
                    'is_staff':   e.get('is_staff', False),
                    'confidence': e.get('confidence', 0.5),
                    'queue_depth': meta.get('queue_depth'),
                    'sku_zone':   meta.get('sku_zone'),
                    'session_seq': meta.get('session_seq', 0),
                    'ingested_at': str(now),
                })
            except Exception as ex:
                pass

    # Seed demo POS transactions for non-zero conversion rate
    pos_sql = text('''
        INSERT OR IGNORE INTO pos_transactions
            (store_id, transaction_id, timestamp, basket_value_inr)
        VALUES (:s, :t, :ts, :a)
    ''')
    demo_txns = [
        ('STORE_BLR_002', 'DEMO_TXN_001', '2026-04-10T10:03:00', 850.0),
        ('STORE_BLR_002', 'DEMO_TXN_002', '2026-04-10T10:03:45', 1240.0),
        ('STORE_BLR_002', 'DEMO_TXN_003', '2026-04-10T10:04:30', 560.0),
    ]
    async with engine.begin() as conn:
        for s, t, ts, a in demo_txns:
            try:
                await conn.execute(pos_sql, {'s': s, 't': t, 'ts': ts, 'a': a})
            except:
                pass

    await engine.dispose()
    print('Seeding complete.')

asyncio.run(seed())
"

echo "=== Starting API ==="
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
