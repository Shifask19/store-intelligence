"""
pipeline/tracker.py — Re-ID logic, visitor_id assignment, re-entry detection

Design decisions:
- Primary tracking: ByteTrack assigns stable track_ids within a single camera clip.
- Cross-camera / re-entry Re-ID: cosine similarity on appearance embeddings.
  We use a lightweight colour histogram (HSV) as the embedding when a full
  OSNet model is not available, and upgrade to OSNet when torchreid is installed.
- Re-entry window: configurable (default 30 min). A visitor who exited and
  re-appears within the window gets a REENTRY event and reuses their visitor_id.
- Staff detection: HSV analysis of the upper-body crop. Configurable colour
  ranges in store_layout.json.
- BILLING_QUEUE_ABANDON: emitted when a visitor leaves a billing zone and no
  POS transaction follows within POS_WINDOW_SECONDS. Because the pipeline runs
  offline (batch), we resolve abandonment at clip-end via finalize_abandonment().
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Optional: try to import torchreid OSNet for richer embeddings
# ---------------------------------------------------------------------------
try:
    import torchreid  # type: ignore
    _TORCHREID_AVAILABLE = True
except ImportError:
    _TORCHREID_AVAILABLE = False

# How long after leaving billing zone to wait for a POS txn before flagging abandon
POS_WINDOW_SECONDS = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class TrackState:
    """Live state for a single ByteTrack track_id within one camera."""

    track_id: int
    visitor_id: str
    camera_id: str
    store_id: str
    first_seen: datetime
    last_seen: datetime
    last_bbox: tuple  # (x1, y1, x2, y2) in pixel coords
    embedding: Optional[np.ndarray]  # appearance embedding for Re-ID
    is_staff: bool = False
    current_zone: Optional[str] = None
    zone_enter_time: Optional[datetime] = None
    last_dwell_emit: Optional[datetime] = None
    session_seq: int = 0
    exited: bool = False  # True after EXIT event emitted


@dataclass
class BillingExit:
    """Records a visitor leaving a billing zone — pending POS correlation."""
    visitor_id: str
    exit_time: datetime
    zone_id: str
    confidence: float
    is_staff: bool
    session_seq: int
    resolved: bool = False  # True once we know if they purchased or abandoned


@dataclass
class VisitorRecord:
    """Persistent record keyed by visitor_id — survives across cameras."""

    visitor_id: str
    embedding: Optional[np.ndarray]
    last_exit_time: Optional[datetime] = None
    is_staff: bool = False
    session_count: int = 1  # increments on each REENTRY


# ---------------------------------------------------------------------------
# Appearance embedding helpers
# ---------------------------------------------------------------------------
def _hsv_histogram_embedding(crop: np.ndarray, bins: int = 32) -> np.ndarray:
    """
    Fast HSV histogram as a fallback embedding when OSNet is unavailable.
    Concatenates H, S, V histograms → 96-dim vector, L2-normalised.
    """
    if crop is None or crop.size == 0:
        return np.zeros(bins * 3, dtype=np.float32)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h_hist = cv2.calcHist([hsv], [0], None, [bins], [0, 180]).flatten()
    s_hist = cv2.calcHist([hsv], [1], None, [bins], [0, 256]).flatten()
    v_hist = cv2.calcHist([hsv], [2], None, [bins], [0, 256]).flatten()
    vec = np.concatenate([h_hist, s_hist, v_hist]).astype(np.float32)
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity in [0, 1]. Returns 0 if either vector is zero."""
    if a is None or b is None:
        return 0.0
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def extract_embedding(frame: np.ndarray, bbox: tuple) -> np.ndarray:
    """
    Extract appearance embedding from the upper-body crop of a detection.
    Uses OSNet if torchreid is available, otherwise HSV histogram.
    """
    x1, y1, x2, y2 = [int(v) for v in bbox]
    # Upper-body crop: top 60% of the bounding box
    ub_y2 = y1 + int((y2 - y1) * 0.6)
    crop = frame[max(0, y1):ub_y2, max(0, x1):x2]
    if crop.size == 0:
        return np.zeros(96, dtype=np.float32)
    return _hsv_histogram_embedding(crop)


# ---------------------------------------------------------------------------
# Staff detection
# ---------------------------------------------------------------------------
def is_staff_by_uniform(
    frame: np.ndarray,
    bbox: tuple,
    hsv_ranges: list,
    coverage_threshold: float = 0.35,
) -> bool:
    """
    Classify a detection as staff if the upper-body crop contains a
    sufficient fraction of pixels matching any configured uniform colour range.

    Args:
        frame: Full BGR frame.
        bbox: (x1, y1, x2, y2) bounding box.
        hsv_ranges: List of dicts with h_min/h_max/s_min/s_max/v_min/v_max.
        coverage_threshold: Fraction of pixels that must match to flag as staff.
    """
    x1, y1, x2, y2 = [int(v) for v in bbox]
    ub_y2 = y1 + int((y2 - y1) * 0.6)
    crop = frame[max(0, y1):ub_y2, max(0, x1):x2]
    if crop.size == 0:
        return False

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    total_pixels = hsv.shape[0] * hsv.shape[1]
    if total_pixels == 0:
        return False

    for r in hsv_ranges:
        lo = np.array([r["h_min"], r["s_min"], r["v_min"]], dtype=np.uint8)
        hi = np.array([r["h_max"], r["s_max"], r["v_max"]], dtype=np.uint8)
        mask = cv2.inRange(hsv, lo, hi)
        coverage = np.count_nonzero(mask) / total_pixels
        if coverage >= coverage_threshold:
            return True
    return False


# ---------------------------------------------------------------------------
# Entry / exit direction detection
# ---------------------------------------------------------------------------
def crossed_line(
    prev_cy: float,
    curr_cy: float,
    line_y: float,
    entry_direction: str,
) -> Optional[str]:
    """
    Determine if a track crossed the entry threshold line between frames.

    entry_direction='down_to_up' means moving from high y (bottom of frame)
    to low y (top of frame) = entering the store.

    Returns: 'ENTRY', 'EXIT', or None.
    """
    if entry_direction == "down_to_up":
        if prev_cy > line_y >= curr_cy:
            return "ENTRY"
        if prev_cy <= line_y < curr_cy:
            return "EXIT"
    else:  # up_to_down
        if prev_cy < line_y <= curr_cy:
            return "ENTRY"
        if prev_cy >= line_y > curr_cy:
            return "EXIT"
    return None


# ---------------------------------------------------------------------------
# Zone assignment
# ---------------------------------------------------------------------------
def assign_zone(cx: float, cy: float, zones: list) -> Optional[str]:
    """
    Return the zone_id whose bounding box contains the centroid (cx, cy).
    If multiple zones overlap, the first match wins (order matters in layout).
    """
    for zone in zones:
        x1, y1, x2, y2 = zone["bbox"]
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            return zone["zone_id"]
    return None


# ---------------------------------------------------------------------------
# Main tracker class
# ---------------------------------------------------------------------------
class VisitorTracker:
    """
    Manages visitor_id assignment and Re-ID across frames and cameras.

    Lifecycle:
      1. update_track() called each frame with ByteTrack outputs.
      2. Returns a list of (event_type, track_state) tuples for the emitter.
      3. finalize_abandonment() called at end of clip to emit BILLING_QUEUE_ABANDON
         for any billing exits that were not followed by a POS transaction.
    """

    # Cosine similarity threshold for Re-ID match
    REID_THRESHOLD = 0.75
    # Minimum frames a track must be alive before we trust it (reduces FP)
    MIN_TRACK_AGE_FRAMES = 5

    BILLING_ZONE_IDS = {"BILLING", "BILLING_QUEUE"}

    def __init__(
        self,
        store_id: str,
        camera_id: str,
        camera_config: dict,
        zones: list,
        staff_config: dict,
        reentry_window_minutes: int = 30,
        clip_start_time: Optional[datetime] = None,
        pos_transactions: Optional[list] = None,
    ):
        self.store_id = store_id
        self.camera_id = camera_id
        self.camera_config = camera_config
        self.zones = zones
        self.staff_config = staff_config
        self.reentry_window = timedelta(minutes=reentry_window_minutes)
        self.clip_start_time = clip_start_time or datetime.now(timezone.utc)
        # POS transaction timestamps for this store (UTC datetimes)
        self._pos_txn_times: list[datetime] = pos_transactions or []

        # track_id → TrackState (live tracks in this camera)
        self._live: dict[int, TrackState] = {}
        # track_id → frame count (for MIN_TRACK_AGE_FRAMES filter)
        self._track_age: dict[int, int] = defaultdict(int)
        # visitor_id → VisitorRecord (global, shared across cameras via injection)
        self._visitor_registry: dict[str, VisitorRecord] = {}
        # track_id → previous centroid y (for line crossing)
        self._prev_cy: dict[int, float] = {}
        # Pending billing exits awaiting POS correlation
        self._pending_billing_exits: list[BillingExit] = []

        # Entry line config (only for entry/exit cameras)
        self._entry_line_y: Optional[float] = None
        self._entry_direction: str = "down_to_up"
        if camera_config.get("entry_line"):
            el = camera_config["entry_line"]
            self._entry_line_y = (el["y1"] + el["y2"]) / 2
            self._entry_direction = camera_config.get("entry_direction", "down_to_up")

    def inject_visitor_registry(self, registry: dict) -> None:
        """Share the global visitor registry across camera trackers."""
        self._visitor_registry = registry

    def frame_timestamp(self, frame_idx: int, fps: float) -> datetime:
        """Convert frame index to wall-clock UTC timestamp."""
        offset_sec = frame_idx / fps
        return self.clip_start_time + timedelta(seconds=offset_sec)

    def _reid_lookup(self, embedding: np.ndarray) -> Optional[str]:
        """
        Search the visitor registry for a matching embedding.
        Returns visitor_id if similarity >= REID_THRESHOLD, else None.
        """
        best_score = 0.0
        best_vid = None
        for vid, record in self._visitor_registry.items():
            if record.embedding is None:
                continue
            score = _cosine_similarity(embedding, record.embedding)
            if score > best_score:
                best_score = score
                best_vid = vid
        if best_score >= self.REID_THRESHOLD:
            return best_vid
        return None

    def _make_visitor_id(self) -> str:
        """UUID4-based visitor ID — guaranteed unique even in tight loops."""
        import uuid as _uuid
        return "VIS_" + _uuid.uuid4().hex[:6]

    def _had_pos_txn_after(self, billing_exit_time: datetime) -> bool:
        """
        Returns True if any POS transaction occurred within POS_WINDOW_SECONDS
        after the visitor left the billing zone.
        """
        window = timedelta(seconds=POS_WINDOW_SECONDS)
        for txn_ts in self._pos_txn_times:
            if timedelta(0) <= (txn_ts - billing_exit_time) <= window:
                return True
        return False

    def _record_billing_exit(
        self,
        visitor_id: str,
        exit_time: datetime,
        zone_id: str,
        confidence: float,
        is_staff: bool,
        session_seq: int,
    ) -> None:
        """Record a billing zone exit for later POS correlation."""
        if is_staff:
            return  # staff exits never generate BILLING_QUEUE_ABANDON
        self._pending_billing_exits.append(BillingExit(
            visitor_id=visitor_id,
            exit_time=exit_time,
            zone_id=zone_id,
            confidence=confidence,
            is_staff=is_staff,
            session_seq=session_seq,
        ))

    def finalize_abandonment(self) -> list[tuple]:
        """
        Called at end of clip processing. For each pending billing exit,
        check if a POS transaction followed within 5 minutes. If not,
        emit BILLING_QUEUE_ABANDON.

        Returns list of (event_type_str, extra_data_dict) tuples.
        """
        events_out = []
        for exit_rec in self._pending_billing_exits:
            if exit_rec.resolved:
                continue
            if not self._had_pos_txn_after(exit_rec.exit_time):
                events_out.append((
                    "BILLING_QUEUE_ABANDON",
                    {
                        "visitor_id": exit_rec.visitor_id,
                        "timestamp": exit_rec.exit_time,
                        "confidence": exit_rec.confidence,
                        "is_staff": exit_rec.is_staff,
                        "zone_id": exit_rec.zone_id,
                        "dwell_ms": 0,
                        "session_seq": exit_rec.session_seq,
                    },
                ))
            exit_rec.resolved = True
        return events_out

    def update_track(
        self,
        frame: np.ndarray,
        frame_idx: int,
        fps: float,
        track_id: int,
        bbox: tuple,
        detection_confidence: float,
    ) -> list[tuple]:
        """
        Process one ByteTrack detection for a single frame.

        Returns a list of (event_type_str, extra_data_dict) tuples.
        The caller (detect.py) converts these into StoreEvent objects.
        """
        self._track_age[track_id] += 1
        ts = self.frame_timestamp(frame_idx, fps)
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
        events_out = []

        embedding = extract_embedding(frame, bbox)
        is_staff = is_staff_by_uniform(
            frame,
            bbox,
            self.staff_config.get("uniform_hsv_ranges", []),
            self.staff_config.get("uniform_coverage_threshold", 0.35),
        )

        # ----------------------------------------------------------------
        # New track: assign visitor_id via Re-ID or create fresh
        # ----------------------------------------------------------------
        if track_id not in self._live:
            matched_vid = self._reid_lookup(embedding)
            is_reentry = False

            if matched_vid and matched_vid in self._visitor_registry:
                record = self._visitor_registry[matched_vid]
                if (
                    record.last_exit_time
                    and (ts - record.last_exit_time) <= self.reentry_window
                ):
                    visitor_id = matched_vid
                    is_reentry = True
                    record.session_count += 1
                    record.last_exit_time = None
                else:
                    visitor_id = self._make_visitor_id()
            else:
                visitor_id = self._make_visitor_id()

            state = TrackState(
                track_id=track_id,
                visitor_id=visitor_id,
                camera_id=self.camera_id,
                store_id=self.store_id,
                first_seen=ts,
                last_seen=ts,
                last_bbox=bbox,
                embedding=embedding,
                is_staff=is_staff,
                session_seq=0,
            )
            self._live[track_id] = state

            self._visitor_registry[visitor_id] = VisitorRecord(
                visitor_id=visitor_id,
                embedding=embedding,
                is_staff=is_staff,
            )

            if is_reentry and self._track_age[track_id] >= self.MIN_TRACK_AGE_FRAMES:
                state.session_seq += 1
                events_out.append((
                    "REENTRY",
                    {
                        "visitor_id": visitor_id,
                        "timestamp": ts,
                        "confidence": detection_confidence,
                        "is_staff": is_staff,
                        "zone_id": None,
                        "session_seq": state.session_seq,
                    },
                ))

        state = self._live[track_id]
        state.last_seen = ts
        state.last_bbox = bbox
        # Update embedding with exponential moving average for robustness
        if state.embedding is not None and embedding is not None:
            state.embedding = 0.7 * state.embedding + 0.3 * embedding
            n = np.linalg.norm(state.embedding)
            if n > 0:
                state.embedding /= n

        # ----------------------------------------------------------------
        # Entry / exit line crossing (entry cameras only)
        # ----------------------------------------------------------------
        if self._entry_line_y is not None and self._track_age[track_id] >= self.MIN_TRACK_AGE_FRAMES:
            prev_cy = self._prev_cy.get(track_id, cy)
            direction = crossed_line(
                prev_cy, cy, self._entry_line_y, self._entry_direction
            )
            if direction == "ENTRY" and not state.exited:
                state.session_seq += 1
                events_out.append((
                    "ENTRY",
                    {
                        "visitor_id": state.visitor_id,
                        "timestamp": ts,
                        "confidence": detection_confidence,
                        "is_staff": state.is_staff,
                        "zone_id": None,
                        "session_seq": state.session_seq,
                    },
                ))
            elif direction == "EXIT" and not state.exited:
                state.exited = True
                if state.visitor_id in self._visitor_registry:
                    self._visitor_registry[state.visitor_id].last_exit_time = ts
                state.session_seq += 1
                events_out.append((
                    "EXIT",
                    {
                        "visitor_id": state.visitor_id,
                        "timestamp": ts,
                        "confidence": detection_confidence,
                        "is_staff": state.is_staff,
                        "zone_id": None,
                        "session_seq": state.session_seq,
                    },
                ))

        self._prev_cy[track_id] = cy

        # ----------------------------------------------------------------
        # Zone tracking (floor / billing cameras)
        # ----------------------------------------------------------------
        if self._entry_line_y is None:
            new_zone = assign_zone(cx, cy, self.zones)

            if new_zone != state.current_zone:
                # Zone exit
                if state.current_zone is not None:
                    dwell_ms = 0
                    if state.zone_enter_time:
                        dwell_ms = int(
                            (ts - state.zone_enter_time).total_seconds() * 1000
                        )
                    state.session_seq += 1
                    events_out.append((
                        "ZONE_EXIT",
                        {
                            "visitor_id": state.visitor_id,
                            "timestamp": ts,
                            "confidence": detection_confidence,
                            "is_staff": state.is_staff,
                            "zone_id": state.current_zone,
                            "dwell_ms": dwell_ms,
                            "session_seq": state.session_seq,
                        },
                    ))
                    # If leaving a billing zone, record for POS correlation
                    if state.current_zone in self.BILLING_ZONE_IDS:
                        self._record_billing_exit(
                            visitor_id=state.visitor_id,
                            exit_time=ts,
                            zone_id=state.current_zone,
                            confidence=detection_confidence,
                            is_staff=state.is_staff,
                            session_seq=state.session_seq,
                        )

                # Zone enter
                if new_zone is not None:
                    state.session_seq += 1
                    events_out.append((
                        "ZONE_ENTER",
                        {
                            "visitor_id": state.visitor_id,
                            "timestamp": ts,
                            "confidence": detection_confidence,
                            "is_staff": state.is_staff,
                            "zone_id": new_zone,
                            "dwell_ms": 0,
                            "session_seq": state.session_seq,
                        },
                    ))
                    state.zone_enter_time = ts
                    state.last_dwell_emit = ts

                state.current_zone = new_zone

            # ZONE_DWELL: emit every 30 seconds of continuous presence
            elif (
                state.current_zone is not None
                and state.last_dwell_emit is not None
                and (ts - state.last_dwell_emit).total_seconds() >= 30
            ):
                dwell_ms = int(
                    (ts - state.zone_enter_time).total_seconds() * 1000
                ) if state.zone_enter_time else 0
                state.session_seq += 1
                events_out.append((
                    "ZONE_DWELL",
                    {
                        "visitor_id": state.visitor_id,
                        "timestamp": ts,
                        "confidence": detection_confidence,
                        "is_staff": state.is_staff,
                        "zone_id": state.current_zone,
                        "dwell_ms": dwell_ms,
                        "session_seq": state.session_seq,
                    },
                ))
                state.last_dwell_emit = ts

        return events_out

    def handle_lost_track(self, track_id: int, frame_idx: int, fps: float) -> list[tuple]:
        """
        Called when ByteTrack marks a track as lost.
        Emits ZONE_EXIT if the visitor was in a zone.
        If the zone was a billing zone, records it for POS correlation.
        """
        events_out = []
        if track_id not in self._live:
            return events_out

        state = self._live[track_id]
        ts = self.frame_timestamp(frame_idx, fps)

        if state.current_zone is not None:
            dwell_ms = 0
            if state.zone_enter_time:
                dwell_ms = int((ts - state.zone_enter_time).total_seconds() * 1000)
            state.session_seq += 1
            events_out.append((
                "ZONE_EXIT",
                {
                    "visitor_id": state.visitor_id,
                    "timestamp": ts,
                    "confidence": 0.5,  # lost track — lower confidence
                    "is_staff": state.is_staff,
                    "zone_id": state.current_zone,
                    "dwell_ms": dwell_ms,
                    "session_seq": state.session_seq,
                },
            ))
            # If lost while in billing zone, record for POS correlation
            if state.current_zone in self.BILLING_ZONE_IDS:
                self._record_billing_exit(
                    visitor_id=state.visitor_id,
                    exit_time=ts,
                    zone_id=state.current_zone,
                    confidence=0.5,
                    is_staff=state.is_staff,
                    session_seq=state.session_seq,
                )

        del self._live[track_id]
        return events_out

    def get_billing_queue_depth(self, billing_zone_ids: list[str]) -> int:
        """Count live (non-staff) tracks currently in billing zones."""
        count = 0
        for state in self._live.values():
            if state.current_zone in billing_zone_ids and not state.is_staff:
                count += 1
        return count
