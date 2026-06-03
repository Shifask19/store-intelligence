"""
scripts/ingest_events.py — Feed a JSONL events file into the API

Usage:
    python scripts/ingest_events.py \
        --events data/events.jsonl \
        --api    http://localhost:8000 \
        --batch  500
"""

import argparse
import json
import sys
import time

import httpx


def ingest(events_path: str, api_url: str, batch_size: int = 500):
    with open(events_path, encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]

    total = len(lines)
    print(f"Ingesting {total} events in batches of {batch_size}...")

    accepted = duplicate = rejected = 0
    errors = []

    for i in range(0, total, batch_size):
        batch_lines = lines[i : i + batch_size]
        events = [json.loads(l) for l in batch_lines]

        try:
            resp = httpx.post(
                f"{api_url}/events/ingest",
                json={"events": events},
                timeout=30.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                accepted += data.get("accepted", 0)
                duplicate += data.get("duplicate", 0)
                rejected += data.get("rejected", 0)
                errors.extend(data.get("errors", []))
            else:
                print(f"  Batch {i//batch_size + 1}: HTTP {resp.status_code} — {resp.text[:200]}")
        except Exception as e:
            print(f"  Batch {i//batch_size + 1}: Request failed — {e}")

        print(f"  Progress: {min(i + batch_size, total)}/{total}", end="\r")
        time.sleep(0.05)  # gentle rate limiting

    print(f"\nDone. accepted={accepted} duplicate={duplicate} rejected={rejected}")
    if errors:
        print(f"Errors ({len(errors)}):")
        for e in errors[:10]:
            print(f"  {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", required=True)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--batch", type=int, default=500)
    args = parser.parse_args()
    ingest(args.events, args.api, args.batch)
