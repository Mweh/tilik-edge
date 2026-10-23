#!/bin/sh
set -eu
[ "$(uname -s)" = Linux ] || { echo 'Install services on Raspberry Pi/Linux only'; exit 1; }
[ "$(id -u)" = 0 ] || { echo 'Run sudo sh deploy/install.sh on the Pi'; exit 1; }
command -v ffmpeg >/dev/null || { echo 'Install ffmpeg and v4l-utils first'; exit 1; }
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
id tilik >/dev/null 2>&1 || useradd --system --home-dir /var/lib/tilik-edge --groups video --shell /usr/sbin/nologin tilik
install -d /opt/tilik-edge /var/lib/tilik-edge
cp -R "$PROJECT_DIR/edge" "$PROJECT_DIR/hotspot" /opt/tilik-edge/
if [ ! -f /var/lib/tilik-edge/rules.json ]; then
    cp "$PROJECT_DIR/config/landfill.json" /var/lib/tilik-edge/rules.json
fi
if [ ! -f /etc/tilik-edge.json ]; then
    cat > /etc/tilik-edge.json <<'CONFIG'
{
  "data_dir": "/var/lib/tilik-edge",
  "rules": "/var/lib/tilik-edge/rules.json",
  "source": "simulation",
  "device": "/dev/video0",
  "confirm_centikelvin": false,
  "host": "0.0.0.0",
  "port": 8080
}
CONFIG
fi
chown -R tilik:tilik /var/lib/tilik-edge
install -m 644 "$PROJECT_DIR/deploy/tilik-capture.service" "$PROJECT_DIR/deploy/tilik-api.service" /etc/systemd/system/
systemctl daemon-reload
echo 'For automatic discovery, install: sudo apt-get install avahi-daemon avahi-utils'
echo 'Installed (not started). Review /etc/tilik-edge.json, then:'
echo 'sudo systemctl enable --now tilik-capture tilik-api'
echo 'Stop old camera/API processes before starting these services.'
