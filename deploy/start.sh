#!/usr/bin/env bash
# =============================================================================
# deploy/start.sh — Start the MEXC trading bot in a detached screen session
#
# Usage:  bash deploy/start.sh
# Attach: screen -r mexc-bot
# Detach: Ctrl+A, then D  (leaves bot running)
# =============================================================================
set -euo pipefail

BOT_DIR="/opt/mexc-trading-bot"
VENV="$BOT_DIR/venv"
SESSION="mexc-bot"
LOG="$BOT_DIR/claude_trader.log"
ENV_FILE="$BOT_DIR/.env"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[x]${NC} $*" >&2; exit 1; }

# ── Guards ────────────────────────────────────────────────────────────────────
[[ -f "$VENV/bin/python3" ]] || error "Virtual environment not found. Run: sudo bash deploy/setup.sh"
[[ -f "$ENV_FILE"         ]] || error ".env not found at $ENV_FILE"

# Warn if credentials look unfilled
if grep -qE '^\s*(MEXC_API_KEY|MEXC_SECRET)\s*=$' "$ENV_FILE" 2>/dev/null; then
    warn "MEXC credentials appear empty in $ENV_FILE"
    warn "The bot will start but cannot trade. Edit and restart."
fi

# ── Kill existing session if running ─────────────────────────────────────────
if screen -ls 2>/dev/null | grep -q "\.${SESSION}\b\|${SESSION}\."; then
    warn "Session '$SESSION' already running — restarting..."
    screen -S "$SESSION" -X quit 2>/dev/null || true
    sleep 1
fi

# ── Launch ────────────────────────────────────────────────────────────────────
info "Starting bot in screen session '$SESSION'..."
cd "$BOT_DIR"

screen -dmS "$SESSION" \
    "$VENV/bin/python3" -u "$BOT_DIR/claude_trader.py"
# python-dotenv loads .env automatically; no need to source it here.
# -u disables stdout buffering so logs appear immediately.

sleep 2

# ── Confirm ───────────────────────────────────────────────────────────────────
if screen -ls 2>/dev/null | grep -qE "\.${SESSION}\b|${SESSION}\."; then
    info "Bot is running."
    echo ""
    echo -e "  ${YELLOW}Attach to session :${NC}  screen -r $SESSION"
    echo -e "  ${YELLOW}Detach (keep bot)  :${NC}  Ctrl+A, then D"
    echo -e "  ${YELLOW}Live logs          :${NC}  tail -f $LOG"
    echo -e "  ${YELLOW}Stop               :${NC}  bash deploy/stop.sh"
    echo ""
else
    error "Screen session failed to start. Check: tail -50 $LOG"
fi
