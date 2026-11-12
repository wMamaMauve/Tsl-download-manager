#!/usr/bin/env bash
# Installs the Record Checker (a web page for list staff) as a service.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo ./install.sh)"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
VENV=/opt/tsl-sync/venv

command -v ffmpeg >/dev/null || { echo ">> Installing ffmpeg..."; apt-get update -qq && apt-get install -y -qq ffmpeg; }
if ! command -v python3 >/dev/null || ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
    apt-get update -qq && apt-get install -y -qq python3 python3-venv
fi
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q --upgrade aiohttp yt-dlp numpy

install -d /opt/record-checker/static /opt/record-checker/backgrounds /var/lib/record-checker
install -m 755 "$SRC/checker.py" /opt/record-checker/checker.py
install -m 755 "$SRC/analyze.py" /opt/record-checker/analyze.py
install -m 644 "$SRC/static/index.html" /opt/record-checker/static/index.html
install -m 644 "$SRC"/backgrounds/*.png /opt/record-checker/backgrounds/

if [ ! -f /etc/record-checker.conf ]; then
    install -m 600 "$SRC/record-checker.conf.example" /etc/record-checker.conf
    PW="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 16)"
    sed -i "s/^CHECKER_PASSWORD=\"\"/CHECKER_PASSWORD=\"$PW\"/" /etc/record-checker.conf
    echo ">> Created /etc/record-checker.conf. Password of the page: $PW"
fi

cat > /etc/systemd/system/record-checker.service <<UNIT
[Unit]
Description=Record Checker (web page for TSL list staff)
Wants=network-online.target
After=network-online.target

[Service]
# YouTube changes often: keep yt-dlp up to date at each start
ExecStartPre=-$VENV/bin/pip install -q --upgrade yt-dlp
ExecStart=$VENV/bin/python /opt/record-checker/checker.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable record-checker >/dev/null 2>&1
systemctl restart record-checker
sleep 2
systemctl is-active --quiet record-checker && echo ">> Record Checker running. Log: journalctl -u record-checker -f" \
    || { echo "!! It did not start, see: journalctl -u record-checker -n 30"; exit 1; }
port=$(sed -n 's/^CHECKER_PORT=\([0-9]*\).*/\1/p' /etc/record-checker.conf); port=${port:-8099}
echo ">> Open it from your PC through an SSH tunnel:  ssh -L $port:127.0.0.1:$port root@<server>  then http://localhost:$port"
