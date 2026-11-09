#!/usr/bin/env bash
# Installe tsl-sync dans /opt/tsl-sync et programme un passage automatique chaque nuit.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "À lancer en root (sudo ./install.sh)"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
HOUR="${1:-4}"   # heure du passage quotidien (défaut : 4 h du matin)

install -d /opt/tsl-sync /var/lib/tsl-sync
install -m 755 "$SRC/tsl-sync.sh" /opt/tsl-sync/tsl-sync.sh
install -m 644 "$SRC/nuxt_levels.awk" /opt/tsl-sync/nuxt_levels.awk
if [ ! -f /etc/tsl-sync.conf ]; then
    install -m 600 "$SRC/tsl-sync.conf.example" /etc/tsl-sync.conf
    echo ">> Config créée : /etc/tsl-sync.conf — remplis NC_URL, NC_USER et NC_PASS."
fi

if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
    cat > /etc/systemd/system/tsl-sync.service <<UNIT
[Unit]
Description=Synchronisation des niveaux TSL vers Nextcloud
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/opt/tsl-sync/tsl-sync.sh
UNIT
    cat > /etc/systemd/system/tsl-sync.timer <<UNIT
[Unit]
Description=Synchronisation quotidienne des niveaux TSL

[Timer]
OnCalendar=*-*-* $(printf '%02d' "$HOUR"):17:00
RandomizedDelaySec=20min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
    systemctl daemon-reload
    systemctl enable --now tsl-sync.timer
    echo ">> Passage automatique programmé chaque jour vers ${HOUR} h (systemd : tsl-sync.timer)."
    echo "   Journal : journalctl -u tsl-sync"
else
    echo "17 $HOUR * * * root /opt/tsl-sync/tsl-sync.sh >> /var/log/tsl-sync.log 2>&1" > /etc/cron.d/tsl-sync
    echo ">> Passage automatique programmé chaque jour vers ${HOUR} h (cron). Journal : /var/log/tsl-sync.log"
fi
echo ">> Premier lancement manuel : /opt/tsl-sync/tsl-sync.sh"
