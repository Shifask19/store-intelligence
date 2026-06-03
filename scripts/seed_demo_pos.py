"""
scripts/seed_demo_pos.py — Seed demo POS transactions that overlap with footage

The provided footage covers 10:00–10:02 on 2026-04-10.
The real POS transactions are from 16:45–18:00 — no natural overlap.

This script seeds 3 demo transactions at 10:03–10:05 so the conversion
rate endpoint returns a non-zero value for demonstration purposes.

Usage:
    python scripts/seed_demo_pos.py          # uses local.db (default)
    python scripts/seed_demo_pos.py --db postgresql+asyncpg://...
"""

import argparse
import os

from sqlalchemy import create_engine, text

DEFAULT_DB = os.getenv("DATABASE_URL", "sqlite:///./local.db").replace(
    "sqlite+aiosqlite", "sqlite"
).replace("postgresql+asyncpg", "postgresql")


def seed(db_url: str):
    engine = create_engine(db_url)
    demo_txns = [
        ("STORE_BLR_002", "DEMO_TXN_001", "2026-04-10T10:03:00", 850.0),
        ("STORE_BLR_002", "DEMO_TXN_002", "2026-04-10T10:03:45", 1240.0),
        ("STORE_BLR_002", "DEMO_TXN_003", "2026-04-10T10:04:30", 560.0),
    ]
    with engine.begin() as conn:
        for store_id, txn_id, ts, amount in demo_txns:
            try:
                conn.execute(text("""
                    INSERT OR IGNORE INTO pos_transactions
                        (store_id, transaction_id, timestamp, basket_value_inr)
                    VALUES (:s, :t, :ts, :a)
                """), {"s": store_id, "t": txn_id, "ts": ts, "a": amount})
                print(f"  Seeded {txn_id} @ {ts}")
            except Exception as e:
                print(f"  Skipped {txn_id}: {e}")
    print("Done. Re-query /metrics?date=2026-04-10 — conversion_rate should now be >0.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLAlchemy DB URL (sync)")
    args = parser.parse_args()
    seed(args.db)
