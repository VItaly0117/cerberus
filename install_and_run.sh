#!/usr/bin/env bash
set -e

echo "=== Cerberus Paper Run Setup ==="

# ─────────────────────────────────────────────────────────────
# 1. Detect environment and install system deps
# ─────────────────────────────────────────────────────────────

# Termux (Android) — no sudo, uses pkg / apt without root
if [ -n "$TERMUX_VERSION" ] || [ -d "/data/data/com.termux" ]; then
    echo "[ENV] Termux (Android) detected"
    pkg update -y -q
    pkg install -y python git curl screen 2>/dev/null || true
    # Termux calls it 'python', make sure python3 resolves
    if ! command -v python3 &>/dev/null && command -v python &>/dev/null; then
        ln -sf "$(command -v python)" "$PREFIX/bin/python3" 2>/dev/null || true
    fi
    SUDO=""
    PKG_INSTALL="pkg install -y"

# Debian / Ubuntu / Raspberry Pi OS — standard apt-get + sudo
elif command -v apt-get &>/dev/null; then
    echo "[ENV] apt-get detected (Debian/Ubuntu/Raspberry Pi)"
    SUDO="sudo"
    $SUDO apt-get update -qq
    $SUDO apt-get install -y python3 python3-pip python3-venv git curl
    PKG_INSTALL="$SUDO apt-get install -y"

# Arch / Manjaro
elif command -v pacman &>/dev/null; then
    echo "[ENV] pacman detected (Arch/Manjaro)"
    sudo pacman -Sy --noconfirm python python-pip git curl
    SUDO="sudo"
    PKG_INSTALL="sudo pacman -S --noconfirm"

else
    echo "[ERR] Unsupported package manager."
    echo "      Please install python3, pip, git manually, then re-run."
    exit 1
fi

# ─────────────────────────────────────────────────────────────
# 2. Clone repo (or pull if already present)
# ─────────────────────────────────────────────────────────────

REPO_DIR="$HOME/project-cerberus"
if [ ! -d "$REPO_DIR" ]; then
    git clone https://github.com/VItaly0117/cerberus.git "$REPO_DIR"
else
    echo "[OK] Repo already exists, pulling latest..."
    cd "$REPO_DIR" && git pull
fi

cd "$REPO_DIR"

# ─────────────────────────────────────────────────────────────
# 3. Virtual environment + dependencies
# ─────────────────────────────────────────────────────────────

# python3-venv may not be installed in minimal Termux builds; try anyway
if python3 -m venv .venv 2>/dev/null; then
    source .venv/bin/activate
else
    # Termux: venv sometimes needs ensurepip
    python3 -m ensurepip --upgrade 2>/dev/null || true
    python3 -m venv --without-pip .venv
    source .venv/bin/activate
    curl -sS https://bootstrap.pypa.io/get-pip.py | python3
fi

pip install --upgrade pip -q
pip install -r requirements.txt -q

# ─────────────────────────────────────────────────────────────
# 4. .env file
# ─────────────────────────────────────────────────────────────

if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "[OK] .env created from example (no credentials needed for paper mode)"
    else
        touch .env
        echo "[OK] Empty .env created (paper mode uses safe defaults)"
    fi
fi

# ─────────────────────────────────────────────────────────────
# 5. Artifacts directories
# ─────────────────────────────────────────────────────────────

mkdir -p artifacts/paper artifacts/runtime

# ─────────────────────────────────────────────────────────────
# 6. Preflight check
# ─────────────────────────────────────────────────────────────

echo ""
echo "=== Running preflight check ==="
python3 cerberustest.py --preflight

# ─────────────────────────────────────────────────────────────
# 7. Ask how long to run
# ─────────────────────────────────────────────────────────────

echo ""
read -p "How many hours to run paper mode? [default: 72] " HOURS
HOURS=${HOURS:-72}

# ─────────────────────────────────────────────────────────────
# 8. Launch in a background session (screen > tmux > fallback)
# ─────────────────────────────────────────────────────────────

LAUNCH_CMD="source $REPO_DIR/.venv/bin/activate && cd $REPO_DIR && python3 cerberustest.py --paper --duration-hours $HOURS 2>&1 | tee artifacts/paper/run.log"

if command -v screen &>/dev/null; then
    screen -dmS cerberus bash -c "$LAUNCH_CMD"
    echo ""
    echo "=== Cerberus is running in background (screen) ==="
    echo "  Watch live : screen -r cerberus"
    echo "  Detach     : Ctrl+A then D"
    echo "  Log only   : tail -f $REPO_DIR/artifacts/paper/run.log"
    echo "  Report dir : $REPO_DIR/artifacts/paper/"

elif command -v tmux &>/dev/null; then
    tmux new-session -d -s cerberus "bash -c \"$LAUNCH_CMD\""
    echo ""
    echo "=== Cerberus is running in background (tmux) ==="
    echo "  Watch live : tmux attach -t cerberus"
    echo "  Detach     : Ctrl+B then D"
    echo "  Log only   : tail -f $REPO_DIR/artifacts/paper/run.log"
    echo "  Report dir : $REPO_DIR/artifacts/paper/"

else
    # Last resort: install screen and retry
    echo "[INFO] Neither screen nor tmux found — installing screen..."
    if [ -n "$TERMUX_VERSION" ] || [ -d "/data/data/com.termux" ]; then
        pkg install -y screen
    elif command -v apt-get &>/dev/null; then
        $SUDO apt-get install -y screen -qq
    fi
    screen -dmS cerberus bash -c "$LAUNCH_CMD"
    echo ""
    echo "=== Cerberus is running in background (screen) ==="
    echo "  Watch live : screen -r cerberus"
    echo "  Detach     : Ctrl+A then D"
    echo "  Log only   : tail -f $REPO_DIR/artifacts/paper/run.log"
fi

echo ""
echo "=== Setup complete ==="
