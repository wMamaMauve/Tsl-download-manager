#!/usr/bin/env bash
# Installs the tsl-sync Discord bot (Python + discord.py) and runs it as a service.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo ./install-bot.sh)"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

if ! command -v python3 >/dev/null || ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
    echo ">> Installing Python 3..."
    if command -v apt-get >/dev/null; then
        apt-get update -qq && apt-get install -y -qq python3 python3-venv
    elif command -v dnf >/dev/null; then
        dnf install -y python3
    else
        echo "Install python3 (with venv), then run this script again."; exit 1
    fi
fi

install -d /opt/tsl-sync
install -m 755 "$SRC/tsl_bot.py" /opt/tsl-sync/tsl_bot.py
install -m 755 "$SRC/extract_sprites.py" /opt/tsl-sync/extract_sprites.py
install -m 644 "$SRC/object_frames.tsv" /opt/tsl-sync/object_frames.tsv
install -m 644 "$SRC/object_names.tsv" /opt/tsl-sync/object_names.tsv
if [ ! -x /opt/tsl-sync/venv/bin/python ]; then
    python3 -m venv /opt/tsl-sync/venv
fi
/opt/tsl-sync/venv/bin/pip install -q --upgrade pip
/opt/tsl-sync/venv/bin/pip install -q "discord.py==2.5.2" pillow

cat > /etc/systemd/system/tsl-bot.service <<'UNIT'
[Unit]
Description=TSL Discord bot (announcements and commands)
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
systemctl enable tsl-bot >/dev/null 2>&1

if grep -qE '^DISCORD_BOT_TOKEN="?[^"]+' /etc/tsl-sync.conf 2>/dev/null; then
    systemctl restart tsl-bot
    echo ">> Bot started. Log: journalctl -u tsl-bot -f"
else
    echo ">> Bot installed. Add DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID to /etc/tsl-sync.conf,"
    echo "   then run: systemctl restart tsl-bot"
fi
