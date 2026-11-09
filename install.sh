#!/usr/bin/env bash
# Installs tsl-sync in /opt/tsl-sync and schedules an automatic run every night.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo ./install.sh)"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
HOUR="${1:-4}"   # hour of the daily run (default: 4 am)

install -d /opt/tsl-sync /var/lib/tsl-sync
install -m 755 "$SRC/tsl-sync.sh" /opt/tsl-sync/tsl-sync.sh
install -m 644 "$SRC/nuxt_levels.awk" /opt/tsl-sync/nuxt_levels.awk
if [ ! -f /etc/tsl-sync.conf ]; then
    install -m 600 "$SRC/tsl-sync.conf.example" /etc/tsl-sync.conf
    echo ">> Config created: /etc/tsl-sync.conf — fill in NC_URL, NC_USER and NC_PASS."
fi

if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
    cat > /etc/systemd/system/tsl-sync.service <<UNIT
[Unit]
Description=Sync of the TSL levels to Nextcloud
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/opt/tsl-sync/tsl-sync.sh
UNIT
    cat > /etc/systemd/system/tsl-sync.timer <<UNIT
[Unit]
Description=Daily sync of the TSL levels

[Timer]
OnCalendar=*-*-* $(printf '%02d' "$HOUR"):17:00
RandomizedDelaySec=20min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
    systemctl daemon-reload
    systemctl enable --now tsl-sync.timer
    echo ">> Automatic run scheduled every day around ${HOUR}:00 (systemd: tsl-sync.timer)."
    echo "   Log: journalctl -u tsl-sync"
else
    echo "17 $HOUR * * * root /opt/tsl-sync/tsl-sync.sh >> /var/log/tsl-sync.log 2>&1" > /etc/cron.d/tsl-sync
    echo ">> Automatic run scheduled every day around ${HOUR}:00 (cron). Log: /var/log/tsl-sync.log"
fi
if systemctl is-enabled --quiet tsl-bot 2>/dev/null; then
    install -m 755 "$SRC/tsl_bot.py" /opt/tsl-sync/tsl_bot.py
    systemctl restart tsl-bot && echo ">> Discord bot updated and restarted."
fi
echo ">> First manual run: systemctl start --no-block tsl-sync && journalctl -u tsl-sync -f"
