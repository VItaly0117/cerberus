#!/usr/bin/env bash
set -e

echo "=== Cerberus Paper Run Setup ==="

# ─────────────────────────────────────────────────────────────
# 1. Detect environment and install system deps
# ─────────────────────────────────────────────────────────────

# Termux (Android) — no sudo, uses pkg
if [ -n "$TERMUX_VERSION" ] || [ -d "/data/data/com.termux" ]; then
    echo "[ENV] Termux (Android) detected"
    pkg update -y -q
    pkg install -y python git curl screen 2>/dev/null || true
    if ! command -v python3 &>/dev/null && command -v python &>/dev/null; then
        ln -sf "$(command -v python)" "$PREFIX/bin/python3" 2>/dev/null || true
    fi
    SUDO=""

# Debian / Ubuntu / Raspberry Pi OS
elif command -v apt-get &>/dev/null; then
    echo "[ENV] apt-get detected (Debian/Ubuntu/Raspberry Pi)"
    SUDO="sudo"
    $SUDO apt-get update -qq
    $SUDO apt-get install -y python3 python3-pip python3-venv git curl

# Arch / Manjaro
elif command -v pacman &>/dev/null; then
    echo "[ENV] pacman detected (Arch)"
    sudo pacman -Sy --noconfirm python python-pip git curl
    SUDO="sudo"

else
    echo "[ERR] Неизвестный пакетный менеджер."
    echo "      Установите python3, pip, git, curl вручную и запустите скрипт снова."
    exit 1
fi

# ─────────────────────────────────────────────────────────────
# 2. Clone repo (or pull if already present)
# ─────────────────────────────────────────────────────────────

REPO_DIR="$HOME/project-cerberus"
if [ ! -d "$REPO_DIR" ]; then
    git clone https://github.com/VItaly0117/cerberus.git "$REPO_DIR"
else
    echo "[OK] Репозиторий уже есть — тяну последние изменения..."
    cd "$REPO_DIR" && git pull
fi

cd "$REPO_DIR"

# ─────────────────────────────────────────────────────────────
# 3. Virtual environment + dependencies
# ─────────────────────────────────────────────────────────────

if python3 -m venv .venv 2>/dev/null; then
    source .venv/bin/activate
else
    python3 -m ensurepip --upgrade 2>/dev/null || true
    python3 -m venv --without-pip .venv
    source .venv/bin/activate
    curl -sS https://bootstrap.pypa.io/get-pip.py | python3
fi

pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "[OK] Зависимости установлены"

# ─────────────────────────────────────────────────────────────
# 4. .env file
# ─────────────────────────────────────────────────────────────

if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
    else
        touch .env
    fi
    echo "[OK] Файл .env создан"
fi

# ─────────────────────────────────────────────────────────────
# 5. Telegram уведомления (опционально)
# ─────────────────────────────────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  📱 Настройка Telegram-уведомлений (необязательно)"
echo "     Когда run закончится — отчёт придёт прямо в Telegram."
echo ""
echo "  Как получить токен:"
echo "  1. Открыть @BotFather в Telegram → /newbot → скопировать токен"
echo "  2. Написать боту /start"
echo "  3. Узнать Chat ID: https://api.telegram.org/bot<ТОКЕН>/getUpdates"
echo "     (в ответе найти \"id\" внутри \"chat\")"
echo ""
echo "  Чтобы пропустить — нажать Enter"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

read -p "  Telegram Bot Token: " TG_TOKEN
if [ -n "$TG_TOKEN" ]; then
    read -p "  Telegram Chat ID:   " TG_CHAT_ID
    # Удалить старые записи и добавить новые
    grep -v "TELEGRAM_BOT_TOKEN\|TELEGRAM_CHAT_ID" .env > .env.tmp 2>/dev/null || true
    mv .env.tmp .env
    echo "TELEGRAM_BOT_TOKEN=$TG_TOKEN" >> .env
    echo "TELEGRAM_CHAT_ID=$TG_CHAT_ID" >> .env
    echo "[OK] Telegram настроен — отчёт придёт когда run завершится"
else
    echo "[--] Telegram пропущен"
fi

# ─────────────────────────────────────────────────────────────
# 6. Artifacts directories
# ─────────────────────────────────────────────────────────────

mkdir -p artifacts/paper artifacts/runtime

# ─────────────────────────────────────────────────────────────
# 7. Preflight check
# ─────────────────────────────────────────────────────────────

echo ""
echo "=== Preflight проверка ==="
python3 cerberustest.py --preflight

# ─────────────────────────────────────────────────────────────
# 8. Сколько часов гонять
# ─────────────────────────────────────────────────────────────

echo ""
read -p "Сколько часов гонять paper mode? [по умолчанию: 72] " HOURS
HOURS=${HOURS:-72}

# ─────────────────────────────────────────────────────────────
# 9. Собрать launch-скрипт
#    (отдельный файл чтобы screen/tmux не экранировал кавычки)
# ─────────────────────────────────────────────────────────────

LAUNCH_SCRIPT="$REPO_DIR/.launch.sh"
cat > "$LAUNCH_SCRIPT" << LAUNCH
#!/usr/bin/env bash
source "$REPO_DIR/.venv/bin/activate"
cd "$REPO_DIR"

echo "[launch] Запускаю paper run на $HOURS ч..."
python3 cerberustest.py --paper --duration-hours $HOURS 2>&1 | tee artifacts/paper/run.log
EXIT_CODE=\${PIPESTATUS[0]}

echo ""
echo "[launch] Paper run завершён (exit=\$EXIT_CODE). Отправляю уведомление..."
python3 "$REPO_DIR/notify.py" 2>&1 | tee -a artifacts/paper/run.log

echo "[launch] Готово. Отчёт: \$(ls -t $REPO_DIR/artifacts/paper/report_*.json 2>/dev/null | head -1)"
exit \$EXIT_CODE
LAUNCH
chmod +x "$LAUNCH_SCRIPT"

# ─────────────────────────────────────────────────────────────
# 10. Запустить в фоне (screen > tmux > установить screen)
# ─────────────────────────────────────────────────────────────

if command -v screen &>/dev/null; then
    screen -dmS cerberus bash "$LAUNCH_SCRIPT"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ✅ Cerberus запущен в фоне (screen)"
    echo ""
    echo "  Смотреть вживую  : screen -r cerberus"
    echo "  Отключиться      : Ctrl+A затем D"
    echo "  Только лог       : tail -f $REPO_DIR/artifacts/paper/run.log"
    echo "  Отчёт будет тут  : $REPO_DIR/artifacts/paper/"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

elif command -v tmux &>/dev/null; then
    tmux new-session -d -s cerberus "bash '$LAUNCH_SCRIPT'"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ✅ Cerberus запущен в фоне (tmux)"
    echo ""
    echo "  Смотреть вживую  : tmux attach -t cerberus"
    echo "  Отключиться      : Ctrl+B затем D"
    echo "  Только лог       : tail -f $REPO_DIR/artifacts/paper/run.log"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

else
    echo "[INFO] screen и tmux не найдены — устанавливаю screen..."
    if [ -n "$TERMUX_VERSION" ] || [ -d "/data/data/com.termux" ]; then
        pkg install -y screen
    elif command -v apt-get &>/dev/null; then
        ${SUDO:+$SUDO} apt-get install -y screen -qq
    fi
    screen -dmS cerberus bash "$LAUNCH_SCRIPT"
    echo ""
    echo "  ✅ Cerberus запущен в фоне (screen)"
    echo "  Смотреть: screen -r cerberus"
fi

echo ""
echo "=== Установка завершена ==="
