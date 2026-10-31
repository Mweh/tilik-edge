#!/bin/sh
# Fresh install or safe rerun. Does not change Wi-Fi profiles or boot firmware.
set -eu
[ "$(uname -s)" = Linux ] || { echo 'Run on the Raspberry Pi.'; exit 1; }
[ "$(id -u)" = 0 ] || { echo 'Run: sudo sh deploy/setup-pi.sh'; exit 1; }
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RUNTIME_USER=${SUDO_USER:-pi}
[ "$RUNTIME_USER" != root ] || { echo 'Run with sudo from your normal Pi account.'; exit 1; }
# Optional live activation requires an explicitly verified radiometric camera.
DEVICE=
CONFIRMED=false
while [ "$#" -gt 0 ]; do
    case "$1" in
        --device) [ "$#" -ge 2 ] || exit 2; DEVICE=$2; shift 2 ;;
        --confirm-centikelvin) CONFIRMED=true; shift ;;
        *) echo "Unknown argument: $1"; exit 2 ;;
    esac
done
if [ "$CONFIRMED" = true ] && [ -z "$DEVICE" ]; then
    echo 'Supply --device with the verified PureThermal Y16 node.'; exit 2
fi
if [ -n "$DEVICE" ] && [ "$CONFIRMED" != true ]; then
    echo 'First verify TLinear 0.01 K, AGC off, telemetry off; then add --confirm-centikelvin.'; exit 2
fi
if [ -n "$DEVICE" ] && [ "$DEVICE" != auto ] && [ ! -c "$DEVICE" ]; then
    echo "Camera device not found: $DEVICE"; exit 2
fi
apt-get update
apt-get install -y python3 git ffmpeg v4l-utils avahi-daemon avahi-utils
systemctl enable --now avahi-daemon
cd "$ROOT"
# Do not reset previously adjusted rules or replace the device identity/capture store.
python3 - "$DEVICE" "$CONFIRMED" <<'PY'
import json, shutil, sys
from pathlib import Path
config = Path('config/pi.local.json')
rules = Path('config/rules.local.json')
if not rules.exists(): shutil.copyfile('config/landfill.json', rules)
if config.exists():
    values = json.loads(config.read_text())
else:
    values = json.loads(Path('config/edge.json').read_text())
    values['rules'] = 'rules.local.json'
values['host'] = '0.0.0.0'
if sys.argv[1]:
    values.update(source='live', device=sys.argv[1], confirm_centikelvin=True)
config.write_text(json.dumps(values, indent=2) + '\n')
config.chmod(0o600)
from edge.settings import load
settings = load(config)
print('Source:', settings['source'], '| Store:', settings['data_dir'])
PY
chown "$RUNTIME_USER:$(id -gn "$RUNTIME_USER")" config/pi.local.json config/rules.local.json
# On a fresh checkout load() creates the data directory as root. Only newly
# created runtime initialization files need their ownership adjusted here.
python3 - "$RUNTIME_USER" <<'PY'
import os, pwd
from pathlib import Path
from edge.settings import load
settings=load('config/pi.local.json'); account=pwd.getpwnam(__import__('sys').argv[1])
for path in (settings['data_dir'], settings['data_dir']/'.identity.lock', settings['data_dir']/'device-id'):
    if path.stat().st_uid == 0: os.chown(path, account.pw_uid, account.pw_gid)
PY
python3 deploy/install-autostart.py --user "$RUNTIME_USER" --capture
systemctl restart tilik-api tilik-capture
systemctl --no-pager --full status tilik-api tilik-capture
echo 'Dashboard/API: http://raspberrypi.local:8080 (or this Pi hostname/IP).'
echo 'Bonjour: _tilik-edge._tcp. No fixed Wi-Fi SSID required.'
echo 'If source=simulation, those captures are labeled simulation, not camera measurements.'
