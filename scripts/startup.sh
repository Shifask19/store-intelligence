#!/bin/bash
# startup.sh — Render startup: seed SQLite DB synchronously, then start API
set -e

export DATABASE_URL="sqlite+aiosqlite:///./demo.db"
export DB_NULL_POOL="false"

echo "=== Store Intelligence API startup ==="
echo "Working dir: $(pwd)"
echo "Events file exists: $(test -f data/events_final.jsonl && echo YES || echo NO)"

# Seed the database synchronously before starting the server
python3 - <<'EOF'
import sqlite3, json, os, uuid
from datetime import datetime, timezone

DB_PATH = "./demo.db"
EVENTS_FILE = "data/events_final.jsonl"

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# Create tables
cur.executescript("""
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    visitor_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    zone_id TEXT,
    dwell_ms INTEGER NOT NULL DEFAULT 0,
    is_staff INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL,
    queue_depth INTEGER,
    sku_zone TEXT,
    session_seq INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT,
    UNIQUE(event_id)
);
CREATE TABLE IF NOT EXISTS pos_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    basket_value_inr REAL NOT NULL DEFAULT 0,
    UNIQUE(transaction_id)
);
""")

# Check if already seeded
cur.execute("SELECT COUNT(*) FROM events")
count = cur.fetchone()[0]
if count > 0:
    print(f"Already seeded: {count} events")
    conn.close()
    exit(0)

# Seed events
if not os.path.exists(EVENTS_FILE):
    print(f"WARNING: {EVENTS_FILE} not found")
    conn.close()
    exit(0)

lines = [l.strip() for l in open(EVENTS_FILE) if l.strip()]
print(f"Seeding {len(lines)} events...")
now = datetime.now(timezone.utc).isoformat()

for line in lines:
    try:
        e = json.loads(line)
        m = e.get("metadata") or {}
        cur.execute("""
            INSERT OR IGNORE INTO events
                (event_id, store_id, camera_id, visitor_id, event_type,
                 timestamp, zone_id, dwell_ms, is_staff, confidence,
                 queue_depth, sku_zone, session_seq, ingested_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            e.get("event_id", str(uuid.uuid4())),
            e.get("store_id", ""),
            e.get("camera_id", ""),
            e.get("visitor_id", ""),
            e.get("event_type", ""),
            e.get("timestamp", ""),
            e.get("zone_id"),
            e.get("dwell_ms", 0),
            1 if e.get("is_staff") else 0,
            e.get("confidence", 0.5),
            m.get("queue_depth"),
            m.get("sku_zone"),
            m.get("session_seq", 0),
            now,
        ))
    except Exception as ex:
        pass

# Seed demo POS transactions
for txn_id, ts, amount in [
    ("DEMO_TXN_001", "2026-04-10T10:03:00", 850.0),
    ("DEMO_TXN_002", "2026-04-10T10:03:45", 1240.0),
    ("DEMO_TXN_003", "2026-04-10T10:04:30", 560.0),
]:
    cur.execute("""
        INSERT OR IGNORE INTO pos_transactions
            (store_id, transaction_id, timestamp, basket_value_inr)
        VALUES (?,?,?,?)
    """, ("STORE_BLR_002", txn_id, ts, amount))

conn.commit()
cur.execute("SELECT COUNT(*) FROM events")
final_count = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM pos_transactions")
pos_count = cur.fetchone()[0]
conn.close()

print(f"Done: {final_count} events, {pos_count} POS transactions seeded.")
EOF

echo "=== Starting API on port ${PORT:-8000} ==="
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1
