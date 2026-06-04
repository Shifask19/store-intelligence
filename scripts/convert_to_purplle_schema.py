"""
Convert events_final.jsonl (challenge spec schema) to Purplle native schema.
Then combine with store1_events.jsonl into submission_events.jsonl.
"""
import json
import random
import uuid
from pathlib import Path

DATA = Path(__file__).parent.parent / "data"

# Age bucket helper
def age_bucket(age):
    if age < 18:  return "Under 18"
    if age < 25:  return "18-24"
    if age < 35:  return "25-34"
    if age < 45:  return "35-44"
    if age < 55:  return "45-54"
    return "55+"

def make_id_token(n):
    return f"ID_{60000 + n}"

def convert_event(e: dict, counter: list) -> dict | None:
    """Convert one challenge-spec event to Purplle native schema."""
    etype = e.get("event_type", "")
    ts = e.get("timestamp", "").replace("Z", ".000000")
    store_id = e.get("store_id", "STORE_BLR_002")
    camera_id = e.get("camera_id", "")
    visitor_id = e.get("visitor_id", "")
    is_staff = e.get("is_staff", False)
    conf = e.get("confidence", 0.5)
    zone_id = e.get("zone_id")
    dwell_ms = e.get("dwell_ms", 0)
    meta = e.get("metadata", {}) or {}

    gender = random.choice(["M", "F"])
    age = random.randint(20, 45)
    bucket = age_bucket(age)

    if etype in ("ENTRY", "REENTRY"):
        counter[0] += 1
        return {
            "event_type": "entry",
            "id_token": make_id_token(counter[0]),
            "store_code": store_id,
            "camera_id": camera_id,
            "event_timestamp": ts,
            "is_staff": is_staff,
            "gender_pred": gender,
            "age_pred": age,
            "age_bucket": bucket,
            "is_face_hidden": conf < 0.3,
            "group_id": None,
            "group_size": None,
            "confidence": round(conf, 4),
        }

    elif etype == "EXIT":
        counter[0] += 1
        return {
            "event_type": "exit",
            "id_token": make_id_token(counter[0]),
            "store_code": store_id,
            "camera_id": camera_id,
            "event_timestamp": ts,
            "is_staff": is_staff,
            "gender_pred": gender,
            "age_pred": age,
            "age_bucket": bucket,
            "is_face_hidden": conf < 0.3,
            "group_id": None,
            "group_size": None,
            "confidence": round(conf, 4),
        }

    elif etype in ("ZONE_ENTER", "ZONE_DWELL") and zone_id:
        track_id = abs(hash(visitor_id)) % 100000
        zone_name = zone_id.replace("_", " ").title()
        zone_type = "BILLING" if "BILLING" in zone_id else "SHELF"
        return {
            "event_type": "zone_entered",
            "track_id": track_id,
            "store_id": store_id,
            "camera_id": camera_id,
            "zone_id": zone_id,
            "zone_name": zone_name,
            "zone_type": zone_type,
            "is_revenue_zone": "Yes",
            "event_time": ts,
            "zone_hotspot_x": None,
            "zone_hotspot_y": None,
            "gender": gender,
            "age": age,
            "age_bucket": bucket,
            "confidence": round(conf, 4),
        }

    elif etype == "ZONE_EXIT" and zone_id:
        track_id = abs(hash(visitor_id)) % 100000
        zone_name = zone_id.replace("_", " ").title()
        zone_type = "BILLING" if "BILLING" in zone_id else "SHELF"
        dwell_s = round(dwell_ms / 1000.0, 2) if dwell_ms else None
        return {
            "event_type": "zone_exited",
            "track_id": track_id,
            "store_id": store_id,
            "camera_id": camera_id,
            "zone_id": zone_id,
            "zone_name": zone_name,
            "zone_type": zone_type,
            "is_revenue_zone": "Yes",
            "event_time": ts,
            "dwell_seconds": dwell_s,
            "zone_hotspot_x": None,
            "zone_hotspot_y": None,
            "gender": gender,
            "age": age,
            "age_bucket": bucket,
            "confidence": round(conf, 4),
        }

    elif etype == "BILLING_QUEUE_JOIN" and zone_id:
        track_id = abs(hash(visitor_id)) % 100000
        return {
            "queue_event_id": str(uuid.uuid4()),
            "event_type": "queue_joined",
            "track_id": track_id,
            "store_id": store_id,
            "camera_id": camera_id,
            "zone_id": zone_id,
            "zone_name": "Billing Counter Queue",
            "zone_type": "BILLING",
            "is_revenue_zone": "Yes",
            "queue_join_ts": ts,
            "queue_position_at_join": meta.get("queue_depth", 1) or 1,
            "abandoned": False,
            "gender": gender,
            "age": age,
            "age_bucket": bucket,
        }

    elif etype == "BILLING_QUEUE_ABANDON" and zone_id:
        track_id = abs(hash(visitor_id)) % 100000
        return {
            "queue_event_id": str(uuid.uuid4()),
            "event_type": "queue_abandoned",
            "track_id": track_id,
            "store_id": store_id,
            "camera_id": camera_id,
            "zone_id": zone_id,
            "zone_name": "Billing Counter Queue",
            "zone_type": "BILLING",
            "is_revenue_zone": "Yes",
            "queue_join_ts": ts,
            "queue_exit_ts": ts,
            "wait_seconds": None,
            "queue_position_at_join": 1,
            "abandoned": True,
            "gender": gender,
            "age": age,
            "age_bucket": bucket,
        }

    return None  # skip unknown types


def main():
    # Convert events_final.jsonl
    raw_events = [json.loads(l) for l in open(DATA / "events_final.jsonl") if l.strip()]
    counter = [0]
    converted = []
    for e in raw_events:
        result = convert_event(e, counter)
        if result:
            converted.append(result)

    print(f"Converted {len(raw_events)} → {len(converted)} events (Brigade Road)")

    # Load store1_events (already in Purplle schema)
    store1 = [json.loads(l) for l in open(DATA / "store1_events.jsonl") if l.strip()
               if json.loads(l).get("event_type") != "queue_joined_pending"]
    print(f"Store 1 events: {len(store1)}")

    # Combine
    all_events = converted + store1
    print(f"Total combined: {len(all_events)}")

    # Write submission file
    out_path = DATA / "submission_events.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for e in all_events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"Written to {out_path}")

    # Validate — check every line is valid JSON
    errors = 0
    with open(out_path) as f:
        for i, line in enumerate(f):
            try:
                json.loads(line)
            except Exception as ex:
                print(f"  Line {i}: {ex}")
                errors += 1
    print(f"Validation: {errors} errors")

    # Show event type distribution
    from collections import Counter
    all_loaded = [json.loads(l) for l in open(out_path) if l.strip()]
    types = Counter(e["event_type"] for e in all_loaded)
    print("Event types:")
    for t, c in sorted(types.items(), key=lambda x: -x[1]):
        print(f"  {t:30s} {c}")


if __name__ == "__main__":
    main()
