#!/usr/bin/env python3
"""
dashboard.py - live monitor for the Cerberus paper-trading run.

Redraws a compact status table every --interval seconds:
  - last [LIVE STATS] line from paper_run.log
  - live aggregates straight from cerberus.db (paper_signals table)
  - a "last 10 minutes" slice, to catch a reason that's currently dominating
  - edge/pnl stats among rows that actually produced a signal
  - whether the python paper-run process is still alive

Read-only. Never touches the running process or the database schema.

Usage:
  python dashboard.py [--interval 30] [--db cerberus.db] [--log paper_run.log]
                       [--errlog paper_run_err.log] [--pid 31476] [--once]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cerberus paper-run dashboard")
    p.add_argument("--interval", type=int, default=30, help="Refresh interval in seconds")
    p.add_argument("--db", default="cerberus.db", help="Path to cerberus.db")
    p.add_argument("--log", default="paper_run.log", help="Path to paper_run.log (stdout)")
    p.add_argument("--errlog", default="paper_run_err.log", help="Path to paper_run_err.log (stderr)")
    p.add_argument("--pid", type=int, default=None, help="PID of the paper-run python process (from Start-Process -PassThru)")
    p.add_argument("--recent-minutes", type=int, default=10, help="Window size for the 'recent' slice")
    p.add_argument("--once", action="store_true", help="Print one snapshot and exit (no loop, no screen clear)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Log tailing
# ---------------------------------------------------------------------------

def last_live_stats_line(log_path: str) -> str:
    if not os.path.exists(log_path):
        return f"(no such file: {log_path})"
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as exc:
        return f"(read error: {exc})"
    for line in reversed(lines):
        if "[LIVE STATS]" in line:
            return line.strip()
    return "(no [LIVE STATS] line yet - first print is 15 min after start)"


def log_mtime_str(log_path: str) -> str:
    if not os.path.exists(log_path):
        return "n/a"
    age_s = time.time() - os.path.getmtime(log_path)
    return f"{age_s:,.0f}s ago"


# ---------------------------------------------------------------------------
# DB aggregates
# ---------------------------------------------------------------------------

def connect_ro(db_path: str) -> sqlite3.Connection:
    # timeout= lets sqlite3 retry internally instead of raising "database is locked"
    # immediately if the writer holds a brief lock during commit.
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_summary(conn: sqlite3.Connection, since_clause: str = "", params: tuple = ()) -> dict:
    cur = conn.execute(
        f"SELECT result, rejection_reason, edge_net_pct, simulated_pnl "
        f"FROM paper_signals {since_clause}",
        params,
    )
    rows = cur.fetchall()

    by_result: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    edge_pcts: list[float] = []
    pnls: list[float] = []

    for r in rows:
        result = r["result"] or "?"
        by_result[result] = by_result.get(result, 0) + 1

        if result in ("BLOCKED_BY_RISK", "STALE_BOOK"):
            reason = r["rejection_reason"] or "(none)"
            by_reason[reason] = by_reason.get(reason, 0) + 1

        if result not in ("BLOCKED_BY_RISK", "STALE_BOOK"):
            if r["edge_net_pct"] is not None:
                edge_pcts.append(float(r["edge_net_pct"]))
            pnls.append(float(r["simulated_pnl"]))

    return {
        "total": len(rows),
        "by_result": by_result,
        "by_reason": by_reason,
        "edge_pcts": edge_pcts,
        "pnls": pnls,
    }


def fmt_stats(values: list[float]) -> str:
    if not values:
        return "n/a (0 rows)"
    return (
        f"mean={statistics.mean(values):+.4f}  "
        f"median={statistics.median(values):+.4f}  "
        f"n={len(values)}"
    )


# ---------------------------------------------------------------------------
# Process check (no psutil dependency - shells out to tasklist)
# ---------------------------------------------------------------------------

def process_status(pid: int | None) -> str:
    is_windows = os.name == "nt"
    try:
        if pid is not None:
            if is_windows:
                out = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                if out and out.upper().startswith('"PYTHON'):
                    return f"ALIVE  (pid={pid})"
                return f"NOT FOUND  (pid={pid} - process exited or wrong pid)"
            alive = subprocess.run(
                ["kill", "-0", str(pid)], capture_output=True, timeout=5,
            ).returncode == 0
            return f"ALIVE  (pid={pid})" if alive else f"NOT FOUND  (pid={pid} - process exited or wrong pid)"
        else:
            if is_windows:
                out = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                if not out or "INFO:" in out:
                    return "NO python.exe PROCESSES RUNNING"
                pids = []
                for line in out.splitlines():
                    parts = [c.strip('"') for c in line.split('","')]
                    if len(parts) >= 2:
                        pids.append(parts[1])
                return f"{len(pids)} python.exe process(es) running: pid(s)={', '.join(pids)}  (pass --pid to pin down which one)"
            out = subprocess.run(
                ["pgrep", "-f", "cerberustest.py"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            pids = [p for p in out.splitlines() if p]
            if not pids:
                return "NO cerberustest.py PROCESSES RUNNING"
            return f"{len(pids)} cerberustest.py process(es) running: pid(s)={', '.join(pids)}  (pass --pid to pin down which one)"
    except Exception as exc:
        return f"(could not check: {exc})"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(args: argparse.Namespace) -> str:
    out = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out.append("=" * 78)
    out.append(f" CERBERUS PAPER-RUN DASHBOARD   {now}   (refresh={args.interval}s)")
    out.append("=" * 78)

    out.append("")
    out.append(f"[process]   {process_status(args.pid)}")
    out.append(f"[log file]  {args.log}  (last write: {log_mtime_str(args.log)})")
    out.append(f"[err log]   {args.errlog}  (last write: {log_mtime_str(args.errlog)})")

    out.append("")
    out.append("[LIVE STATS] (from paper_run.log, prints every 15 min):")
    out.append("  " + last_live_stats_line(args.log))

    if not os.path.exists(args.db):
        out.append("")
        out.append(f"!! DB not found: {args.db}")
        return "\n".join(out)

    conn = connect_ro(args.db)
    try:
        all_time = fetch_summary(conn)

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=args.recent_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        recent = fetch_summary(
            conn, "WHERE recorded_at >= ?", (cutoff,)
        )
    finally:
        conn.close()

    out.append("")
    out.append(f"[DB all-time]  total rows={all_time['total']}")
    out.append("  by result:   " + (", ".join(f"{k}={v}" for k, v in sorted(all_time["by_result"].items(), key=lambda x: -x[1])) or "(none)"))
    out.append("  by reason (blocked/stale only): " + (", ".join(f"{k}={v}" for k, v in sorted(all_time["by_reason"].items(), key=lambda x: -x[1])) or "(none)"))

    out.append("")
    out.append(f"[DB last {args.recent_minutes} min]  total rows={recent['total']}  (recorded_at is UTC)")
    out.append("  by result:   " + (", ".join(f"{k}={v}" for k, v in sorted(recent["by_result"].items(), key=lambda x: -x[1])) or "(none)"))
    out.append("  by reason (blocked/stale only): " + (", ".join(f"{k}={v}" for k, v in sorted(recent["by_reason"].items(), key=lambda x: -x[1])) or "(none)"))
    if recent["total"] > 0:
        dominant_reason = max(recent["by_reason"].items(), key=lambda x: x[1], default=None)
        if dominant_reason and dominant_reason[1] / recent["total"] > 0.5:
            out.append(f"  !! WARNING: '{dominant_reason[0]}' is {dominant_reason[1]}/{recent['total']} "
                        f"({100*dominant_reason[1]/recent['total']:.0f}%) of the last {args.recent_minutes} min - likely still bottlenecked.")

    out.append("")
    out.append("[edge_net_pct among non-blocked rows]  " + fmt_stats(all_time["edge_pcts"]))
    out.append("[simulated_pnl among non-blocked rows] " + fmt_stats(all_time["pnls"]))

    out.append("")
    out.append("=" * 78)
    return "\n".join(out)


def main() -> int:
    args = parse_args()

    if args.once:
        print(render(args))
        return 0

    try:
        while True:
            screen = render(args)
            os.system("cls" if os.name == "nt" else "clear")
            print(screen)
            print(f"\n(Ctrl+C to stop monitoring - this does not touch the paper-run process)")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
