// PM2 ecosystem config
// Usage on VPS:
//   pm2 start deploy/ecosystem.config.cjs
//   pm2 save   (persist across reboots)
//   pm2 status
//   pm2 logs mexc-monitor

module.exports = {
  apps: [
    {
      // ── Trading Dashboard ─────────────────────────────────────────────────
      name:        "mexc-monitor",
      script:      "monitoring-server.js",
      cwd:         "/opt/mexc-trading-bot",
      interpreter: "node",
      env: {
        NODE_ENV: "production",
        PORT:     "3000",
      },

      // Memory guard — restart if RSS exceeds 300 MB
      max_memory_restart: "300M",

      // Restart behaviour
      restart_delay: 5000,   // 5 s cool-down before restart
      max_restarts:  20,     // give up after 20 crashes in the window below
      min_uptime:    "30s",  // must stay alive 30 s to count as "started"

      // Logs
      log_file:    "/opt/mexc-trading-bot/logs/monitor.log",
      error_file:  "/opt/mexc-trading-bot/logs/monitor-error.log",
      merge_logs:  true,
      time:        true,     // prefix each log line with timestamp

      // Watch (disabled in prod — use pm2 restart instead of hot-reload)
      watch: false,
    },

    {
      // ── SMC MCP Server (coin selector + signal engine) ────────────────────
      name:        "mexc-smc-mcp",
      script:      "mexc_mcp_server.js",
      cwd:         "/opt/mexc-trading-bot",
      interpreter: "node",
      env: {
        NODE_ENV: "production",
      },
      max_memory_restart: "200M",
      restart_delay:      5000,
      max_restarts:       20,
      min_uptime:         "30s",
      log_file:    "/opt/mexc-trading-bot/logs/mcp.log",
      error_file:  "/opt/mexc-trading-bot/logs/mcp-error.log",
      merge_logs:  true,
      time:        true,
      watch:       false,
    },
  ],
};
