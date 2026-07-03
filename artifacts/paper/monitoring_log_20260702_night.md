# Ночной мониторинг paper run — режим наблюдения (без вмешательства в код)

PID 83684, `--duration-hours 72`, запущен 2026-07-02 ~22:00 EEST с исправленным
кодом (fee formula + edge instrumentation). Только фиксация, никаких
структурных изменений сегодня — согласно решению пользователя.

## 00:37 EEST (2026-07-03)

- Процесс жив (PID 83684).
- evaluated=732 (растёт нормально, ~700 за ~2.5ч работы).
- result breakdown: FILTERED=771 (100%). Ноль SUCCESS/CLEAN_MISS/LEGGED_RISK/
  REPAIRED/BLOCKED_BY_RISK пока.
- rejection_reason: hybrid_maker_edge_below_threshold_fok_edge_below_threshold=616,
  hybrid_maker_invalid_limit_price_fok_min_levels_gate=155.
- hourly_attempt_cap: 0 вхождений — не доминирует, как и раньше.
- Отслеживается 5 уникальных рынков (как и ожидалось при текущих фильтрах).
- Инцидентов (падений, рестартов, ошибок) нет.

## 2026-07-03 00:41 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 791
- result breakdown: {'FILTERED': 791}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 5

## 2026-07-03 00:42 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 796
- result breakdown: {'FILTERED': 796}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 5

## 2026-07-03 01:17 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 933
- result breakdown: {'FILTERED': 933}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 5

## 2026-07-03 02:33 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 968
- result breakdown: {'FILTERED': 968}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 5

## 2026-07-03 03:20 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 973
- result breakdown: {'FILTERED': 973}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 5

## 2026-07-03 04:17 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 978
- result breakdown: {'FILTERED': 978}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 5

## 2026-07-03 05:23 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 980
- result breakdown: {'FILTERED': 980}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 5

## 2026-07-03 06:17 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 986
- result breakdown: {'FILTERED': 986}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 5

## 2026-07-03 07:23 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 986
- result breakdown: {'FILTERED': 986}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 5

## 2026-07-03 08:20 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 991
- result breakdown: {'FILTERED': 991}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 6

## 2026-07-03 09:21 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 1001
- result breakdown: {'FILTERED': 1001}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 6

## 2026-07-03 10:20 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 1010
- result breakdown: {'FILTERED': 1010}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 6

## 2026-07-03 11:21 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 1015
- result breakdown: {'FILTERED': 1015}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 6

## 2026-07-03 12:22 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 1027
- result breakdown: {'FILTERED': 1027}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 7

## 2026-07-03 13:17 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 1057
- result breakdown: {'FILTERED': 1057}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 7

## 2026-07-03 14:17 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 1411
- result breakdown: {'FILTERED': 1411}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 7

## 2026-07-03 15:17 EEST

- Процесс жив (PID 83684).
- Всего paper_signals: 1752
- result breakdown: {'FILTERED': 1752}
- hourly_attempt_cap упоминаний: 0
- Уникальных рынков: 7

## 13:20 EEST — вмешательство (по запросу пользователя)

Обнаружена причина замедления с ~01:00 EEST: 93 события сна Mac (pmset -g log)
с полуночи, каждое 8-17 минут — WebSocket рвётся, требует времени на
переподключение. Пример: провал 01:24→02:33 (69 минут почти без активности)
после `Watcher: error — no close frame received or sent`.

Фикс: `caffeinate -d -i -m -s -w 83684` запущен в фоне (PID 96048), держит
4 assertion'а (idle/display/system/disk sleep), привязан к PID прогона —
автоматически освободится, когда прогон завершится. `pmset -b sleep 0`
(системный, более надёжный вариант) не применён — требует sudo, не был
доступен без пароля пользователя.

Ограничение: принудительный сон при закрытой крышке без внешнего монитора
не блокируется caffeinate — это аппаратное поведение macOS.
