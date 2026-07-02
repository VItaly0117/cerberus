#!/usr/bin/env python3
"""
monitor_paper_run.py — read-only watchdog for the Cerberus paper run.

Runs standalone (no dependency on the Claude session) via launchd. Checks
process liveness + paper_signals aggregates, appends a timestamped entry to
the monitoring log, and sends a Telegram alert only on a *state change*
(process died, or a result outside FILTERED/BLOCKED_BY_RISK appears for the
first time) — not on every run, to avoid spam.

Never modifies cerberus.db, .env, or touches the running process. Read-only
observation only.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB_PATH = REPO / "cerberus.db"
LOG_PATH = REPO / "artifacts" / "paper" / "monitoring_log_20260702_night.md"
STATE_PATH = REPO / "artifacts" / "paper" / ".monitor_state.json"

sys.path.insert(0, str(REPO))
import notify  # noqa: E402  (reuses _load_env / send_message)


def _proc_alive() -> str | None:
    """Return the PID string if cerberustest.py is running, else None."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", "cerberustest.py"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return out.splitlines()[0] if out else None
    except Exception:
        return None


def _query_db() -> dict:
    if not DB_PATH.exists():
        return {"error": "cerberus.db not found"}
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()
    cur.execute("SELECT result, COUNT(*) FROM paper_signals GROUP BY result")
    result_breakdown = dict(cur.fetchall())
    cur.execute(
        "SELECT COUNT(*) FROM paper_signals WHERE rejection_reason LIKE '%hourly_attempt_cap%'"
    )
    hourly_cap_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT market_id) FROM paper_signals")
    distinct_markets = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM paper_signals")
    total = cur.fetchone()[0]
    con.close()
    return {
        "result_breakdown": result_breakdown,
        "hourly_cap_count": hourly_cap_count,
        "distinct_markets": distinct_markets,
        "total": total,
    }


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"was_alive": True, "non_filtered_count": 0}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def main() -> None:
    notify._load_env(REPO / ".env")
    token = __import__("os").environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = __import__("os").environ.get("TELEGRAM_CHAT_ID", "").strip()

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    pid = _proc_alive()
    db = _query_db()
    state = _load_state()

    alert_lines = []

    if pid is None and state.get("was_alive", True):
        alert_lines.append(
            f"⚠️ Cerberus paper run process is DOWN (was alive last check). "
            f"Time: {now}. Not restarting automatically — needs your decision."
        )

    non_filtered = 0
    if "result_breakdown" in db:
        non_filtered = sum(
            n for r, n in db["result_breakdown"].items()
            if r not in ("FILTERED", "BLOCKED_BY_RISK")
        )
        if non_filtered > state.get("non_filtered_count", 0):
            new_rows = non_filtered - state.get("non_filtered_count", 0)
            alert_lines.append(
                f"🔔 First non-FILTERED/BLOCKED_BY_RISK result(s) appeared! "
                f"+{new_rows} new row(s). Breakdown: {db['result_breakdown']}. "
                f"Time: {now}. Not touching the process — review in the morning."
            )

    # ── append to monitoring log ──────────────────────────────────────────
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"\n## {now}\n\n")
        if pid is None:
            f.write("- Процесс НЕ жив (см. алерт).\n")
        else:
            f.write(f"- Процесс жив (PID {pid}).\n")
        if "error" in db:
            f.write(f"- Ошибка чтения БД: {db['error']}\n")
        else:
            f.write(f"- Всего paper_signals: {db['total']}\n")
            f.write(f"- result breakdown: {db['result_breakdown']}\n")
            f.write(f"- hourly_attempt_cap упоминаний: {db['hourly_cap_count']}\n")
            f.write(f"- Уникальных рынков: {db['distinct_markets']}\n")
        if alert_lines:
            f.write("\n**ИНЦИДЕНТ / ИЗМЕНЕНИЕ СОСТОЯНИЯ:**\n")
            for line in alert_lines:
                f.write(f"- {line}\n")

    # ── send Telegram alert only on state change ──────────────────────────
    if alert_lines and token and chat_id:
        notify.send_message(token, chat_id, "\n".join(alert_lines))

    _save_state({
        "was_alive": pid is not None,
        "non_filtered_count": non_filtered,
    })


if __name__ == "__main__":
    main()
