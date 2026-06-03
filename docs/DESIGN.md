# DESIGN.md — Store Intelligence System Architecture

## Overview

This system converts raw CCTV footage from a Purplle retail store (Brigade Road, Bangalore) into a live analytics API. The north star metric is **offline store conversion rate**: the fraction of unique visitors who completed a purchase.

The pipeline has four stages:

```
Raw CCTV Clips
    ↓
Detection Layer  (YOLOv8 + ByteTrack + HSV Re-ID)
    ↓
Event Stream     (JSONL → POST /events/ingest)
    ↓
Intelligence API (FastAPI + PostgreSQL)
    ↓
Live Dashboard   (rich terminal + web)
```

---

## Stage 1 — Detection Pipeline (`pipeline/`)

### Person Detection
YOLOv8n runs on every 3rd frame (configurable via `--stride`). Class 0 (person) only. Confidence threshold 0.25 — low-confidence detections are **emitted with their actual confidence**, never suppressed. This is a deliberate spec requirement: partial occlusion must degrade gracefully, not fail silently.

### Tracking
ByteTrack (via the `supervision` library) assigns stable `track_id`s within a single camera clip. ByteTrack was chosen over DeepSORT because it does not require a separate Re-ID model for within-clip tracking — it uses IoU + Kalman filter, which is faster and sufficient for the 30fps retail footage.

### Re-ID (Cross-Camera + Re-Entry)
Within-clip Re-ID uses cosine similarity on HSV colour histograms of the upper-body crop. This is a deliberate trade-off: OSNet/torchreid gives better accuracy but requires a GPU and adds ~2GB to the Docker image. The HSV approach works well for retail footage where uniform colour is a strong discriminator (staff vs. customer, repeat visitors in the same outfit).

The Re-ID threshold is 0.75 cosine similarity. Embeddings are updated with an exponential moving average (α=0.7) to handle lighting changes across frames.

**Re-entry window**: 30 minutes (configurable in `store_layout.json`). A visitor who exited and re-appears within the window gets a `REENTRY` event and reuses their `visitor_id`. Outside the window, they get a fresh `ENTRY`.

### Staff Detection
Upper-body crop is converted to HSV. If ≥35% of pixels fall within any configured uniform colour range (black or purple for Purplle staff), `is_staff=True`. The threshold and colour ranges are in `store_layout.json` — no redeployment needed to tune them.

### Entry/Exit Direction
A horizontal threshold line is defined in `store_layout.json` per entry camera. Direction is determined by whether the track centroid crosses the line top-to-bottom (EXIT) or bottom-to-top (ENTRY). This is robust to the camera angle at Brigade Road where the entry is at the bottom of the frame.

### Zone Assignment
Each zone is a bounding box in pixel coordinates. The track centroid is tested against each zone's bbox. First match wins (zones are ordered in `store_layout.json` to handle overlaps).

### POS Correlation
A visitor is "converted" if they were in a billing zone (`BILLING` or `BILLING_QUEUE`) within 5 minutes before any POS transaction timestamp for the same store. This is a time-window join, not a customer ID match — the spec explicitly states there is no `customer_id` in POS data.

---

## Stage 2 — Event Schema (`pipeline/emit.py`)

The `StoreEvent` Pydantic model is the contract between the pipeline and the API. Key decisions:

- **`event_id` is UUID-v4** — globally unique, generated at emission time. The API uses this for idempotency (`ON CONFLICT DO NOTHING`).
- **`visitor_id` is `VIS_` + 6 hex chars** — short enough to be readable in logs, long enough to avoid collisions within a session.
- **`confidence` is never suppressed** — even 0.05 confidence events are emitted. The API consumer decides what to do with low-confidence data.
- **`session_seq`** is an ordinal counter per visitor session — useful for debugging and for the funnel query to order events.

---

## Stage 3 — Intelligence API (`app/`)

### Framework: FastAPI + SQLAlchemy async
FastAPI was chosen for its native async support, automatic OpenAPI docs, and Pydantic integration. The async SQLAlchemy engine (asyncpg) means DB queries don't block the event loop.

### Database: PostgreSQL
Events are stored in a single `events` table with composite indexes on `(store_id, timestamp)` and `visitor_id`. The `event_id` column has a unique constraint — this is the idempotency guarantee for `POST /events/ingest`.

### Idempotency
`POST /events/ingest` uses `INSERT ... ON CONFLICT (event_id) DO NOTHING`. Calling it twice with the same payload produces the same DB state. The response distinguishes `accepted` vs `duplicate` counts.

### Graceful Degradation
All route handlers catch `OperationalError` and `InterfaceError` from SQLAlchemy and return HTTP 503 with a structured JSON body. No stack traces are ever returned to the client.

### Structured Logging
Every request logs `trace_id`, `store_id`, `endpoint`, `latency_ms`, and `status_code` as a JSON line. The `trace_id` is also injected into the response as `X-Trace-Id` for client-side correlation.

---

## Stage 4 — Live Dashboard (`dashboard/`)

The `rich` library renders a live terminal dashboard that polls the API every 5 seconds. It shows metrics, funnel, heatmap, anomalies, and health in a multi-panel layout. This proves the pipeline and API are genuinely connected — not just batch-processed.

---

## AI-Assisted Decisions

### 1. ByteTrack vs DeepSORT for within-clip tracking
I asked Claude to compare ByteTrack, DeepSORT, and StrongSORT for retail CCTV tracking. The AI recommended ByteTrack for its speed and the fact that it's built into the `supervision` library (no separate install). It also noted that DeepSORT's Re-ID model is overkill for within-clip tracking where IoU is sufficient. **I agreed** — ByteTrack is the right choice here.

### 2. HSV histogram vs OSNet for Re-ID embeddings
The AI initially suggested using OSNet (torchreid) for all Re-ID. I pushed back: OSNet requires a GPU for real-time use and adds significant Docker image size. The AI then suggested HSV histograms as a fallback with a code path to upgrade to OSNet when available. **I adopted this hybrid approach** — it's pragmatic for a take-home challenge where the evaluator may not have a GPU.

### 3. POS correlation via time-window join
The AI suggested using a customer ID for POS correlation. I corrected it: the spec explicitly states there is no `customer_id` in POS data. The AI then proposed the 5-minute billing-zone look-back window, which matches the spec exactly. **I agreed** and implemented it as a SQL JOIN with an interval condition.

---

## Known Limitations

1. **Cross-camera Re-ID accuracy**: HSV histograms are sensitive to lighting changes between cameras. In production, OSNet embeddings would improve accuracy significantly.
2. **Staff detection**: The HSV uniform detection works for solid-colour uniforms but would fail for patterned uniforms. A VLM-based approach (e.g., prompting GPT-4V with "is this person wearing a retail uniform?") would be more robust.
3. **Re-entry window**: The 30-minute window is a heuristic. In a store with a long dwell time, a genuine new visit could be misclassified as a re-entry.
4. **POS correlation**: The 5-minute window assumes customers pay immediately after entering the billing zone. Customers who browse the billing area and then leave to get another item would be missed.
