#!/usr/bin/env bash
# =============================================================================
# deploy/status.sh — Show bot run status and tail recent logs
# =============================================================================

BOT_DIR="/opt/mexc-trading-bot"
SESSION="mexc-bot"
LOG="$BOT_DIR/claude_trader.log"
ERR_LOG="$BOT_DIR/error.log"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

echo ""
echo -e "${GREEN}=== Screen session ===${NC}"
if screen -ls 2>/dev/null | grep -qE "\.${SESSION}\b|${SESSION}\."; then
    echo -e "  ${GREEN}RUNNING${NC}  (session: $SESSION)"
    screen -ls 2>/dev/null | grep -E "$SESSION" || true
else
    echo -e "  ${RED}NOT RUNNING${NC}"
    echo "  Start with: bash deploy/start.sh"
fi

echo ""
echo -e "${GREEN}=== Disk / memory ===${NC}"
df -h "$BOT_DIR" 2>/dev/null | tail -1 | awk '{printf "  Disk: %s used of %s (%s)\n",$3,$2,$5}'
free -h | awk '/^Mem:/{printf "  RAM:  %s used of %s\n",$3,$2}'

echo ""
echo -e "${GREEN}=== Last 50 lines: claude_trader.log ===${NC}"
if [[ -f "$LOG" ]]; then
    tail -50 "$LOG"
else
    echo "  (log file not found: $LOG)"
fi

if [[ -f "$ERR_LOG" ]] && [[ -s "$ERR_LOG" ]]; then
    echo ""
    echo -e "${YELLOW}=== Last 20 lines: error.log ===${NC}"
    tail -20 "$ERR_LOG"
fi

echo ""
