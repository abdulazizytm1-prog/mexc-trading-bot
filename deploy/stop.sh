#!/usr/bin/env bash
# =============================================================================
# deploy/stop.sh — Gracefully stop the MEXC trading bot screen session
# =============================================================================
set -euo pipefail

SESSION="mexc-bot"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }

if screen -ls 2>/dev/null | grep -qE "\.${SESSION}\b|${SESSION}\."; then
    screen -S "$SESSION" -X quit
    info "Bot stopped (screen session '$SESSION' terminated)."
    warn "Open positions are NOT closed by stopping the bot."
    warn "The exchange-side OCO orders (SL/TP) remain active."
else
    warn "No active session named '$SESSION' found."
    echo ""
    echo "  Running sessions:"
    screen -ls 2>/dev/null || echo "  (none)"
fi
