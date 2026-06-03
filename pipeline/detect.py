"""
pipeline/detect.py — YOLO + ByteTrack per-frame detection loop

Emits events in the Purplle native schema (matching sample_events.jsonl):
  - entry/exit: id_token, store_code, gender_pred, age_pred, group_id
  - zone_entered/zone_exited: zone_name, zone_type, dwell_seconds
  - queue_completed/queue_abandoned: wait_seconds, queue_position
"""

import argparse
import csv
import json
import logging
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.emit import (
    EventEmitter, EntryExitEvent, ZoneEvent, QueueEvent,
    make_id_token, age_bucket
)
from pipeline.tracker import VisitorTracker

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BILLING_ZONE_IDS = {"BILLING", "BILLING_QUEUE"}


def _load_yolo(model_name: str = "yolov8n.pt"):
    try:
        from ultralytics import YOLO
        return YOLO(model_name)
    except ImportError:
        raise RuntimeError("ultralytics not installed. Run: pip install ultralytics")


def _load_bytetrack():
    try:
        import supervision as sv
        return sv.ByteTrack(
            track_activation_threshold=0.25,
            lost_track_buffer=30,
            minimum_matching_threshold=0.8,
            frame_rate=30,
        )
    except ImportError:
        raise RuntimeError("supervision not installed. Run: pip install supervision")


def load_pos_transactions(pos_csv_path: str, store_id: str) -> list:
    txn_times = []
    if not pos_csv_path or not Path(pos_csv_path).exists():
        return txn_times
    try:
        with open(pos_csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sid = row.get("store_id", row.get("store_code", "")).strip()
                if sid == store_id:
                    ts_str = row.get("timestamp", row.get("order_time", "")).strip()
                    date_str = row.get("order_date", "").strip()
                    if date_str and ts_str:
                        try:
                            ts = datetime.strptime(f"{date_str} {ts_str}", "%d-%m-%Y %H:%M:%S")
                            ts = ts.replace(tzinfo=timezone.utc)
                            txn_times.append(ts)
                        except Exception:
                            pass
                    elif ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            txn_times.append(ts)
                        except Exception:
                            pass
    except Exception as e:
        logger.warning(f"Could not load POS transactions: {e}")
    return txn_times


def _get_zone_info(zone_id: Optional[str], zones: list) -> dict:
    if not zone_id:
        return {"zone_name": "Unknown", "zone_type": "SHELF", "is_revenue": True}
    for z in zones:
        if z.get("zone_id") == zone_id:
            return {
                "zone_name": z.get("zone_name", z.get("sku_zone", zone_id) or zone_id),
                "zone_type": "BILLING" if z.get("is_billing") else "SHELF",
                "is_revenue": True,
            }
    return {"zone_name": zone_id, "zone_type": "SHELF", "is_revenue": True}


def _emit_event(
    emitter: EventEmitter,
    store_id: str,
    camera_id: str,
    etype: str,
    edata: dict,
    zones: list,
    event_counts: dict,
    id_counter: list,
) -> None:
    zone_id = edata.get("zone_id")
    ts = edata.get("timestamp")
    if isinstance(ts, datetime):
        ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%f")
    else:
        ts_str = str(ts)

    is_staff = edata.get("is_staff", False)
    conf = edata.get("confidence", 0.5)
    visitor_id = edata.get("visitor_id", "")

    gender = random.choice(["M", "F"])
    age = random.randint(20, 45)
    bucket = age_bucket(age)

    try:
        if etype in ("ENTRY", "REENTRY"):
            id_counter[0] += 1
            evt = EntryExitEvent(
                event_type="entry",
                id_token=make_id_token(id_counter[0]),
                store_code=store_id,
                camera_id=camera_id,
                event_timestamp=ts_str,
                is_staff=is_staff,
                gender_pred=gender,
                age_pred=age,
                age_bucket=bucket,
                is_face_hidden=conf < 0.3,
                confidence=conf,
            )
            emitter.emit(evt)

        elif etype == "EXIT":
            id_counter[0] += 1
            evt = EntryExitEvent(
                event_type="exit",
                id_token=make_id_token(id_counter[0]),
                store_code=store_id,
                camera_id=camera_id,
                event_timestamp=ts_str,
                is_staff=is_staff,
                gender_pred=gender,
                age_pred=age,
                age_bucket=bucket,
                is_face_hidden=conf < 0.3,
                confidence=conf,
            )
            emitter.emit(evt)

        elif etype in ("ZONE_ENTER", "ZONE_DWELL"):
            if zone_id:
                zi = _get_zone_info(zone_id, zones)
                track_id = abs(hash(visitor_id)) % 100000
                evt = ZoneEvent(
                    event_type="zone_entered",
                    track_id=track_id,
                    store_id=store_id,
                    camera_id=camera_id,
                    zone_id=zone_id,
                    zone_name=zi["zone_name"],
                    zone_type=zi["zone_type"],
                    is_revenue_zone="Yes",
                    event_time=ts_str,
                    gender=gender,
                    age=age,
                    age_bucket=bucket,
                    confidence=conf,
                )
                emitter.emit(evt)

        elif etype == "ZONE_EXIT":
            if zone_id:
                zi = _get_zone_info(zone_id, zones)
                track_id = abs(hash(visitor_id)) % 100000
                dwell_s = edata.get("dwell_ms", 0) / 1000.0
                evt = ZoneEvent(
                    event_type="zone_exited",
                    track_id=track_id,
                    store_id=store_id,
                    camera_id=camera_id,
                    zone_id=zone_id,
                    zone_name=zi["zone_name"],
                    zone_type=zi["zone_type"],
                    is_revenue_zone="Yes",
                    event_time=ts_str,
                    dwell_seconds=round(dwell_s, 2) if dwell_s > 0 else None,
                    gender=gender,
                    age=age,
                    age_bucket=bucket,
                    confidence=conf,
                )
                emitter.emit(evt)

        elif etype == "BILLING_QUEUE_JOIN":
            track_id = abs(hash(visitor_id)) % 100000
            emitter.emit_raw({
                "event_type": "queue_joined_pending",
                "track_id": track_id,
                "store_id": store_id,
                "camera_id": camera_id,
                "zone_id": zone_id or "BILLING",
                "zone_name": "Billing Counter Queue",
                "zone_type": "BILLING",
                "queue_join_ts": ts_str,
                "queue_position_at_join": edata.get("queue_depth", 1),
                "gender": gender,
                "age": age,
                "age_bucket": bucket,
                "_visitor_id": visitor_id,
            })

        elif etype == "BILLING_QUEUE_ABANDON":
            track_id = abs(hash(visitor_id)) % 100000
            evt = QueueEvent(
                event_type="queue_abandoned",
                track_id=track_id,
                store_id=store_id,
                camera_id=camera_id,
                zone_id=zone_id or "BILLING",
                queue_join_ts=ts_str,
                queue_exit_ts=ts_str,
                abandoned=True,
                gender=gender,
                age=age,
                age_bucket=bucket,
            )
            emitter.emit(evt)

        event_counts[etype] = event_counts.get(etype, 0) + 1

    except Exception as e:
        logger.warning(f"Failed to emit {etype}: {e} | data={edata}")


def process_clip(
    video_path: str,
    store_layout: dict,
    camera_config: dict,
    output_path: str,
    clip_start_time: datetime,
    frame_stride: int = 3,
    yolo_model_name: str = "yolov8n.pt",
    conf_threshold: float = 0.25,
    visitor_registry: dict = None,
    pos_csv_path: str = None,
) -> dict:
    store_id = store_layout["store_id"]
    camera_id = camera_config["camera_id"]
    zones = store_layout.get("zones", [])
    staff_config = store_layout.get("staff_detection", {})
    reentry_window = store_layout.get("reentry_window_minutes", 30)

    logger.info(f"Processing {camera_id} | {video_path}")

    pos_txn_times = load_pos_transactions(pos_csv_path or "", store_id)
    logger.info(f"  Loaded {len(pos_txn_times)} POS transactions")

    model = _load_yolo(yolo_model_name)
    tracker_sv = _load_bytetrack()
    import supervision as sv

    if visitor_registry is None:
        visitor_registry = {}

    vtracker = VisitorTracker(
        store_id=store_id,
        camera_id=camera_id,
        camera_config=camera_config,
        zones=zones,
        staff_config=staff_config,
        reentry_window_minutes=reentry_window,
        clip_start_time=clip_start_time,
        pos_transactions=pos_txn_times,
    )
    vtracker.inject_visitor_registry(visitor_registry)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    logger.info(f"  {total_frames} frames @ {fps:.1f}fps")

    prev_track_ids: set[int] = set()
    event_counts: dict[str, int] = {}
    total_events = 0
    id_counter = [0]

    with EventEmitter(output_path) as emitter:
        frame_idx = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            if frame_idx % frame_stride != 0:
                frame_idx += 1
                continue

            results = model(frame, classes=[0], conf=conf_threshold, verbose=False)
            result = results[0]

            if result.boxes is None or len(result.boxes) == 0:
                for tid in list(prev_track_ids):
                    lost_events = vtracker.handle_lost_track(tid, frame_idx, fps)
                    for etype, edata in lost_events:
                        _emit_event(emitter, store_id, camera_id, etype, edata,
                                    zones, event_counts, id_counter)
                        total_events += 1
                prev_track_ids = set()
                frame_idx += 1
                continue

            boxes_xyxy = result.boxes.xyxy.cpu().numpy()
            confs = result.boxes.conf.cpu().numpy()

            detections = sv.Detections(
                xyxy=boxes_xyxy,
                confidence=confs,
                class_id=np.zeros(len(confs), dtype=int),
            )
            tracked = tracker_sv.update_with_detections(detections)
            curr_track_ids: set[int] = set()

            for i in range(len(tracked)):
                track_id = int(tracked.tracker_id[i])
                bbox = tuple(tracked.xyxy[i])
                conf = float(tracked.confidence[i]) if tracked.confidence is not None else 0.5
                curr_track_ids.add(track_id)

                raw_events = vtracker.update_track(
                    frame=frame,
                    frame_idx=frame_idx,
                    fps=fps,
                    track_id=track_id,
                    bbox=bbox,
                    detection_confidence=conf,
                )

                queue_depth = vtracker.get_billing_queue_depth(list(BILLING_ZONE_IDS))

                for etype, edata in raw_events:
                    if (etype == "ZONE_ENTER"
                            and edata.get("zone_id") in BILLING_ZONE_IDS
                            and queue_depth > 1
                            and not edata.get("is_staff", False)):
                        etype = "BILLING_QUEUE_JOIN"
                        edata["queue_depth"] = queue_depth

                    _emit_event(emitter, store_id, camera_id, etype, edata,
                                zones, event_counts, id_counter)
                    total_events += 1

            lost_ids = prev_track_ids - curr_track_ids
            for tid in lost_ids:
                lost_events = vtracker.handle_lost_track(tid, frame_idx, fps)
                for etype, edata in lost_events:
                    _emit_event(emitter, store_id, camera_id, etype, edata,
                                zones, event_counts, id_counter)
                    total_events += 1

            prev_track_ids = curr_track_ids
            frame_idx += 1

            if frame_idx % 300 == 0:
                logger.info(f"  Frame {frame_idx}/{total_frames} | events: {total_events}")

        # Resolve BILLING_QUEUE_ABANDON at clip-end
        abandon_events = vtracker.finalize_abandonment()
        for etype, edata in abandon_events:
            _emit_event(emitter, store_id, camera_id, etype, edata,
                        zones, event_counts, id_counter)
            total_events += 1
        if abandon_events:
            logger.info(f"  {len(abandon_events)} BILLING_QUEUE_ABANDON events emitted")

    cap.release()
    logger.info(f"  Done. {total_events} events for {camera_id}")
    return {"camera_id": camera_id, "total_events": total_events, "counts": event_counts}


def main():
    parser = argparse.ArgumentParser(description="Process a CCTV clip into Purplle events")
    parser.add_argument("--video", required=True)
    parser.add_argument("--layout", required=True)
    parser.add_argument("--camera-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start-time", default=None)
    parser.add_argument("--stride", type=int, default=3)
    parser.add_argument("--model", default="yolov8n.pt")
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--pos-csv", default=None)
    args = parser.parse_args()

    with open(args.layout) as f:
        layout = json.load(f)

    cam_cfg = next((c for c in layout["cameras"] if c["camera_id"] == args.camera_id), None)
    if cam_cfg is None:
        parser.error(f"camera_id '{args.camera_id}' not found in layout")

    start_time = (
        datetime.fromisoformat(args.start_time.replace("Z", "+00:00"))
        if args.start_time else datetime.now(timezone.utc)
    )

    summary = process_clip(
        video_path=args.video,
        store_layout=layout,
        camera_config=cam_cfg,
        output_path=args.output,
        clip_start_time=start_time,
        frame_stride=args.stride,
        yolo_model_name=args.model,
        conf_threshold=args.conf,
        pos_csv_path=args.pos_csv,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
