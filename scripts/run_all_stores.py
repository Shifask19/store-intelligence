"""
Run detection pipeline on all store footage and combine into one events file.
"""
import json
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline.detect import process_clip

BASE = Path(r"C:\Users\shifa\OneDrive\Desktop\purplle challenge")
PROJ = Path(__file__).parent.parent

CAMERAS = [
    # Store 1
    {
        "video": BASE / "Store_1_footage/Store 1/CAM 3 - entry.mp4",
        "layout": PROJ / "data/store1_layout.json",
        "camera_id": "CAM_ENTRY_03",
        "output": PROJ / "data/store1_events.jsonl",
        "start": "2026-04-10T10:00:00Z",
        "pos_csv": PROJ / "data/pos_sample.csv",
    },
    {
        "video": BASE / "Store_1_footage/Store 1/CAM 1 - zone.mp4",
        "layout": PROJ / "data/store1_layout.json",
        "camera_id": "CAM_ZONE_01",
        "output": PROJ / "data/store1_events.jsonl",
        "start": "2026-04-10T10:00:00Z",
    },
    {
        "video": BASE / "Store_1_footage/Store 1/CAM 2 - zone.mp4",
        "layout": PROJ / "data/store1_layout.json",
        "camera_id": "CAM_ZONE_02",
        "output": PROJ / "data/store1_events.jsonl",
        "start": "2026-04-10T10:00:00Z",
    },
    {
        "video": BASE / "Store_1_footage/Store 1/CAM 5 - billing.mp4",
        "layout": PROJ / "data/store1_layout.json",
        "camera_id": "CAM_BILLING_05",
        "output": PROJ / "data/store1_events.jsonl",
        "start": "2026-04-10T10:00:00Z",
        "pos_csv": PROJ / "data/pos_sample.csv",
    },
    # Store 2
    {
        "video": BASE / "Store_2_footage/Store 2/entry 1.mp4",
        "layout": PROJ / "data/store2_layout.json",
        "camera_id": "CAM_ENTRY_01",
        "output": PROJ / "data/store2_events.jsonl",
        "start": "2026-04-10T10:00:00Z",
        "pos_csv": PROJ / "data/pos_sample.csv",
    },
    {
        "video": BASE / "Store_2_footage/Store 2/entry 2.mp4",
        "layout": PROJ / "data/store2_layout.json",
        "camera_id": "CAM_ENTRY_02",
        "output": PROJ / "data/store2_events.jsonl",
        "start": "2026-04-10T10:00:00Z",
    },
    {
        "video": BASE / "Store_2_footage/Store 2/zone.mp4",
        "layout": PROJ / "data/store2_layout.json",
        "camera_id": "CAM_ZONE_01",
        "output": PROJ / "data/store2_events.jsonl",
        "start": "2026-04-10T10:00:00Z",
    },
    {
        "video": BASE / "Store_2_footage/Store 2/billing_area.mp4",
        "layout": PROJ / "data/store2_layout.json",
        "camera_id": "CAM_BILLING_01",
        "output": PROJ / "data/store2_events.jsonl",
        "start": "2026-04-10T10:00:00Z",
        "pos_csv": PROJ / "data/pos_sample.csv",
    },
]

# Clear output files
for path in [PROJ / "data/store1_events.jsonl", PROJ / "data/store2_events.jsonl"]:
    open(path, "w").close()

total = 0
for cam in CAMERAS:
    layout = json.load(open(cam["layout"]))
    cam_cfg = next(c for c in layout["cameras"] if c["camera_id"] == cam["camera_id"])
    start = datetime.fromisoformat(cam["start"].replace("Z", "+00:00"))
    pos = str(cam.get("pos_csv", "")) or None

    print(f"\n=== {cam['camera_id']} ===")
    try:
        result = process_clip(
            video_path=str(cam["video"]),
            store_layout=layout,
            camera_config=cam_cfg,
            output_path=str(cam["output"]),
            clip_start_time=start,
            frame_stride=3,
            pos_csv_path=pos,
        )
        total += result["total_events"]
        print(f"  Events: {result['total_events']} | {result['counts']}")
    except Exception as e:
        print(f"  ERROR: {e}")

# Combine all into one file
all_lines = []
for f in [PROJ / "data/store1_events.jsonl",
          PROJ / "data/store2_events.jsonl",
          PROJ / "data/events_final.jsonl"]:
    if Path(f).exists():
        with open(f) as fh:
            all_lines.extend([l for l in fh if l.strip()])

with open(PROJ / "data/events_all_stores.jsonl", "w") as out:
    out.writelines(all_lines)

print(f"\n=== DONE. Total new events: {total} ===")
print(f"Combined file: data/events_all_stores.jsonl ({len(all_lines)} lines)")
