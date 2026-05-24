#!/usr/bin/env python3
"""
notify.py — отправляет итоговый отчёт Cerberus в Telegram.

Запускается автоматически из install_and_run.sh сразу после окончания
paper run. Можно вызвать вручную в любой момент:

    python3 notify.py

Требует переменных в .env:
    TELEGRAM_BOT_TOKEN=<токен от @BotFather>
    TELEGRAM_CHAT_ID=<ваш chat id>
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib import error, parse, request


# ---------------------------------------------------------------------------
# .env loader (без сторонних библиотек)
# ---------------------------------------------------------------------------

def _load_env(env_path: Path) -> None:
    """Читает .env и добавляет значения в os.environ (не перезаписывает)."""
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip("\"'")
        os.environ.setdefault(key.strip(), val)


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

_API = "https://api.telegram.org/bot{token}/{method}"


def _tg_post(token: str, method: str, payload: dict) -> bool:
    """POST к Telegram Bot API через curl (обходит SSL-проблемы urllib)."""
    url = _API.format(token=token, method=method)
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-X", "POST", url,
                "-H", "Content-Type: application/json",
                "-d", json.dumps(payload, ensure_ascii=False),
            ],
            capture_output=True, timeout=15,
        )
        resp = json.loads(result.stdout)
        return resp.get("ok", False)
    except Exception as exc:
        print(f"[notify] Telegram API error: {exc}", file=sys.stderr)
        return False


def send_message(token: str, chat_id: str, text: str) -> bool:
    return _tg_post(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    })


def send_document(token: str, chat_id: str, file_path: Path, caption: str = "") -> bool:
    """Отправляет файл через curl (multipart — проще чем urllib)."""
    try:
        result = subprocess.run(
            [
                "curl", "-s",
                "-F", f"chat_id={chat_id}",
                "-F", f"document=@{file_path}",
                "-F", f"caption={caption}",
                _API.format(token=token, method="sendDocument"),
            ],
            capture_output=True, timeout=30,
        )
        resp = json.loads(result.stdout)
        return resp.get("ok", False)
    except Exception as exc:
        print(f"[notify] sendDocument failed: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Report discovery
# ---------------------------------------------------------------------------

def _latest_report(artifacts_dir: Path) -> Optional[Path]:
    reports = sorted(artifacts_dir.glob("report_*.json"))
    return reports[-1] if reports else None


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def _build_message(report: dict) -> str:
    r = report["results"]
    g = report["gate_status"]
    sc = report["stop_conditions_triggered"]
    hours = report["run_duration_hours"]

    pnl = float(r["total_simulated_pnl"])
    edge_pct = float(r["median_edge_net_pct"]) * 100
    viable = int(r["viable_signals"])
    successes = int(r["successes"])
    legged = int(r["legged_incidents"])

    pnl_emoji = "🟢" if pnl > 0 else "🔴"
    edge_emoji = "📈" if edge_pct > 2 else ("📉" if edge_pct <= 0 else "📊")

    lines = [
        "🐺 *Cerberus Paper Run — Завершён*",
        "━━━━━━━━━━━━━━━━━━",
        f"⏱ Длительность: {hours:.1f}ч",
        f"📊 Снапшотов оценено: {int(r['total_snapshots_evaluated']):,}",
        f"📡 Viable сигналов: {viable}",
        f"✅ Успешных: {successes}",
        f"💨 Clean miss: {int(r['clean_misses'])}",
        f"⚡ Legged risk: {legged}",
        f"🚫 Заблокировано: {int(r['blocked_by_risk']):,}",
        f"📚 Stale book: {int(r['stale_book_rejections'])}",
        "━━━━━━━━━━━━━━━━━━",
        f"{pnl_emoji} Simulated P&L: *{pnl:+.4f} USDC*",
        f"{edge_emoji} Медиана edge: *{edge_pct:.2f}%*",
        f"🔒 Stale rate: {g['stale_book_rate_pct']:.1f}%",
        f"⚡ Legged rate: {g['legged_incident_rate_pct']:.1f}%",
        "━━━━━━━━━━━━━━━━━━",
    ]

    # Stop conditions
    if sc:
        lines.append(f"🚨 Stop conditions: `{', '.join(sc)}`")
    else:
        lines.append("✅ Stop conditions: нет")

    # Verdict
    lines.append("")
    if pnl > 5 and edge_pct > 2 and viable > 30 and not sc:
        lines.append("🏆 *Вердикт: Стратегия работает — анализировать для live*")
    elif pnl > 0 and edge_pct > 1 and viable > 5:
        lines.append("⚠️ *Вердикт: Edge слабый — оптимизировать параметры*")
    elif viable == 0:
        lines.append("❌ *Вердикт: Сигналов нет — рынок эффективен*")
    else:
        lines.append("❌ *Вердикт: Edge не подтверждён — смотреть базу данных*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    root = Path(__file__).parent
    _load_env(root / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print(
            "[notify] TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы — "
            "уведомление пропущено.\n"
            "Добавьте в .env:\n"
            "  TELEGRAM_BOT_TOKEN=<токен>\n"
            "  TELEGRAM_CHAT_ID=<chat id>",
            file=sys.stderr,
        )
        return 0  # не ошибка — просто не настроено

    artifacts_dir = root / "artifacts" / "paper"
    report_path = _latest_report(artifacts_dir)

    if not report_path:
        send_message(token, chat_id,
            "⚠️ *Cerberus*: прогон завершён, но файл отчёта не найден.\n"
            f"Проверьте папку `{artifacts_dir}`")
        return 1

    # Загрузить отчёт
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        send_message(token, chat_id, f"⚠️ *Cerberus*: не удалось прочитать отчёт: {exc}")
        return 1

    # 1. Отправить сводку
    msg = _build_message(report)
    ok = send_message(token, chat_id, msg)
    if ok:
        print(f"[notify] ✅ Сообщение отправлено в Telegram (chat_id={chat_id})")
    else:
        print("[notify] ❌ Не удалось отправить сообщение", file=sys.stderr)

    # 2. Отправить JSON-файл
    ok_file = send_document(
        token, chat_id, report_path,
        caption=f"📄 Полный отчёт Cerberus ({report_path.name})"
    )
    if ok_file:
        print(f"[notify] ✅ JSON-отчёт отправлен: {report_path.name}")
    else:
        # Fallback: отправить как текст (первые 3800 символов)
        raw = report_path.read_text(encoding="utf-8")
        preview = raw[:3800] + ("\n..." if len(raw) > 3800 else "")
        send_message(token, chat_id, f"```json\n{preview}\n```")
        print("[notify] ✅ JSON отправлен как текст (fallback)")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
