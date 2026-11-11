#!/bin/sh
# Run on the grounded Pi. Does not change the current Wi-Fi state.
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run with sudo'; exit 1; }
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
systemctl cat tilik-hotspot.service >/dev/null
# Legacy togglers must not compete with BLE control.
for unit in tilik-payload.service tilik-recover-hotspot.service; do
    if systemctl is-active --quiet "$unit" || systemctl is-enabled --quiet "$unit" 2>/dev/null; then
        echo "Disable legacy toggler $unit before installing BLE control"; exit 1
    fi
done
missing=0
for package in bluez python3-venv; do
    state=$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)
    [ "$state" = 'install ok installed' ] || missing=1
done
if [ "$missing" = 1 ]; then
    apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=20 update
    apt-get install -y bluez python3-venv
fi
python3 -m venv /opt/tilik-ble-venv
if [ -n "${1:-}" ]; then
    /opt/tilik-ble-venv/bin/pip install --no-index --find-links "$1" 'bless==0.2.6' 'bleak==0.22.3'
else
    /opt/tilik-ble-venv/bin/pip install --timeout 20 --retries 1 'bless==0.2.6' 'bleak==0.22.3'
fi
install -d -m 755 /opt/tilik-ble
install -m 644 "$repo/edge/ble_wifi.py" /opt/tilik-ble/ble_wifi.py
if [ ! -e /etc/tilik-ble.key ]; then
    (umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > /etc/tilik-ble.key)
fi
chmod 600 /etc/tilik-ble.key
cat > /etc/systemd/system/tilik-ble.service <<'UNIT'
[Unit]
Description=Tilik authenticated Bluetooth Wi-Fi control
Requires=bluetooth.service NetworkManager.service
After=bluetooth.service NetworkManager.service

[Service]
ExecStart=/opt/tilik-ble-venv/bin/python /opt/tilik-ble/ble_wifi.py
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
ProtectHome=true
ProtectSystem=strict
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now bluetooth.service
rfkill unblock bluetooth
attempt=0
until bluetoothctl show | grep -q 'Powered: yes'; do
    attempt=$((attempt + 1))
    [ "$attempt" -le 5 ] || { echo 'Bluetooth adapter did not power on'; exit 1; }
    bluetoothctl --timeout 5 power on || true
    sleep 1
done
systemctl enable tilik-ble.service
systemctl restart tilik-ble.service
systemctl is-active tilik-ble.service
echo 'Installed. Wi-Fi unchanged. Provision /etc/tilik-ble.key privately to your client.'
