#!/usr/bin/env bash
# pipeline/run.sh — One command to process all clips → events.jsonl
#
# Usage:
#   bash pipeline/run.sh \
#     --footage-dir /data/footage \
#     --layout      /data/store_layout.json \
#     --output      /data/events.jsonl \
#     --start-time  "2026-04-10T10:00:00Z" \
#     --pos-csv     /data/pos_transactions.csv
#
# The script processes cameras in a defined order so the shared visitor
# registry (passed via a temp JSON file) enables cross-camera Re-ID.
# Entry camera is processed first so visitor_ids are established before
# floor/billing cameras try to match them.
#
# --pos-csv is required for BILLING_QUEUE_ABANDON detection. The pipeline
# loads POS transactions at startup and resolves abandonment at clip-end.

set -euo pipefail

FOOTAGE_DIR=""
LAYOUT=""
OUTPUT=""
START_TIME=""
POS_CSV=""
STRIDE=3
MODEL="yolov8n.pt"
CONF=0.25

# Parse args
while [[ $# -gt 0 ]]; do
  case $1 in
    --footage-dir) FOOTAGE_DIR="$2"; shift 2 ;;
    --layout)      LAYOUT="$2";      shift 2 ;;
    --output)      OUTPUT="$2";      shift 2 ;;
    --start-time)  START_TIME="$2";  shift 2 ;;
    --pos-csv)     POS_CSV="$2";     shift 2 ;;
    --stride)      STRIDE="$2";      shift 2 ;;
    --model)       MODEL="$2";       shift 2 ;;
    --conf)        CONF="$2";        shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$FOOTAGE_DIR" || -z "$LAYOUT" || -z "$OUTPUT" ]]; then
  echo "Usage: $0 --footage-dir DIR --layout FILE --output FILE [--start-time ISO8601] [--pos-csv FILE]"
  exit 1
fi

# Default start time to now if not provided
if [[ -z "$START_TIME" ]]; then
  START_TIME=$(python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")
fi

echo "=== Store Intelligence Pipeline ==="
echo "Footage dir : $FOOTAGE_DIR"
echo "Layout      : $LAYOUT"
echo "Output      : $OUTPUT"
echo "Start time  : $START_TIME"
if [[ -n "$POS_CSV" ]]; then
  echo "POS CSV     : $POS_CSV"
else
  echo "POS CSV     : (not provided — BILLING_QUEUE_ABANDON will not be emitted)"
fi
echo ""

# Clear output file (fresh run)
> "$OUTPUT"

# Camera processing order: entry first, then floor, then billing
# This ensures visitor_ids are seeded before cross-camera Re-ID runs
CAMERA_ORDER=(
  "CAM_ENTRY_01:CAM 1.mp4"
  "CAM_FLOOR_02:CAM 2.mp4"
  "CAM_FLOOR_03:CAM 3.mp4"
  "CAM_BILLING_04:CAM 4.mp4"
  "CAM_BILLING_05:CAM 5.mp4"
)

for ENTRY in "${CAMERA_ORDER[@]}"; do
  CAM_ID="${ENTRY%%:*}"
  CAM_FILE="${ENTRY##*:}"
  VIDEO_PATH="$FOOTAGE_DIR/$CAM_FILE"

  if [[ ! -f "$VIDEO_PATH" ]]; then
    echo "WARNING: Video not found: $VIDEO_PATH — skipping $CAM_ID"
    continue
  fi

  echo "--- Processing $CAM_ID ($CAM_FILE) ---"

  POS_ARG=""
  if [[ -n "$POS_CSV" ]]; then
    POS_ARG="--pos-csv $POS_CSV"
  fi

  python3 -m pipeline.detect \
    --video      "$VIDEO_PATH" \
    --layout     "$LAYOUT" \
    --camera-id  "$CAM_ID" \
    --output     "$OUTPUT" \
    --start-time "$START_TIME" \
    --stride     "$STRIDE" \
    --model      "$MODEL" \
    --conf       "$CONF" \
    $POS_ARG

  COUNT=$(wc -l < "$OUTPUT")
  echo "  Cumulative events: $COUNT"
done

FINAL_COUNT=$(wc -l < "$OUTPUT")
echo ""
echo "=== Pipeline complete. Total events: $FINAL_COUNT ==="
echo "Output: $OUTPUT"
