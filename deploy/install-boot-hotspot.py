#!/usr/bin/env python3
"""Install Tilik-Payload boot hotspot without USB toggles or firmware changes."""
import argparse
import configparser
import getpass
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

PROFILE = Path('/etc/NetworkManager/system-connections/tilik-boot-hotspot.nmconnection')
UNIT = Path('/etc/systemd/system/tilik-hotspot.service')
SSID = 'Tilik-Payload'


def keyfile(password, connection_uuid, interface):
    if not (8 <= len(password) <= 63 and password.isascii() and password.isprintable()
            and password == password.strip()):
        raise ValueError('Password must be 8–63 printable ASCII characters, without outer spaces.')
    uuid.UUID(connection_uuid)
    if not interface or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in interface):
        raise ValueError('Invalid Wi-Fi interface name.')
    escaped = password.replace('\\', '\\\\')
    return f'''[connection]
id=tilik-boot-hotspot
uuid={connection_uuid}
type=wifi
interface-name={interface}
autoconnect=true
autoconnect-priority=999
autoconnect-retries=0

[wifi]
mode=ap
ssid={SSID}
band=bg
channel=6

[wifi-security]
key-mgmt=wpa-psk
proto=rsn;
psk={escaped}

[ipv4]
method=shared
address1=10.42.0.1/24

[ipv6]
method=disabled
'''


def service(connection_uuid, interface):
    uuid.UUID(connection_uuid)
    return f'''[Unit]
Description=Tilik-Payload hotspot at boot (no USB toggle)
Requires=NetworkManager.service
After=NetworkManager.service
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStartPre=/usr/bin/nmcli --wait 10 radio wifi on
ExecStart=/usr/bin/nmcli --wait 30 connection up uuid {connection_uuid} ifname {interface}
RemainAfterExit=yes
Restart=on-failure
RestartSec=15
TimeoutStartSec=50

[Install]
WantedBy=multi-user.target
'''


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True)


def install(interface, password_file=None):
    if os.geteuid() != 0 or sys.platform != 'linux':
        raise SystemExit('Run on the Pi: sudo python3 ~/tilik-edge/deploy/install-boot-hotspot.py')
    for tool in ('nmcli', 'systemctl'):
        if not shutil.which(tool):
            raise SystemExit(f'Missing {tool}; requires Raspberry Pi OS with NetworkManager.')
    run('systemctl', 'is-active', '--quiet', 'NetworkManager')
    if not Path('/sys/class/net', interface).exists():
        raise SystemExit('Wi-Fi interface not found: ' + interface)
    modes = run('nmcli', '-g', 'WIFI-PROPERTIES.AP', 'device', 'show', interface).stdout.strip()
    if modes != 'yes':
        raise SystemExit('NetworkManager does not report AP support: ' + modes)
    # Validate the runtime prerequisites before enabling any network boot changes.
    root = Path(__file__).resolve().parents[1]
    config = root / 'config/pi.local.json'
    if not config.is_file():
        raise SystemExit('Set up config/pi.local.json and the capture store first.')
    if not (shutil.which('ffmpeg') or shutil.which('v4l2-ctl')):
        raise SystemExit('Install ffmpeg or v4l-utils before enabling camera autostart.')
    if not (shutil.which('dnsmasq') or Path('/usr/sbin/dnsmasq').is_file()):
        raise SystemExit('Install DHCP helper first: sudo apt-get install dnsmasq-base. Network settings unchanged.')
    # Complete runtime validation/installation before any network writes. A bad
    # config cannot enable an AP without its API/capture services being installed.
    # Independent API/camera services survive network changes and start at boot.
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location('tilik_autostart', root/'deploy/install-autostart.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = sys.argv
    try:
        sys.argv = ['install-autostart.py', '--capture', '--config', str(config),
                    '--user', os.environ.get('SUDO_USER', 'pi')]
        module.main()
    finally:
        sys.argv = original
    identity = str(uuid.uuid4())
    if PROFILE.exists():
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(PROFILE)
        if parser.get('wifi', 'ssid', fallback='') != SSID or parser.get('connection', 'interface-name', fallback='') != interface:
            raise SystemExit('Existing hotspot profile differs; refusing to overwrite it.')
        identity = parser['connection']['uuid']
        uuid.UUID(identity)
        if password_file:
            raise SystemExit('Hotspot already configured; password preserved. Change it through NetworkManager.')
    else:
        if password_file:
            password = Path(password_file).read_text().rstrip('\r\n')
        else:
            password = getpass.getpass('New Tilik-Payload Wi-Fi password (8–63 characters): ')
            if password != getpass.getpass('Repeat Wi-Fi password: '):
                raise SystemExit('Passwords do not match.')
        content = keyfile(password, identity, interface)
        PROFILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(PROFILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    run('nmcli', 'connection', 'load', str(PROFILE))
    # Prevent known legacy Wi-Fi togglers from competing with the boot-only AP.
    for name in ('tilik-payload.service', 'tilik-recover-hotspot.service'):
        exists = subprocess.run(['systemctl', 'cat', name], capture_output=True).returncode == 0
        if exists:
            run('systemctl', 'disable', '--now', name)
    UNIT.write_text(service(identity, interface))
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', 'tilik-hotspot.service')
    print('Installed for next boot: Tilik-Payload, API, and camera services.')
    print('Current Wi-Fi remains connected. No reboot performed.')
    print('After reboot, join Tilik-Payload and open http://10.42.0.1:8080')
    print('SSH: ssh pi@10.42.0.1. Hotspot does not require internet for sync.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', default='wlan0')
    parser.add_argument('--password-file', help='Read hotspot password from a local file; do not put secrets in argv')
    args = parser.parse_args()
    try:
        install(args.interface, args.password_file)
    except subprocess.CalledProcessError as error:
        raise SystemExit((error.stderr or '').strip() or 'System configuration command failed; inspect service logs.')
