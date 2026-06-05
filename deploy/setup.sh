#!/usr/bin/env bash
# =============================================================================
# deploy/setup.sh — MEXC Trading Bot Python environment setup
#
# Tested on: Ubuntu 22.04 LTS (Hetzner CX22 / CAX11)
# Run as root:  sudo bash deploy/setup.sh
# =============================================================================
set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[x]${NC} $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root:  sudo bash deploy/setup.sh"

# ── Config ────────────────────────────────────────────────────────────────────
BOT_DIR="/opt/mexc-trading-bot"
BOT_USER="botuser"
VENV="$BOT_DIR/venv"

# ── Step 1 — System packages ──────────────────────────────────────────────────
info "Updating system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    screen curl wget git ufw \
    build-essential libssl-dev

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python ${PY_VER} detected"

# ── Step 2 — Dedicated system user ───────────────────────────────────────────
if ! id "$BOT_USER" &>/dev/null; then
    info "Creating user '$BOT_USER'..."
    useradd -r -m -d "$BOT_DIR" -s /bin/bash "$BOT_USER"
else
    info "User '$BOT_USER' already exists"
fi

# ── Step 3 — Bot directory ────────────────────────────────────────────────────
info "Setting up $BOT_DIR..."
mkdir -p "$BOT_DIR"/{data,reports}

# Copy project files from the directory containing this script's parent
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$SCRIPT_DIR" != "$BOT_DIR" ]]; then
    info "Syncing files from $SCRIPT_DIR → $BOT_DIR..."
    rsync -a --delete \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='*.log' \
        --exclude='.env' \
        --exclude='node_modules' \
        --exclude='venv' \
        --exclude='data/*.csv' \
        "$SCRIPT_DIR/" "$BOT_DIR/"
fi

chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"

# ── Step 4 — Python virtual environment ──────────────────────────────────────
if [[ ! -d "$VENV" ]]; then
    info "Creating virtual environment at $VENV..."
    python3 -m venv "$VENV"
fi

info "Installing Python dependencies..."
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install -r "$BOT_DIR/requirements.txt" --quiet
info "Dependencies installed"

# ── Step 5 — .env setup ───────────────────────────────────────────────────────
ENV_FILE="$BOT_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$BOT_DIR/.env.example" "$ENV_FILE"
    chown "$BOT_USER:$BOT_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    warn ".env created at $ENV_FILE"
    warn ">>> EDIT IT NOW: nano $ENV_FILE <<<"
else
    info ".env already exists — skipping (not overwritten)"
fi

# ── Step 6 — Log rotation ─────────────────────────────────────────────────────
info "Configuring log rotation..."
cat > /etc/logrotate.d/mexc-bot <<'EOF'
/opt/mexc-trading-bot/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
EOF

# ── Step 7 — Firewall (SSH only — bot needs no inbound ports) ─────────────────
info "Configuring UFW firewall..."
ufw --force reset      >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow ssh          >/dev/null
ufw --force enable     >/dev/null
info "Firewall: SSH(22) open. All other inbound blocked."

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}=============================================${NC}"
echo -e "${GREEN}  Setup complete!${NC}"
echo -e "${GREEN}=============================================${NC}"
echo ""
echo -e "  Next steps:"
echo -e "  ${YELLOW}1.${NC} nano $ENV_FILE"
echo -e "     Fill in MEXC_API_KEY, MEXC_SECRET,"
echo -e "     COINRANKING_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
echo ""
echo -e "  ${YELLOW}2.${NC} bash $BOT_DIR/deploy/start.sh"
echo -e "     (starts the bot in a screen session)"
echo ""
echo -e "  ${YELLOW}3.${NC} bash $BOT_DIR/deploy/status.sh"
echo -e "     (check it's running + tail logs)"
echo ""
