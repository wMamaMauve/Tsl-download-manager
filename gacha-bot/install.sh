#!/usr/bin/env bash
# Installs the Extreme Demon Gacha clan bot (Python + discord.py) and runs it as a service.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo ./install.sh)"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

if ! command -v python3 >/dev/null || ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
    echo ">> Installing Python 3..."
    if command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq python3 python3-venv
    elif command -v dnf >/dev/null; then dnf install -y python3
    else echo "Install python3 (with venv), then run this script again."; exit 1; fi
fi

install -d /opt/gacha-bot /var/lib/gacha-bot
install -m 755 "$SRC/gacha_bot.py" /opt/gacha-bot/gacha_bot.py
[ -x /opt/gacha-bot/venv/bin/python ] || python3 -m venv /opt/gacha-bot/venv
/opt/gacha-bot/venv/bin/pip install -q --upgrade pip
/opt/gacha-bot/venv/bin/pip install -q "discord.py==2.5.2"

if [ ! -f /etc/gacha-bot.conf ]; then
    install -m 600 "$SRC/gacha-bot.conf.example" /etc/gacha-bot.conf
    echo ">> Created /etc/gacha-bot.conf: fill in DISCORD_BOT_TOKEN, GACHA_USERNAME and GACHA_PASSWORD."
fi

cat > /etc/systemd/system/gacha-bot.service <<'UNIT'
[Unit]
Description=Extreme Demon Gacha clan bot (Discord)
Wants=network-online.target
After=network-online.target

[Service]
ExecStart=/opt/gacha-bot/venv/bin/python /opt/gacha-bot/gacha_bot.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable gacha-bot >/dev/null 2>&1
if grep -q '^DISCORD_BOT_TOKEN="..*"' /etc/gacha-bot.conf; then
    systemctl restart gacha-bot && echo ">> gacha-bot (re)started. Log: journalctl -u gacha-bot -f"
else
    echo ">> Fill in /etc/gacha-bot.conf, then: systemctl restart gacha-bot"
fi
