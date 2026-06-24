#!/bin/bash
# Быстрая выдача результатов после прогона Cerberus

set -e

DB_PATH="${DB_PATH:-cerberus.db}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🐺 Cerberus Paper Run — Результаты"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Проверка БД
if [ ! -f "$DB_PATH" ]; then
    echo "❌ БД не найдена: $DB_PATH"
    echo "Возможно, прогон ещё не начался или путь неверен."
    exit 1
fi

echo "📊 ОСНОВНЫЕ МЕТРИКИ"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

sqlite3 "$DB_PATH" <<'SQL'
SELECT
  COUNT(*) as total_signals,
  SUM(CASE WHEN result='SUCCESS' THEN 1 ELSE 0 END) as successful,
  SUM(CASE WHEN result='FILTERED' THEN 1 ELSE 0 END) as filtered,
  SUM(CASE WHEN result='BLOCKED_BY_RISK' THEN 1 ELSE 0 END) as blocked_risk,
  SUM(CASE WHEN result='CLEAN_MISS' THEN 1 ELSE 0 END) as clean_miss,
  SUM(CASE WHEN result='LEGGED_RISK' THEN 1 ELSE 0 END) as legged_risk
FROM paper_signals;
SQL

echo ""
echo "💰 P&L И EDGE"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

sqlite3 "$DB_PATH" <<'SQL'
SELECT
  ROUND(SUM(simulated_pnl), 4) as total_pnl_usdc,
  ROUND(AVG(edge_net_pct)*100, 2) as median_edge_pct,
  ROUND(MIN(edge_net_pct)*100, 2) as min_edge_pct,
  ROUND(MAX(edge_net_pct)*100, 2) as max_edge_pct
FROM paper_signals
WHERE result='SUCCESS' AND edge_net_pct IS NOT NULL;
SQL

echo ""
echo "📈 ТОП 10 РЫНКОВ ПО СИГНАЛАМ"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

sqlite3 "$DB_PATH" <<'SQL'
.mode column
.headers on
SELECT
  market_id,
  COUNT(*) as signals,
  SUM(CASE WHEN result='SUCCESS' THEN 1 ELSE 0 END) as success,
  ROUND(AVG(edge_net_pct)*100, 2) as avg_edge_pct,
  ROUND(SUM(simulated_pnl), 4) as total_pnl
FROM paper_signals
GROUP BY market_id
ORDER BY signals DESC
LIMIT 10;
SQL

echo ""
echo "📅 P&L ПО ДНЯМ"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

sqlite3 "$DB_PATH" <<'SQL'
.mode column
.headers on
SELECT
  DATE(recorded_at) as day,
  COUNT(*) as trades,
  SUM(CASE WHEN result='SUCCESS' THEN 1 ELSE 0 END) as successes,
  ROUND(SUM(simulated_pnl), 4) as daily_pnl
FROM paper_signals
WHERE result='SUCCESS'
GROUP BY DATE(recorded_at)
ORDER BY day;
SQL

echo ""
echo "⚡ УСПЕШНОСТЬ ТОРГОВЛИ"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

sqlite3 "$DB_PATH" <<'SQL'
SELECT
  ROUND(
    SUM(CASE WHEN result='SUCCESS' THEN 1 ELSE 0 END) * 100.0 /
    COUNT(*),
    1
  ) as success_rate_pct
FROM paper_signals
WHERE result IN ('SUCCESS', 'CLEAN_MISS', 'LEGGED_RISK');
SQL

echo ""
echo "📄 ПОЛНЫЙ ОТЧЁТ JSON"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

REPORT=$(find "$PROJECT_DIR/artifacts/paper" -name "report_*.json" -type f | sort -r | head -1)
if [ -n "$REPORT" ]; then
    echo "📍 $REPORT"
    echo ""
    cat "$REPORT" | python3 -m json.tool
else
    echo "⚠️  JSON отчёт не найден в artifacts/paper/"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Готово!"
