#!/usr/bin/env bash
set -e

echo "=== Cerberus Paper Run Setup ==="

# 1. Detect OS and install system deps
if command -v apt-get &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y python3 python3-pip python3-venv git curl
elif command -v pacman &>/dev/null; then
    sudo pacman -Sy --noconfirm python python-pip git curl
else
    echo "Unsupported package manager. Install python3, pip, git manually."
    exit 1
fi

# 2. Clone repo if not already cloned
REPO_DIR="$HOME/project-cerberus"
if [ ! -d "$REPO_DIR" ]; then
    git clone https://github.com/VItaly0117/project-cerberus.git "$REPO_DIR"
else
    echo "Repo already exists, pulling latest..."
    cd "$REPO_DIR" && git pull
fi

cd "$REPO_DIR"

# 3. Create virtualenv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 4. Copy .env if not exists
if [ ! -f .env ]; then
    cp .env.example .env
    echo "[OK] .env created from example (no credentials needed for paper mode)"
fi

# 5. Create artifacts dir
mkdir -p artifacts/paper artifacts/runtime

# 6. Run preflight
echo ""
echo "=== Running preflight check ==="
python3 cerberustest.py --preflight

# 7. Ask duration
echo ""
read -p "How many hours to run paper mode? [default: 72] " HOURS
HOURS=${HOURS:-72}

# 8. Start paper run inside screen session so it survives disconnect
if command -v screen &>/dev/null; then
    screen -dmS cerberus bash -c "
        source $REPO_DIR/.venv/bin/activate
        cd $REPO_DIR
        python3 cerberustest.py --paper --duration-hours $HOURS 2>&1 | tee artifacts/paper/run.log
    "
    echo ""
    echo "=== Cerberus is running in background ==="
    echo "To watch live:     screen -r cerberus"
    echo "To detach:         Ctrl+A then D"
    echo "To check log:      tail -f $REPO_DIR/artifacts/paper/run.log"
    echo "Report will be at: $REPO_DIR/artifacts/paper/"
elif command -v tmux &>/dev/null; then
    tmux new-session -d -s cerberus "
        source $REPO_DIR/.venv/bin/activate
        cd $REPO_DIR
        python3 cerberustest.py --paper --duration-hours $HOURS 2>&1 | tee artifacts/paper/run.log
    "
    echo ""
    echo "=== Cerberus is running in background ==="
    echo "To watch live:     tmux attach -t cerberus"
    echo "To detach:         Ctrl+B then D"
    echo "To check log:      tail -f $REPO_DIR/artifacts/paper/run.log"
else
    sudo apt-get install -y screen -qq
    screen -dmS cerberus bash -c "
        source $REPO_DIR/.venv/bin/activate
        cd $REPO_DIR
        python3 cerberustest.py --paper --duration-hours $HOURS 2>&1 | tee artifacts/paper/run.log
    "
    echo "To watch: screen -r cerberus"
fi

echo ""
echo "=== Setup complete ==="
