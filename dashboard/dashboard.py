"""
dashboard/dashboard.py — Live terminal dashboard (Part E)

Uses the `rich` library to render a live-updating terminal UI.
Polls the Intelligence API every REFRESH_SECONDS and displays:
  - Unique visitors today
  - Conversion rate
  - Current queue depth
  - Abandonment rate
  - Zone heatmap (top 5 zones by visit frequency)
  - Active anomalies
  - Feed health per store

Run:
    python dashboard/dashboard.py --api http://localhost:8000 --store STORE_BLR_002

Or via docker compose — the dashboard service calls this automatically.
"""

import argparse
import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

API_URL = os.getenv("API_URL", "http://localhost:8000")
STORE_ID = os.getenv("STORE_ID", "STORE_BLR_002")
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "5"))
# Date to query — defaults to today, override with DATA_DATE env var
# (useful when replaying historical footage like 2026-04-10)
DATA_DATE = os.getenv("DATA_DATE", "")  # empty = today

console = Console()


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
def fetch(path: str) -> dict | None:
    try:
        sep = "&" if "?" in path else "?"
        url = f"{API_URL}{path}{sep}date={DATA_DATE}" if DATA_DATE else f"{API_URL}{path}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status == 200:
                return json.loads(r.read().decode())
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------
def build_metrics_panel(store_id: str) -> Panel:
    data = fetch(f"/stores/{store_id}/metrics")
    if not data:
        return Panel("[red]⚠ API unreachable[/red]", title="Metrics", border_style="red")

    uv = data.get("unique_visitors", 0)
    cr = data.get("conversion_rate", 0.0)
    qd = data.get("current_queue_depth", 0)
    ar = data.get("abandonment_rate", 0.0)
    txn = data.get("total_transactions", 0)

    cr_color = "green" if cr >= 0.15 else "yellow" if cr >= 0.08 else "red"
    qd_color = "red" if qd >= 5 else "yellow" if qd >= 3 else "green"

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column("Metric", style="bold cyan")
    table.add_column("Value")
    table.add_row("👥 Unique Visitors", str(uv))
    table.add_row("💳 Conversion Rate", f"[{cr_color}]{cr:.1%}[/{cr_color}]")
    table.add_row("🧾 Transactions", str(txn))
    table.add_row("🔢 Queue Depth", f"[{qd_color}]{qd}[/{qd_color}]")
    table.add_row("🚪 Abandonment Rate", f"{ar:.1%}")

    return Panel(table, title=f"[bold]📊 Store Metrics — {store_id}[/bold]",
                 border_style="blue")


def build_heatmap_panel(store_id: str) -> Panel:
    data = fetch(f"/stores/{store_id}/heatmap")
    if not data:
        return Panel("[red]⚠ API unreachable[/red]", title="Heatmap", border_style="red")

    zones = data.get("zones", [])[:8]  # top 8
    confidence = data.get("data_confidence", True)

    table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    table.add_column("Zone", style="cyan", min_width=16)
    table.add_column("Visits", justify="right")
    table.add_column("Avg Dwell", justify="right")
    table.add_column("Heat", min_width=20)

    for z in zones:
        score = z.get("normalised_score", 0)
        bar_len = int(score / 5)  # 0–20 chars
        bar = "█" * bar_len + "░" * (20 - bar_len)
        color = "red" if score >= 80 else "yellow" if score >= 40 else "green"
        dwell_s = z.get("avg_dwell_ms", 0) / 1000
        table.add_row(
            z["zone_id"],
            str(z.get("visit_frequency", 0)),
            f"{dwell_s:.0f}s",
            f"[{color}]{bar}[/{color}] {score:.0f}",
        )

    conf_note = "" if confidence else " [yellow](low data confidence)[/yellow]"
    return Panel(table, title=f"[bold]🗺 Zone Heatmap{conf_note}[/bold]",
                 border_style="blue")


def build_anomalies_panel(store_id: str) -> Panel:
    data = fetch(f"/stores/{store_id}/anomalies")
    if not data:
        return Panel("[red]⚠ API unreachable[/red]", title="Anomalies", border_style="red")

    anomalies = data.get("anomalies", [])
    if not anomalies:
        return Panel("[green]✓ No active anomalies[/green]",
                     title="[bold]⚠ Anomalies[/bold]", border_style="green")

    table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    table.add_column("Severity", min_width=8)
    table.add_column("Type", min_width=22)
    table.add_column("Description")
    table.add_column("Action")

    sev_colors = {"CRITICAL": "red", "WARN": "yellow", "INFO": "cyan"}
    for a in anomalies:
        sev = a.get("severity", "INFO")
        color = sev_colors.get(sev, "white")
        table.add_row(
            f"[{color}]{sev}[/{color}]",
            a.get("anomaly_type", ""),
            a.get("description", "")[:60],
            a.get("suggested_action", "")[:50],
        )

    return Panel(table, title="[bold red]⚠ Active Anomalies[/bold red]",
                 border_style="red")


def build_funnel_panel(store_id: str) -> Panel:
    data = fetch(f"/stores/{store_id}/funnel")
    if not data:
        return Panel("[red]⚠ API unreachable[/red]", title="Funnel", border_style="red")

    stages = data.get("stages", [])
    table = Table(box=box.SIMPLE, show_header=True, padding=(0, 2))
    table.add_column("Stage", style="cyan", min_width=16)
    table.add_column("Count", justify="right")
    table.add_column("Drop-off %", justify="right")
    table.add_column("Bar", min_width=20)

    max_count = max((s.get("count", 0) for s in stages), default=1) or 1
    for s in stages:
        count = s.get("count", 0)
        drop = s.get("drop_off_pct", 0.0)
        bar_len = int((count / max_count) * 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        drop_color = "red" if drop > 50 else "yellow" if drop > 25 else "green"
        table.add_row(
            s.get("stage", ""),
            str(count),
            f"[{drop_color}]{drop:.1f}%[/{drop_color}]",
            f"[blue]{bar}[/blue]",
        )

    return Panel(table, title="[bold]🔽 Conversion Funnel[/bold]", border_style="blue")


def build_health_panel() -> Panel:
    data = fetch("/health")
    if not data:
        return Panel("[red]⚠ API unreachable[/red]", title="Health", border_style="red")

    db_status = data.get("db_status", "unknown")
    overall = data.get("status", "unknown")
    stores = data.get("stores", [])

    db_color = "green" if db_status == "ok" else "red"
    ov_color = "green" if overall == "healthy" else "red"

    lines = [
        f"Overall: [{ov_color}]{overall}[/{ov_color}]   DB: [{db_color}]{db_status}[/{db_color}]",
        "",
    ]
    for s in stores:
        st = s.get("status", "?")
        lag = s.get("lag_minutes", 0)
        st_color = "green" if st == "OK" else "red"
        lines.append(f"  [{st_color}]{s['store_id']}[/{st_color}] — {st} (lag: {lag:.1f} min)")

    return Panel("\n".join(lines), title="[bold]💚 Health[/bold]", border_style="green")


# ---------------------------------------------------------------------------
# Main render loop
# ---------------------------------------------------------------------------
def build_layout(store_id: str) -> Layout:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="top", size=14),
        Layout(name="middle", size=14),
        Layout(name="bottom", size=8),
    )
    layout["header"].update(
        Panel(
            f"[bold cyan]🏪 Store Intelligence Dashboard[/bold cyan]  "
            f"[dim]Store: {store_id}  |  Refreshing every {REFRESH_SECONDS}s  |  {now}[/dim]",
            border_style="cyan",
        )
    )
    layout["top"].split_row(
        Layout(build_metrics_panel(store_id), name="metrics"),
        Layout(build_funnel_panel(store_id), name="funnel"),
    )
    layout["middle"].split_row(
        Layout(build_heatmap_panel(store_id), name="heatmap"),
        Layout(build_anomalies_panel(store_id), name="anomalies"),
    )
    layout["bottom"].update(build_health_panel())
    return layout


def run_dashboard(store_id: str, refresh: int):
    with Live(
        build_layout(store_id),
        refresh_per_second=1,
        screen=True,
        console=console,
    ) as live:
        while True:
            time.sleep(refresh)
            live.update(build_layout(store_id))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Store Intelligence Live Dashboard")
    parser.add_argument("--api", default=API_URL, help="API base URL")
    parser.add_argument("--store", default=STORE_ID, help="Store ID to monitor")
    parser.add_argument("--refresh", type=int, default=REFRESH_SECONDS,
                        help="Refresh interval in seconds")
    parser.add_argument("--date", default="", help="Date to query YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    API_URL = args.api
    DATA_DATE = args.date
    console.print(f"[cyan]Connecting to {API_URL} — monitoring {args.store}[/cyan]")
    if DATA_DATE:
        console.print(f"[dim]Querying date: {DATA_DATE}[/dim]")
    run_dashboard(args.store, args.refresh)
