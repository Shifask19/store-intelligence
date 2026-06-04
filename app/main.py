"""
app/main.py — FastAPI application entrypoint

Wires together all routers, middleware, and startup/shutdown hooks.

Middleware stack (outermost → innermost):
  1. RequestLoggingMiddleware — structured JSON log per request
  2. DB error handler — converts SQLAlchemy errors to 503 responses
  3. Route handlers

Structured log format per request:
  {"trace_id": "...", "store_id": "...", "endpoint": "...",
   "latency_ms": 42, "status_code": 200, "event_count": null}
"""

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError, InterfaceError

from app.database import init_db
from app.ingestion import router as ingest_router
from app.metrics import router as metrics_router
from app.funnel import router as funnel_router
from app.heatmap import router as heatmap_router
from app.anomalies import router as anomalies_router
from app.health import router as health_router


# ---------------------------------------------------------------------------
# Demo data seeding — runs on every startup when DB is empty
# (needed for Render/Railway free tier where storage is ephemeral)
# ---------------------------------------------------------------------------
async def _seed_demo_data_if_empty():
    """Seed events_final.jsonl into DB if empty. Safe to call multiple times."""
    import json
    import os
    from pathlib import Path
    from datetime import datetime, timezone
    from sqlalchemy import text
    from app.database import AsyncSessionLocal

    events_file = Path(__file__).parent.parent / "data" / "events_final.jsonl"
    if not events_file.exists():
        return

    async with AsyncSessionLocal() as session:
        try:
            result = await session.execute(text("SELECT COUNT(*) FROM events"))
            count = result.scalar() or 0
            if count > 0:
                logger.info(f"DB already has {count} events — skipping seed")
                return

            lines = [l.strip() for l in events_file.open() if l.strip()]
            logger.info(f"Seeding {len(lines)} events into empty DB...")
            now = datetime.now(timezone.utc).isoformat()

            insert_sql = text("""
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

            for line in lines:
                try:
                    e = json.loads(line)
                    m = e.get("metadata") or {}
                    await session.execute(insert_sql, {
                        "event_id":    e.get("event_id", ""),
                        "store_id":    e.get("store_id", ""),
                        "camera_id":   e.get("camera_id", ""),
                        "visitor_id":  e.get("visitor_id", ""),
                        "event_type":  e.get("event_type", ""),
                        "timestamp":   e.get("timestamp", ""),
                        "zone_id":     e.get("zone_id"),
                        "dwell_ms":    e.get("dwell_ms", 0),
                        "is_staff":    e.get("is_staff", False),
                        "confidence":  e.get("confidence", 0.5),
                        "queue_depth": m.get("queue_depth"),
                        "sku_zone":    m.get("sku_zone"),
                        "session_seq": m.get("session_seq", 0),
                        "ingested_at": now,
                    })
                except Exception:
                    pass

            # Seed demo POS transactions for non-zero conversion rate
            pos_sql = text("""
                INSERT OR IGNORE INTO pos_transactions
                    (store_id, transaction_id, timestamp, basket_value_inr)
                VALUES (:s, :t, :ts, :a)
            """)
            for txn_id, ts, amount in [
                ("DEMO_TXN_001", "2026-04-10T10:03:00", 850.0),
                ("DEMO_TXN_002", "2026-04-10T10:03:45", 1240.0),
                ("DEMO_TXN_003", "2026-04-10T10:04:30", 560.0),
            ]:
                try:
                    await session.execute(pos_sql, {
                        "s": "STORE_BLR_002", "t": txn_id, "ts": ts, "a": amount
                    })
                except Exception:
                    pass

            await session.commit()
            logger.info(f"Seeded {len(lines)} events + 3 POS transactions successfully")

        except Exception as e:
            logger.warning(f"Seed skipped: {e}")

# ---------------------------------------------------------------------------
# Logging setup — JSON-structured for log aggregators (Loki, CloudWatch, etc.)
# ---------------------------------------------------------------------------
import json as _json

class _StructuredFormatter(logging.Formatter):
    """Emit one JSON line per log record, including any 'extra' fields."""
    def format(self, record: logging.LogRecord) -> str:
        base = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Merge any extra fields injected via logger.info(..., extra={...})
        for key in ("trace_id", "store_id", "endpoint", "method",
                    "latency_ms", "status_code", "event_count"):
            if hasattr(record, key):
                base[key] = getattr(record, key)
        return _json.dumps(base, ensure_ascii=False)

_handler = logging.StreamHandler()
_handler.setFormatter(_StructuredFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger("store_intelligence")


# ---------------------------------------------------------------------------
# Lifespan: DB init on startup
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up — initialising database schema")
    try:
        await init_db()
        logger.info("Database schema ready")
        # Auto-seed demo data if DB is empty (for Render/Railway deployments)
        await _seed_demo_data_if_empty()
    except Exception as e:
        logger.error(f"DB init failed: {e} — continuing (will 503 on DB queries)")
    yield
    logger.info("Shutting down")


# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Store Intelligence API",
    description="Real-time retail analytics from CCTV event streams",
    version="1.0.0",
    lifespan=lifespan,
    # Disable default exception handlers so our middleware controls the format
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS — allow dashboard origin in dev; tighten in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Structured request logging middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    trace_id = str(uuid.uuid4())
    request.state.trace_id = trace_id

    # Extract store_id from path if present (e.g. /stores/STORE_BLR_002/metrics)
    path_parts = request.url.path.split("/")
    store_id = None
    if "stores" in path_parts:
        idx = path_parts.index("stores")
        if idx + 1 < len(path_parts):
            store_id = path_parts[idx + 1]

    start = time.perf_counter()
    try:
        response: Response = await call_next(request)
    except Exception as exc:
        # Catch unhandled exceptions — return 500 with no stack trace
        logger.error(
            f"Unhandled exception trace_id={trace_id}: {exc}",
            exc_info=True,
        )
        response = JSONResponse(
            status_code=500,
            content={"error": "internal_error", "trace_id": trace_id},
        )

    latency_ms = round((time.perf_counter() - start) * 1000, 1)

    # Structured log line
    logger.info(
        "",
        extra={
            "trace_id": trace_id,
            "store_id": store_id,
            "endpoint": request.url.path,
            "method": request.method,
            "latency_ms": latency_ms,
            "status_code": response.status_code,
        },
    )
    # Inject trace_id into response headers for client-side correlation
    response.headers["X-Trace-Id"] = trace_id
    return response


# ---------------------------------------------------------------------------
# Global exception handlers — no raw stack traces in responses
# ---------------------------------------------------------------------------
@app.exception_handler(OperationalError)
@app.exception_handler(InterfaceError)
async def db_error_handler(request: Request, exc: Exception):
    trace_id = getattr(request.state, "trace_id", str(uuid.uuid4()))
    logger.error(f"DB error trace_id={trace_id}: {exc}")
    return JSONResponse(
        status_code=503,
        content={
            "error": "database_unavailable",
            "message": "The database is temporarily unavailable. Please retry.",
            "trace_id": trace_id,
        },
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    trace_id = getattr(request.state, "trace_id", str(uuid.uuid4()))
    logger.error(f"Unhandled error trace_id={trace_id}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "An unexpected error occurred.",
            "trace_id": trace_id,
        },
    )


# ---------------------------------------------------------------------------
# Register routers
# ---------------------------------------------------------------------------
app.include_router(ingest_router, tags=["Ingestion"])
app.include_router(metrics_router, tags=["Analytics"])
app.include_router(funnel_router, tags=["Analytics"])
app.include_router(heatmap_router, tags=["Analytics"])
app.include_router(anomalies_router, tags=["Analytics"])
app.include_router(health_router, tags=["Operations"])


# ---------------------------------------------------------------------------
# Root redirect to docs
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def root():
    return JSONResponse({"message": "Store Intelligence API", "docs": "/docs"})
