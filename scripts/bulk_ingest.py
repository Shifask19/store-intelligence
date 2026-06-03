"""Bulk ingest all events from JSONL into the API."""
import httpx, json, time, sys

path = sys.argv[1] if len(sys.argv) > 1 else "data/events_all.jsonl"
api  = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000"

with open(path) as f:
    lines = [l.strip() for l in f if l.strip()]

total_accepted = total_dup = 0
for i in range(0, len(lines), 500):
    batch = [json.loads(l) for l in lines[i:i+500]]
    r = httpx.post(f"{api}/events/ingest", json={"events": batch}, timeout=30)
    d = r.json()
    total_accepted += d.get("accepted", 0)
    total_dup      += d.get("duplicate", 0)
    print(f"Batch {i//500+1}: accepted={d['accepted']} dup={d['duplicate']}")
    time.sleep(0.05)

print(f"\nDone. Total accepted={total_accepted} duplicate={total_dup}")
