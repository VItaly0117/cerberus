# 🐺 Cerberus — Polymarket Paper Trader

Cerberus автоматически мониторит рынки [Polymarket](https://polymarket.com) и ищет арбитраж между ценами YES и NO токенов одного и того же события. Все сделки **симулируются** — реальные деньги не тратятся. По окончании прогона присылает отчёт в **Telegram**.

> **Принцип**: цена YES + цена NO должна быть = 1.00 USDC. Если сумма < 0.97 — можно купить оба токена и зафиксировать прибыль. Cerberus ловит такие моменты.

---

## ⚡ Запустить одной командой

### Linux (Ubuntu / Debian / Raspberry Pi)
```bash
curl -fsSL https://raw.githubusercontent.com/VItaly0117/cerberus/main/install_and_run.sh | bash
```

### Android (Termux)
```bash
pkg install curl -y
curl -fsSL https://raw.githubusercontent.com/VItaly0117/cerberus/main/install_and_run.sh | bash
```

Скрипт сам установит Python, зависимости, запустит в фоне через `screen` и настроит Telegram-уведомление.

---

## 📱 Настройка Telegram-уведомлений

Когда 72-часовой прогон закончится, Cerberus пришлёт отчёт прямо в Telegram. Настройка занимает 2 минуты:

### Шаг 1 — Создать бота
1. Открыть [@BotFather](https://t.me/BotFather) в Telegram
2. Написать `/newbot`
3. Придумать имя и username для бота
4. Скопировать **токен** — выглядит так: `7412345678:AAFxyz...`

### Шаг 2 — Узнать свой Chat ID
1. Написать боту любое сообщение (например `/start`)
2. Открыть в браузере (вставить свой токен):
   ```
   https://api.telegram.org/bot<ТВОЙ_ТОКЕН>/getUpdates
   ```
3. В ответе найти `"chat":{"id":123456789}` — это твой **Chat ID**

### Шаг 3 — Добавить в `.env`
```bash
# В папке проекта:
echo "TELEGRAM_BOT_TOKEN=7412345678:AAFxyz..." >> .env
echo "TELEGRAM_CHAT_ID=123456789" >> .env
```

> 💡 Скрипт `install_and_run.sh` спросит токен и chat_id сам — можно ввести прямо во время установки.

### Что придёт в Telegram

```
🐺 Cerberus Paper Run — Завершён
━━━━━━━━━━━━━━━━━━
⏱ Время: 72.0ч
📊 Снапшотов оценено: 48 320
📡 Viable сигналов: 41
✅ Успешных: 27
💨 Clean miss: 12
⚡ Legged risk: 2
🚫 Заблокировано: 1 840
━━━━━━━━━━━━━━━━━━
🟢 Simulated P&L: +8.4700 USDC
📈 Медиана edge: 2.31%
📚 Stale книга: 1.8%
━━━━━━━━━━━━━━━━━━
✅ Stop conditions: нет

🏆 Вердикт: Стратегия работает — запускать live
```

---

## 📊 Как читать результаты

### Ключевые метрики

| Метрика | Что означает | Хорошо | Плохо |
|---------|-------------|--------|-------|
| `viable_signals` | Сколько раз нашёлся арбитраж | > 30 за 72ч | < 5 за 72ч |
| `median_edge_net_pct` | Медианный чистый профит | > 2% | ≤ 0% |
| `total_simulated_pnl` | Итоговый P&L в USDC | > $5 | < 0 |
| `legged_incident_rate_pct` | % сделок где 2-я нога не купилась | < 5% | > 15% |
| `stale_book_rate_pct` | % устаревших данных книги | < 5% | > 10% |
| `successes / viable` | Процент успешных сделок | > 60% | < 40% |

### Три сценария

**✅ Стратегия работает**
- viable_signals > 30 за 72ч
- median_edge > 2%
- P&L > $5 на $25 нотионал (= >7% за 3 дня)

→ Запускать live с реальным капиталом $100–500.

**⚠️ Работает слабо**
- viable_signals 5–30
- median_edge 1–2%
- P&L около нуля

→ Оптимизировать параметры или искать нишевые рынки.

**❌ Не работает**
- viable_signals < 5 за 72ч
- median_edge ≤ 0

→ Рынок эффективен в этом сегменте — менять стратегию.

### Как вытащить подробные данные из базы

```bash
cd ~/project-cerberus

# Топ рынков по сигналам
sqlite3 cerberus.db "
SELECT market_id, COUNT(*) as n, ROUND(AVG(edge_net_pct)*100,2) as avg_edge_pct
FROM paper_signals WHERE result='SUCCESS'
GROUP BY market_id ORDER BY n DESC LIMIT 10;"

# P&L по дням
sqlite3 cerberus.db "
SELECT DATE(recorded_at) as day, SUM(simulated_pnl) as daily_pnl, COUNT(*) as trades
FROM paper_signals WHERE result='SUCCESS'
GROUP BY day;"

# Распределение edge (гистограмма)
sqlite3 cerberus.db "
SELECT ROUND(edge_net_pct*100,1) as edge_pct, COUNT(*) as count
FROM paper_signals WHERE edge_net_pct IS NOT NULL
GROUP BY edge_pct ORDER BY edge_pct;"
```

---

## ⚙️ Конфигурация

Все параметры задаются через переменные окружения в `.env`:

| Переменная | По умолчанию | Описание |
|------------|-------------|----------|
| `TRADE_NOTIONAL_USDC` | `25` | Размер ставки на каждую пару (USDC) |
| `MIN_NET_EDGE_PCT` | `0.0125` | Минимальный чистый edge для входа (1.25%) |
| `DB_PATH` | `cerberus.db` | Путь к базе данных |
| `GAMMA_HOST` | `gamma-api.polymarket.com` | API для списка рынков |
| `CLOB_REST_URL` | `https://clob.polymarket.com` | REST API ордербука |
| `WS_URL` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | WebSocket поток |
| `ALLOW_LIVE_MODE` | `false` | ⛔ Всегда `false` — реальные сделки не поддерживаются |
| `TELEGRAM_BOT_TOKEN` | — | Токен Telegram-бота для уведомлений |
| `TELEGRAM_CHAT_ID` | — | Ваш Telegram Chat ID |

---

## 🔍 Как работает изнутри

```
Polymarket Gamma API
        │
        ▼
  MarketDiscovery          ← сканирует список рынков каждые 5 минут
  (market_discovery.py)       фильтрует: объём, дата, бинарность
        │
        ▼ candidate_queue
      Watcher              ← подписывается на WebSocket ордербуков
   (watcher.py)               обновляет L2 книгу в реальном времени
        │
        ▼ opportunity_queue
      RiskManager          ← проверяет: kill switch, cooldown, лимиты
    (risk.py)
        │ (разрешено)
        ▼
        Core               ← считает edge = 1 - YES_ask - NO_ask - fees - slippage
      (core.py)
        │ (edge > порога)
        ▼
      Executor             ← симулирует FOK на YES, потом FOK на NO
   (executor.py)              при неудаче — аварийный откат
        │
        ▼
      Storage              ← записывает каждый сигнал в SQLite
   (storage.py)
        │
        ▼
    notify.py              ← по окончании → отчёт в Telegram
```

### Почему нет реальных сделок

В текущей версии `allow_live_mode = False` **принудительно** — это не настройка, а защитный код. Если кто-то поставит `ALLOW_LIVE_MODE=true`, скрипт завершится с ошибкой (`exit 1`). Реальный live-режим требует отдельного аудита и интеграции с CLOB API.

---

## 🧪 Тесты

```bash
cd ~/project-cerberus
source .venv/bin/activate
pytest tests/ -v --tb=short
# Ожидается: 106 passed, 1 skipped
```

---

## 🔧 Наблюдать за процессом

```bash
# Подключиться к фоновой сессии
screen -r cerberus

# Только лог (не подключаясь к screen)
tail -f ~/project-cerberus/artifacts/paper/run.log

# Отключиться от screen без остановки
# Ctrl+A, затем D

# Посмотреть текущую статистику
sqlite3 ~/project-cerberus/cerberus.db "
SELECT result, COUNT(*) FROM paper_signals GROUP BY result;"
```

---

## ❓ FAQ

**Q: Нужны ли реальные деньги или аккаунт Polymarket?**
A: Нет. Cerberus читает публичные данные без авторизации. Никаких аккаунтов, никаких ключей API.

**Q: Что такое "legged risk"?**
A: Когда купился YES-токен, но NO-токен не успели купить (цена ушла). В этот момент позиция незакрыта и есть риск убытка. Executor сразу же продаёт YES обратно по рынку чтобы ограничить потери.

**Q: Почему "FILTERED" записей много?**
A: Cerberus оценивает каждый снапшот ордербука. Большинство не проходят по минимальному edge — это нормально. Важно соотношение viable_signals / total_snapshots.

**Q: Можно запустить на VPS или домашнем роутере?**
A: Да. Любой Linux с Python 3.9+. На слабом железе (Raspberry Pi Zero) может быть задержка WebSocket — в этом случае увеличить `max_book_age_ms` в `.env`.

**Q: Что делать если preflight падает на websockets?**
A: `pip install websockets` внутри venv (`source .venv/bin/activate && pip install websockets`).
