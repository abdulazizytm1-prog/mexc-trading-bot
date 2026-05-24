#!/usr/bin/env bash
# =============================================================================
# vps-setup.sh — MEXC Trading Bot + Monitor on Hetzner Ubuntu 22.04
# =============================================================================
# Run as root on a fresh VPS:
#   curl -fsSL https://raw.githubusercontent.com/your/repo/main/vps-setup.sh | bash
# Or after uploading:
#   chmod +x vps-setup.sh && sudo bash vps-setup.sh
# =============================================================================
set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# ── Config ────────────────────────────────────────────────────────────────────
BOT_DIR="/opt/mexc-trading-bot"
BOT_USER="botuser"
NODE_VERSION="20"
APP_PORT="3000"
NGINX_PORT="443"

# ── Require root ─────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Run as root: sudo bash vps-setup.sh"

# ── Step 1 — System packages ─────────────────────────────────────────────────
info "Updating system packages…"
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
  curl wget git unzip ufw logrotate \
  nginx certbot python3-certbot-nginx \
  build-essential

# ── Step 2 — Node.js 20 via NodeSource ───────────────────────────────────────
info "Installing Node.js ${NODE_VERSION}…"
if ! command -v node &>/dev/null || [[ "$(node -v | cut -d. -f1 | tr -d v)" -lt "$NODE_VERSION" ]]; then
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_VERSION}.x" | bash -
  apt-get install -y nodejs
fi
node -v; npm -v
info "Node.js $(node -v) installed"

# ── Step 3 — PM2 (process manager) ───────────────────────────────────────────
info "Installing PM2…"
npm install -g pm2 --quiet
pm2 --version

# ── Step 4 — Dedicated system user ───────────────────────────────────────────
info "Creating user '${BOT_USER}'…"
if ! id "$BOT_USER" &>/dev/null; then
  useradd -r -m -d "$BOT_DIR" -s /bin/bash "$BOT_USER"
fi

# ── Step 5 — Bot directory ────────────────────────────────────────────────────
info "Setting up ${BOT_DIR}…"
mkdir -p "$BOT_DIR"/{reports,logs}
chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"

# Copy files if running from source directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$SCRIPT_DIR" != "$BOT_DIR" ]]; then
  info "Copying bot files from ${SCRIPT_DIR} to ${BOT_DIR}…"
  for f in monitoring-server.js mexc_mcp_server.js mexc_mcp.js trading-dashboard.html \
            trade-journal.json package.json package-lock.json; do
    [[ -f "${SCRIPT_DIR}/${f}" ]] && cp "${SCRIPT_DIR}/${f}" "${BOT_DIR}/${f}"
  done
fi

# ── Step 6 — npm install ──────────────────────────────────────────────────────
info "Installing npm dependencies…"
cd "$BOT_DIR"
sudo -u "$BOT_USER" npm install --omit=dev --quiet

# ── Step 7 — .env file ────────────────────────────────────────────────────────
ENV_FILE="${BOT_DIR}/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  info "Creating .env template…"
  cat > "$ENV_FILE" <<'EOF'
# MEXC API credentials (Read + Trade permissions, NO Withdraw)
MEXC_API_KEY=your_api_key_here
MEXC_SECRET=your_api_secret_here

# Dashboard auth (change these!)
DASH_USER=admin
DASH_PASS=changeme123

# Server port (keep 3000; Nginx proxies 443 → 3000)
PORT=3000
EOF
  chown "$BOT_USER:$BOT_USER" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  warn ".env created at ${ENV_FILE} — EDIT IT before starting the bot"
else
  info ".env already exists — skipping"
fi

# ── Step 8 — Self-signed SSL cert (use Let's Encrypt if you have a domain) ───
SSL_DIR="/etc/ssl/mexc-monitor"
mkdir -p "$SSL_DIR"
if [[ ! -f "${SSL_DIR}/server.crt" ]]; then
  info "Generating self-signed TLS certificate…"
  openssl req -x509 -nodes -newkey rsa:2048 -days 730 \
    -keyout "${SSL_DIR}/server.key" \
    -out    "${SSL_DIR}/server.crt" \
    -subj   "/CN=$(curl -sf4 ifconfig.me || echo localhost)/O=MEXCBot/C=US" \
    2>/dev/null
  chmod 600 "${SSL_DIR}/server.key"
  info "Self-signed cert valid 2 years. Replace with Let's Encrypt for a real domain."
fi

# ── Step 9 — Nginx config ─────────────────────────────────────────────────────
info "Configuring Nginx…"
NGINX_CONF="/etc/nginx/sites-available/mexc-monitor"
cat > "$NGINX_CONF" <<EOF
server {
    listen 80;
    server_name _;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name _;

    ssl_certificate     ${SSL_DIR}/server.crt;
    ssl_certificate_key ${SSL_DIR}/server.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # Increase timeout for WebSocket keep-alive
    proxy_read_timeout  86400s;
    proxy_send_timeout  86400s;

    location / {
        proxy_pass         http://127.0.0.1:${APP_PORT};
        proxy_http_version 1.1;
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;
    }
}
EOF

ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/mexc-monitor
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx --quiet
systemctl restart nginx
info "Nginx configured"

# ── Step 10 — UFW firewall ───────────────────────────────────────────────────
info "Configuring UFW firewall…"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow ssh >/dev/null
ufw allow 80/tcp >/dev/null   # HTTP → redirects to HTTPS
ufw allow 443/tcp >/dev/null  # HTTPS
# Port 3000 is NOT exposed externally — Nginx proxies it
ufw --force enable >/dev/null
info "Firewall: SSH(22), HTTP(80), HTTPS(443) open. Port 3000 internal only."

# ── Step 11 — PM2 ecosystem config ───────────────────────────────────────────
info "Writing PM2 ecosystem config…"
cat > "${BOT_DIR}/ecosystem.config.cjs" <<'EOF'
module.exports = {
  apps: [
    {
      name:         "mexc-monitor",
      script:       "monitoring-server.js",
      cwd:          "/opt/mexc-trading-bot",
      interpreter:  "node",
      node_args:    "--experimental-vm-modules",
      env: { NODE_ENV: "production" },
      max_memory_restart: "300M",
      restart_delay:      5000,
      max_restarts:       20,
      log_file:           "/opt/mexc-trading-bot/logs/monitor.log",
      error_file:         "/opt/mexc-trading-bot/logs/monitor-error.log",
      time:               true,
    },
    {
      name:         "mexc-smc-mcp",
      script:       "mexc_mcp_server.js",
      cwd:          "/opt/mexc-trading-bot",
      interpreter:  "node",
      env: { NODE_ENV: "production" },
      max_memory_restart: "200M",
      restart_delay:      5000,
      max_restarts:       20,
      log_file:           "/opt/mexc-trading-bot/logs/mcp.log",
      error_file:         "/opt/mexc-trading-bot/logs/mcp-error.log",
      time:               true,
    },
  ],
};
EOF
chown "$BOT_USER:$BOT_USER" "${BOT_DIR}/ecosystem.config.cjs"

# ── Step 12 — Log rotation ────────────────────────────────────────────────────
info "Configuring log rotation…"
cat > /etc/logrotate.d/mexc-bot <<'EOF'
/opt/mexc-trading-bot/logs/*.log {
    daily
    rotate 30
    compress
    missingok
    notifempty
    sharedscripts
    postrotate
        pm2 reloadLogs >/dev/null 2>&1 || true
    endscript
}
EOF

# ── Step 13 — PM2 startup + launch ───────────────────────────────────────────
info "Starting services with PM2…"
cd "$BOT_DIR"
sudo -u "$BOT_USER" pm2 start ecosystem.config.cjs
sudo -u "$BOT_USER" pm2 save

# Generate and install PM2 startup script
startup_cmd=$(pm2 startup systemd -u "$BOT_USER" --hp "$BOT_DIR" | tail -1)
eval "$startup_cmd" 2>/dev/null || true

# ── Step 14 — Systemd service (alternative / fallback) ───────────────────────
cat > /etc/systemd/system/mexc-monitor.service <<EOF
[Unit]
Description=MEXC Trading Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${BOT_USER}
WorkingDirectory=${BOT_DIR}
ExecStart=/usr/bin/node ${BOT_DIR}/monitoring-server.js
Restart=on-failure
RestartSec=10
StandardOutput=append:${BOT_DIR}/logs/monitor.log
StandardError=append:${BOT_DIR}/logs/monitor-error.log
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

# ── Summary ───────────────────────────────────────────────────────────────────
VPS_IP=$(curl -sf4 ifconfig.me || echo "your-vps-ip")
echo ""
echo -e "${GREEN}══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✓  MEXC Trading Bot deployed successfully!${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Dashboard:  ${YELLOW}https://${VPS_IP}${NC}  (accept the self-signed cert)"
echo -e "  Login:      admin / changeme123  ${RED}← CHANGE IN .env!${NC}"
echo ""
echo -e "  IMPORTANT — edit your credentials first:"
echo -e "  ${YELLOW}nano ${ENV_FILE}${NC}"
echo -e "  Then restart: ${YELLOW}pm2 restart all${NC}"
echo ""
echo -e "  Useful commands:"
echo -e "  ${YELLOW}pm2 status${NC}               — process list"
echo -e "  ${YELLOW}pm2 logs mexc-monitor${NC}    — live dashboard logs"
echo -e "  ${YELLOW}pm2 logs mexc-smc-mcp${NC}    — live MCP server logs"
echo -e "  ${YELLOW}pm2 restart all${NC}          — restart everything"
echo -e "  ${YELLOW}ufw status${NC}               — firewall rules"
echo ""
warn "Replace the self-signed cert with Let's Encrypt once you point a domain:"
echo "  certbot --nginx -d yourdomain.com"
echo ""
