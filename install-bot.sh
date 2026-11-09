#!/usr/bin/env bash
# Installe le bot Discord de tsl-sync (Python + discord.py) et le lance en service.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "À lancer en root (sudo ./install-bot.sh)"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

if ! command -v python3 >/dev/null || ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
    echo ">> Installation de Python 3..."
    if command -v apt-get >/dev/null; then
        apt-get update -qq && apt-get install -y -qq python3 python3-venv
    elif command -v dnf >/dev/null; then
        dnf install -y python3
    else
        echo "Installe python3 (avec venv) puis relance ce script."; exit 1
    fi
fi

install -d /opt/tsl-sync
install -m 755 "$SRC/tsl_bot.py" /opt/tsl-sync/tsl_bot.py
if [ ! -x /opt/tsl-sync/venv/bin/python ]; then
    python3 -m venv /opt/tsl-sync/venv
fi
/opt/tsl-sync/venv/bin/pip install -q --upgrade pip
/opt/tsl-sync/venv/bin/pip install -q "discord.py==2.5.2"

cat > /etc/systemd/system/tsl-bot.service <<'UNIT'
[Unit]
Description=Bot Discord TSL (annonces et commandes)
Wants=network-online.target
After=network-online.target

[Service]
ExecStart=/opt/tsl-sync/venv/bin/python /opt/tsl-sync/tsl_bot.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload

if grep -qE '^DISCORD_BOT_TOKEN="?[^"]+' /etc/tsl-sync.conf 2>/dev/null; then
    systemctl enable tsl-bot >/dev/null 2>&1
    systemctl restart tsl-bot
    echo ">> Bot démarré. Journal : journalctl -u tsl-bot -f"
else
    systemctl enable tsl-bot >/dev/null 2>&1
    echo ">> Bot installé. Ajoute DISCORD_BOT_TOKEN et DISCORD_CHANNEL_ID dans /etc/tsl-sync.conf,"
    echo "   puis lance : systemctl restart tsl-bot"
fi
