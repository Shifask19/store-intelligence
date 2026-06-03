"""Quick check of all API endpoints."""
import urllib.request, json, time

time.sleep(2)  # wait for --reload to pick up changes

base  = "http://localhost:8000"
store = "STORE_BLR_002"
date  = "2026-04-10"

print("=== /metrics ===")
r = urllib.request.urlopen(f"{base}/stores/{store}/metrics?date={date}")
m = json.loads(r.read())
print("unique_visitors :", m["unique_visitors"])
print("conversion_rate :", m["conversion_rate"])
print("abandonment_rate:", m["abandonment_rate"])
print("queue_depth     :", m["current_queue_depth"])
print("transactions    :", m["total_transactions"])
for z in m["avg_dwell_per_zone"][:3]:
    print(f"  dwell {z['zone_id']:15s}: {z['avg_dwell_ms']/1000:.1f}s ({z['visit_count']} visits)")

print()
print("=== /funnel ===")
r = urllib.request.urlopen(f"{base}/stores/{store}/funnel?date={date}")
f = json.loads(r.read())
for s in f["stages"]:
    print(f"  {s['stage']:15s}  count={s['count']:4d}  drop={s['drop_off_pct']}%")

print()
print("=== /heatmap (top 4) ===")
r = urllib.request.urlopen(f"{base}/stores/{store}/heatmap?date={date}")
h = json.loads(r.read())
print("data_confidence:", h["data_confidence"])
for z in h["zones"][:4]:
    print(f"  {z['zone_id']:15s}  score={z['normalised_score']:5.1f}  dwell={z['avg_dwell_ms']/1000:.1f}s")

print()
print("=== /anomalies ===")
r = urllib.request.urlopen(f"{base}/stores/{store}/anomalies?date={date}")
a = json.loads(r.read())
if a["anomalies"]:
    for x in a["anomalies"]:
        print(f"  [{x['severity']}] {x['anomaly_type']}: {x['description'][:60]}")
else:
    print("  No active anomalies")

print()
print("=== /health ===")
r = urllib.request.urlopen(f"{base}/health")
hh = json.loads(r.read())
print("status    :", hh["status"])
print("db_status :", hh["db_status"])
for s in hh["stores"]:
    print(f"  {s['store_id']}  {s['status']}  lag={s['lag_minutes']}min")
