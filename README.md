# Store Intelligence System

End-to-end retail analytics pipeline: raw CCTV footage → structured events → REST API → live dashboard.

**North Star Metric**: Offline Store Conversion Rate = unique visitors who purchased ÷ total unique visitors

**Store**: Brigade Road, Bangalore (`STORE_BLR_002`)

---

## 🚀 Live Demo (no setup required)

| URL | Description |
|-----|-------------|
| **https://store-intelligence-m4v1.onrender.com/docs** | Swagger UI — try all endpoints interactively |
| https://store-intelligence-m4v1.onrender.com/stores/STORE_BLR_002/metrics?date=2026-04-10 | Store metrics |
| https://store-intelligence-m4v1.onrender.com/stores/STORE_BLR_002/funnel?date=2026-04-10 | Conversion funnel |
| https://store-intelligence-m4v1.onrender.com/stores/STORE_BLR_002/heatmap?date=2026-04-10 | Zone heatmap |
| https://store-intelligence-m4v1.onrender.com/stores/STORE_BLR_002/anomalies?date=2026-04-10 | Anomalies |
| https://store-intelligence-m4v1.onrender.com/health | Health check |

> **Note:** First request may take ~30 seconds if the free-tier instance is sleeping. Subsequent requests are fast.

---

## Table of Contents

- [System Overview](#system-overview)
- [Quick Start — No Docker](#quick-start--no-docker-fastest)
- [Quick Start — Docker](#quick-start--docker-recommended-for-submission)
- [Running the Detection Pipeline](#running-the-detection-pipeline)
- [Ingest Events into the API](#ingest-events-into-the-api)
- [Live Dashboard](#live-dashboard)
- [API Reference](#api-reference)
- [Running Tests](#running-tests)
- [Project Structure](#project-structure)
- [Environment Variables](#environment-variables)
- [Architecture](#architecture)

---

## System Overview

```
Raw CCTV Clips
    ↓
Detection Layer     YOLOv8n + ByteTrack + HSV Re-ID
    ↓
Event Stream        JSONL → POST /events/ingest
    ↓
Intelligence API    FastAPI + SQLite / PostgreSQL
    ↓
Live Dashboard      rich terminal + web UI (port 8080)
```

The pipeline produces 8 event types: `ENTRY`, `EXIT`, `ZONE_ENTER`, `ZONE_EXIT`,
`ZONE_DWELL`, `BILLING_QUEUE_JOIN`, `BILLING_QUEUE_ABANDON`, `REENTRY`.

---

## Quick Start — No Docker (fastest)

**5 commands to a running system:**

```powershell
# 1. Enter the project directory
cd store-intelligence

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the API (SQLite, no database setup needed)
$env:DATABASE_URL = "sqlite+aiosqlite:///./local.db"
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 4. Ingest pre-generated events (new terminal)
python scripts\ingest_events.py --events data\events_final.jsonl --api http://localhost:8000

# 5. Open the live dashboard (new terminal)
python dashboard\dashboard.py --api http://localhost:8000 --store STORE_BLR_002 --date 2026-04-10
```

- API: http://localhost:8000
- Swagger UI: http://localhost:8000/docs
- Dashboard: running in the terminal

---

## Quick Start — Docker (recommended for submission)

```powershell
# 1. Make sure Docker Desktop is running, then:
cd store-intelligence
docker compose up -d

# 2. Wait ~15 seconds for postgres to be ready, then ingest events
python scripts\ingest_events.py --events data\events_final.jsonl --api http://localhost:8000

# 3. Open the dashboard
python dashboard\dashboard.py --api http://localhost:8000 --store STORE_BLR_002 --date 2026-04-10
```

- API: http://localhost:8000/docs
- Dashboard web UI: http://localhost:8080
- PostgreSQL: localhost:5432

To stop everything:
```powershell
docker compose down
```

---

## Running the Detection Pipeline

> **Skip this if using pre-generated events.** The `data/events_final.jsonl` file already
> contains events processed from all 5 cameras. Use it directly with the ingest command above.

### Prerequisites

```powershell
pip install -r requirements.txt
# YOLOv8 weights (yolov8n.pt) download automatically on first run (~6 MB)
```

### Process a single camera

```powershell
# Windows PowerShell
python -m pipeline.detect `
  --video      "C:\path\to\footage\CCTV Footage\CAM 1.mp4" `
  --layout     data\store_layout.json `
  --camera-id  CAM_ENTRY_01 `
  --output     data\events.jsonl `
  --start-time "2026-04-10T10:00:00Z" `
  --pos-csv    data\pos_transactions.csv `
  --stride     3 `
  --conf       0.25
```

```bash
# Linux / macOS
python -m pipeline.detect \
  --video      "data/footage/CCTV Footage/CAM 1.mp4" \
  --layout     data/store_layout.json \
  --camera-id  CAM_ENTRY_01 \
  --output     data/events.jsonl \
  --start-time "2026-04-10T10:00:00Z" \
  --pos-csv    data/pos_transactions.csv \
  --stride     3 \
  --conf       0.25
```

### Camera ID mapping

| File | Camera ID | Type |
|------|-----------|------|
| CAM 1.mp4 | `CAM_ENTRY_01` | Entry/exit threshold |
| CAM 2.mp4 | `CAM_FLOOR_02` | Main floor zones |
| CAM 3.mp4 | `CAM_FLOOR_03` | Secondary floor zones |
| CAM 4.mp4 | `CAM_BILLING_04` | Billing counter |
| CAM 5.mp4 | `CAM_BILLING_05` | Billing queue area |

### Process all cameras in one command

```bash
bash pipeline/run.sh \
  --footage-dir "data/footage/CCTV Footage" \
  --layout      data/store_layout.json \
  --output      data/events.jsonl \
  --start-time  "2026-04-10T10:00:00Z" \
  --pos-csv     data/pos_transactions.csv
```

> **Important:** Run cameras in order — entry first, then floor, then billing.
> This seeds visitor IDs before cross-camera Re-ID runs.

> **`--pos-csv` flag:** Required for `BILLING_QUEUE_ABANDON` detection. The pipeline
> loads POS transactions at startup and checks each billing-zone exit against the
> transaction log. If no transaction follows within 5 minutes, it emits `BILLING_QUEUE_ABANDON`.

---

## Ingest Events into the API

```powershell
# Ingest pre-generated events (recommended)
python scripts\ingest_events.py `
  --events data\events_final.jsonl `
  --api    http://localhost:8000 `
  --batch  500

# Or ingest freshly detected events
python scripts\ingest_events.py `
  --events data\events.jsonl `
  --api    http://localhost:8000 `
  --batch  500
```

The ingest endpoint is **idempotent** — running this twice produces the same DB state.
Duplicate `event_id`s are silently skipped and reported in the `duplicate` count.

---

## Live Dashboard

### Terminal (rich UI)

```powershell
python dashboard\dashboard.py `
  --api     http://localhost:8000 `
  --store   STORE_BLR_002 `
  --date    2026-04-10 `
  --refresh 5
```

The dashboard refreshes every 5 seconds and shows:
- Real-time metrics: unique visitors, conversion rate, queue depth, abandonment rate
- Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase with drop-off %
- Zone heatmap: normalised 0–100 with avg dwell time
- Active anomalies: severity (INFO / WARN / CRITICAL) + suggested action
- Feed health: per-store lag, STALE_FEED warning if >10 min

### Web UI via Docker

```powershell
docker compose up -d
# Dashboard web interface available at:
# http://localhost:8080
```

---

## API Reference

All endpoints accept `?date=YYYY-MM-DD` (default: today UTC).

### POST /events/ingest

Batch ingest up to 500 events. Idempotent by `event_id`. Partial success on malformed events.

```json
{
  "events": [
    {
      "event_id": "uuid-v4",
      "store_id": "STORE_BLR_002",
      "camera_id": "CAM_ENTRY_01",
      "visitor_id": "VIS_c8a2f1",
      "event_type": "ENTRY",
      "timestamp": "2026-04-10T10:00:00Z",
      "zone_id": null,
      "dwell_ms": 0,
      "is_staff": false,
      "confidence": 0.91,
      "metadata": { "queue_depth": null, "sku_zone": null, "session_seq": 1 }
    }
  ]
}
```

Response: `{"accepted": 1, "duplicate": 0, "rejected": 0, "errors": []}`

---

### GET /stores/{store_id}/metrics

Returns today's store metrics. Excludes `is_staff=true` events.

```json
{
  "store_id": "STORE_BLR_002",
  "date": "2026-04-10",
  "unique_visitors": 9,
  "conversion_rate": 0.3333,
  "avg_dwell_per_zone": [
    { "zone_id": "HAIRCARE", "avg_dwell_ms": 14043.6, "visit_count": 54 }
  ],
  "current_queue_depth": 0,
  "abandonment_rate": 0.0,
  "total_transactions": 26
}
```

---

### GET /stores/{store_id}/funnel

Conversion funnel — session-level, re-entry deduplicated.

```json
{
  "store_id": "STORE_BLR_002",
  "stages": [
    { "stage": "Entry",         "count": 9, "drop_off_pct": 0.0 },
    { "stage": "Zone Visit",    "count": 9, "drop_off_pct": 0.0 },
    { "stage": "Billing Queue", "count": 3, "drop_off_pct": 66.7 },
    { "stage": "Purchase",      "count": 3, "drop_off_pct": 0.0 }
  ],
  "session_window_start": "2026-04-10T10:00:00Z",
  "session_window_end": "2026-04-10T10:02:27Z"
}
```

---

### GET /stores/{store_id}/heatmap

Zone visit frequency + avg dwell, normalised 0–100.

```json
{
  "store_id": "STORE_BLR_002",
  "data_confidence": true,
  "zones": [
    { "zone_id": "SKINCARE", "sku_zone": "MOISTURISER", "visit_frequency": 260,
      "avg_dwell_ms": 1122.6, "normalised_score": 100.0, "data_confidence": true }
  ]
}
```

`data_confidence` is `false` when fewer than 20 sessions exist in the window.

---

### GET /stores/{store_id}/anomalies

Active operational anomalies with severity and suggested action.

```json
{
  "store_id": "STORE_BLR_002",
  "anomalies": [
    {
      "anomaly_type": "BILLING_QUEUE_SPIKE",
      "severity": "WARN",
      "description": "Queue depth 8 is 4.0× the daily average (2.0)",
      "suggested_action": "Open an additional billing counter or redirect to self-checkout.",
      "detected_at": "2026-04-10T10:01:00Z",
      "zone_id": "BILLING",
      "value": 8.0,
      "threshold": 4.0
    }
  ],
  "checked_at": "2026-04-10T10:01:05Z"
}
```

Anomaly types: `BILLING_QUEUE_SPIKE`, `CONVERSION_DROP`, `DEAD_ZONE`
Severity levels: `INFO`, `WARN`, `CRITICAL`

---

### GET /health

Service health — DB status + per-store feed freshness.

```json
{
  "status": "healthy",
  "db_status": "ok",
  "stores": [
    {
      "store_id": "STORE_BLR_002",
      "status": "OK",
      "last_event_at": "2026-04-10T10:02:27Z",
      "lag_minutes": 2.3,
      "warning": null
    }
  ],
  "checked_at": "2026-06-02T06:30:00Z"
}
```

`status` is `STALE_FEED` if no events received in the last 10 minutes.

---

## Running Tests

```powershell
# Run all tests with coverage
pytest tests/ -v --cov=app --cov=pipeline --cov-report=term-missing

# Run individual test files
pytest tests/test_pipeline.py -v    # Pipeline: schema, tracker, Re-ID, abandon
pytest tests/test_metrics.py -v     # API: ingest, metrics, funnel, heatmap
pytest tests/test_anomalies.py -v   # API: anomalies, health endpoint
```

**Results**: 77 tests, 0 failures, ~74% statement coverage.

Edge cases covered: empty store, all-staff clip, zero purchases, re-entry deduplication,
idempotent ingest, partial success on malformed events, STALE_FEED detection,
BILLING_QUEUE_ABANDON with and without POS match, group entry (3 simultaneous tracks).

---

## Project Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py          # YOLOv8n + ByteTrack detection loop, CLI entrypoint
│   ├── tracker.py         # Re-ID, visitor_id assignment, re-entry, abandon detection
│   ├── emit.py            # Pydantic event schema + JSONL writer
│   └── run.sh             # Processes all 5 cameras in correct order
├── app/
│   ├── main.py            # FastAPI app, structured JSON logging middleware
│   ├── models.py          # SQLAlchemy ORM + Pydantic request/response schemas
│   ├── database.py        # Async engine (asyncpg / aiosqlite), health check
│   ├── ingestion.py       # POST /events/ingest — per-event validation, idempotency
│   ├── metrics.py         # GET /stores/{id}/metrics
│   ├── funnel.py          # GET /stores/{id}/funnel
│   ├── heatmap.py         # GET /stores/{id}/heatmap
│   ├── anomalies.py       # GET /stores/{id}/anomalies
│   └── health.py          # GET /health
├── dashboard/
│   └── dashboard.py       # Live terminal dashboard (rich), polls API every 5s
├── tests/
│   ├── conftest.py        # SQLite test DB setup, per-test table cleanup
│   ├── test_pipeline.py   # 40 pipeline tests (schema, tracker, emit, edge cases)
│   ├── test_metrics.py    # 22 API tests (ingest, metrics, funnel, heatmap)
│   └── test_anomalies.py  # 15 tests (anomalies, health, STALE_FEED)
├── docs/
│   ├── DESIGN.md          # Architecture + AI-assisted decisions
│   └── CHOICES.md         # 3 key decisions: model, schema, API architecture
├── data/
│   ├── store_layout.json  # Zone definitions, camera config, staff HSV ranges
│   ├── pos_transactions.csv
│   └── events_final.jsonl # Pre-generated events from all 5 cameras
├── scripts/
│   ├── ingest_events.py   # Feeds JSONL events into the API in batches
│   ├── bulk_ingest.py     # Minimal bulk ingest script
│   ├── check_api.py       # Prints all endpoint responses for quick verification
│   └── init_pos.sql       # Seeds POS transactions on Docker DB init
├── docker-compose.yml     # postgres + api + dashboard (3 services)
├── Dockerfile             # Multi-stage build: builder + runtime
├── Dockerfile.dashboard   # Lightweight dashboard image
├── .coveragerc            # Coverage config (greenlet for async tracing)
├── pytest.ini
└── requirements.txt
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://store_user:store_pass@localhost:5432/store_intelligence` | DB connection. Use `sqlite+aiosqlite:///./local.db` for local dev |
| `DB_NULL_POOL` | `false` | Set `true` in tests to avoid connection pool leaks |
| `LOG_LEVEL` | `info` | Uvicorn log level |
| `API_URL` | `http://localhost:8000` | API base URL for dashboard |
| `STORE_ID` | `STORE_BLR_002` | Store ID for dashboard |
| `REFRESH_SECONDS` | `5` | Dashboard poll interval |
| `DATA_DATE` | *(empty = today)* | Override query date in dashboard (e.g. `2026-04-10`) |

---

## Data Notes

| Item | Detail |
|------|--------|
| Store | Brigade Road, Bangalore — `STORE_BLR_002` |
| Cameras | 5 cameras: 1 entry, 2 floor, 2 billing |
| Footage duration | ~2.3 min per camera @ 1080p 30fps |
| POS transactions | 24 real transactions (16:45–18:00 on 10-Apr-2026) |
| Pre-generated events | `data/events_final.jsonl` — 2308 events from all cameras |
| Entry direction fix | Layout updated to `up_to_down` (camera looks inward from store) |

> **POS correlation note:** The provided footage covers 10:00–10:02 while POS
> transactions are from 16:45–18:00. There is no natural time overlap. The system
> correctly returns `conversion_rate: 0.0` for this scenario. Demo transactions
> overlapping the footage window can be seeded via `scripts/seed_demo_pos.py`
> to verify the conversion logic end-to-end.

---

## Architecture

See [`docs/DESIGN.md`](docs/DESIGN.md) for full architecture documentation including
the AI-Assisted Decisions section.

See [`docs/CHOICES.md`](docs/CHOICES.md) for the three key design decisions:
detection model selection, event schema design, and API architecture — each with
options considered, AI suggestions, and reasoning for the final choice.

---

## Known Limitations

| Limitation | Impact | Mitigation |
|-----------|--------|------------|
| HSV Re-ID vs OSNet | Lower cross-camera Re-ID accuracy | Documented; OSNet upgrade path in `tracker.py` |
| Entry camera angle | Few ENTRY events from short clip | Fallback: count unique visitors from all events |
| POS time gap | Conversion 0% on real footage | Expected — footage is morning, POS is evening |
| 7-day anomaly baseline | `CONVERSION_DROP` needs history | Only fires after multiple days of data |
| Single-process emitter | Not thread-safe | Sufficient for offline batch pipeline |
