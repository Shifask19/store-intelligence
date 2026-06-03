# CHOICES.md — Three Key Design Decisions

---

## Decision 1: Detection Model — YOLOv8n + ByteTrack

### Options Considered

| Option | Pros | Cons |
|--------|------|------|
| YOLOv8n (chosen) | Fast, runs on CPU, built-in ByteTrack, 80-class COCO | Lower accuracy than larger models |
| YOLOv8m | Better accuracy, still reasonable speed | Needs GPU for real-time |
| RT-DETR | State-of-the-art accuracy | Transformer-based, slow on CPU |
| MediaPipe Pose | Lightweight, good for single-person | Struggles with crowds |

### What AI Suggested
Claude suggested starting with YOLOv8n for development and switching to YOLOv8m for the final submission. It noted that ByteTrack is built into the `ultralytics` package, eliminating a separate dependency.

### What I Chose and Why
**YOLOv8n with ByteTrack**, keeping the model swappable via `--model` CLI flag.

The Brigade Road footage is 1080p at 30fps. At stride=3 (10 detections/sec), YOLOv8n processes a frame in ~15ms on CPU — fast enough for near-real-time processing. The accuracy trade-off is acceptable because:

1. The spec evaluates entry/exit count accuracy, not bounding box precision. ByteTrack's temporal smoothing compensates for per-frame detection noise.
2. The footage has full-face blur applied, which actually helps YOLOv8 — it focuses on body shape rather than face features, which is what we want.
3. The `--model` flag means an evaluator with a GPU can swap to `yolov8m.pt` with one argument change.

**On partial occlusion**: YOLOv8 handles partial occlusion better than older models because it was trained on COCO which includes occluded persons. The spec says "degrade gracefully, not fail silently" — we emit with actual confidence rather than suppressing low-confidence detections.

---

## Decision 2: Event Schema Design

### Options Considered

**Option A: Flat schema** — all fields at the top level, no `metadata` nesting.
```json
{"event_id": "...", "queue_depth": null, "sku_zone": "MOISTURISER", "session_seq": 5}
```

**Option B: Nested metadata** (chosen) — core fields flat, optional/event-specific fields in `metadata`.
```json
{"event_id": "...", "metadata": {"queue_depth": null, "sku_zone": "MOISTURISER", "session_seq": 5}}
```

**Option C: Typed event union** — separate Pydantic models per event type (EntryEvent, ZoneDwellEvent, etc.)

### What AI Suggested
The AI initially suggested Option C (typed union) for maximum type safety. It argued that `BILLING_QUEUE_JOIN` and `ZONE_DWELL` have different required fields and a union would enforce this at the schema level.

### What I Chose and Why
**Option B (nested metadata)**, matching the spec's example schema exactly.

I overrode the AI's suggestion for three reasons:

1. **Spec compliance**: The spec provides a single event schema with a `metadata` object. Deviating from this would fail the automated schema compliance tests.
2. **API simplicity**: A single `POST /events/ingest` endpoint that accepts one schema is simpler to implement and test than a union type with discriminated variants.
3. **Forward compatibility**: New event types can add fields to `metadata` without breaking existing consumers. A typed union requires updating the discriminator on every new event type.

The trade-off is that `queue_depth` is nullable on all events, not just `BILLING_QUEUE_JOIN`. This is acceptable — the pipeline only populates it for the relevant event types, and the API queries filter by `event_type` before reading `queue_depth`.

**`visitor_id` format**: `VIS_` + 6 hex chars from UUID4. The AI suggested using the full UUID as `visitor_id`. I shortened it to 6 hex chars because:
- It's more readable in logs and dashboards
- 6 hex chars = 16M possible values, sufficient for a single store session
- The full UUID is still used for `event_id` (global uniqueness)

---

## Decision 3: API Architecture — Async FastAPI + PostgreSQL (no Redis)

### Options Considered

| Option | Pros | Cons |
|--------|------|------|
| FastAPI + PostgreSQL (chosen) | Simple, production-proven, good async support | No pub/sub for real-time push |
| FastAPI + PostgreSQL + Redis | Real-time pub/sub, caching | More moving parts, harder to operate |
| FastAPI + SQLite | Zero-dependency, easy to run | Not production-grade, no concurrent writes |
| Django + PostgreSQL | Batteries included | Sync-first, heavier, worse for streaming |

### What AI Suggested
The AI strongly recommended adding Redis for two purposes:
1. Caching metrics queries (avoid recomputing on every request)
2. Pub/sub for the live dashboard (push events instead of polling)

### What I Chose and Why
**FastAPI + PostgreSQL only**, with Redis in `docker-compose.yml` as an optional service but not wired into the API.

I disagreed with the AI on caching for this use case:

1. **The spec says "real-time — not cached from yesterday"** for `/metrics`. A Redis cache with a TTL would violate this requirement if the TTL is too long. Without a TTL, the cache provides no benefit.
2. **Query performance**: The metrics queries are simple aggregations on indexed columns. At the scale of a single store (thousands of events/day), PostgreSQL handles these in <10ms without caching.
3. **Operational complexity**: Redis adds another service to monitor, another failure mode, and another thing to explain in the README. For a take-home challenge, simplicity is a virtue.

**On the dashboard**: The dashboard polls the API every 5 seconds rather than using WebSocket push. This is simpler and sufficient for a retail analytics use case where metrics don't change faster than once per second. The spec says "terminal output acceptable" — polling is fine.

**What would make me add Redis**: At 40 live stores sending events in real time (the scale mentioned in the follow-up questions), the `/metrics` endpoint would be called by 40 dashboards simultaneously. At that point, a 10-second Redis cache per store would reduce DB load by ~99% with negligible staleness impact. I'd add it then.

---

## Summary

| Decision | AI Suggested | I Chose | Reason for Override |
|----------|-------------|---------|---------------------|
| Detection model | YOLOv8n → YOLOv8m for final | YOLOv8n, swappable | CPU-first, evaluator may not have GPU |
| Re-ID embeddings | OSNet (torchreid) | HSV histogram + OSNet fallback | GPU dependency, Docker image size |
| Event schema | Typed union per event type | Single schema with metadata nesting | Spec compliance, forward compatibility |
| visitor_id format | Full UUID | VIS_ + 6 hex | Readability, sufficient uniqueness |
| API caching | Redis for metrics | No cache | Spec says real-time, queries are fast |
| Dashboard | WebSocket push | Polling every 5s | Simpler, sufficient for retail cadence |
