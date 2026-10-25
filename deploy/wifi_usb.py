#!/usr/bin/env python3
"""NetworkManager hotspot boot setup and debounced physical USB insertion toggle."""
import argparse
import getpass
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

CONFIG = Path('/etc/tilik-payload.json')
STATE = Path('/run/tilik-payload/state.json')
SYSFS = Path('/sys/bus/usb/devices')
LOG = logging.getLogger('tilik-payload')


def nm(*args):
    try:
        return subprocess.run(['nmcli', '--wait', '20', *args], check=True,
                              capture_output=True, text=True, timeout=25)
    except subprocess.CalledProcessError as error:
        LOG.error('NetworkManager: %s', error.stderr.strip())
        raise


def read(path):
    return path.read_text().strip()


def devices(root=SYSFS, ignored=()):
    """One entry per physical device; never USB interfaces, roots or hubs."""
    result = set()
    for path in root.iterdir():
        if ':' in path.name or path.name.startswith('usb'):
            continue
        try:
            vendor, product = read(path / 'idVendor'), read(path / 'idProduct')
            name = read(path / 'product') if (path / 'product').exists() else ''
            if read(path / 'bDeviceClass').lower() == '09':
                continue
            if f'{vendor}:{product}' in {'1e4e:0100', *ignored} or 'purethermal' in name.lower():
                continue
            # devnum distinguishes a quick unplug/replug on the same physical port.
            result.add((path.name, read(path / 'busnum'), read(path / 'devnum')))
        except (OSError, ValueError):
            continue  # Device disappeared or is still enumerating; retry next poll.
    return result


class Insertions:
    def __init__(self, baseline, settle=2):
        self.known = set(baseline)
        self.pending = {}
        self.settle = settle

    def update(self, current, now):
        self.known.intersection_update(current)
        self.pending = {key: since for key, since in self.pending.items() if key in current}
        for key in current - self.known:
            self.pending.setdefault(key, now)
        ready = sorted(key for key, since in self.pending.items() if now - since >= self.settle)
        for key in ready:
            self.pending.pop(key)
            self.known.add(key)
        return ready


def activate(settings, enabled):
    if enabled:
        nm('radio', 'wifi', 'on')
        nm('connection', 'up', 'uuid', settings['connection_uuid'], 'ifname', settings['interface'])
    else:
        nm('radio', 'wifi', 'off')
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps({'enabled': enabled}))
    temp.replace(STATE)
    LOG.info('Hotspot %s', 'ON' if enabled else 'OFF')


def run():
    settings = json.loads(CONFIG.read_text())
    # /run resets on reboot. A service restart preserves the last successful state.
    enabled = json.loads(STATE.read_text())['enabled'] if STATE.exists() else True
    subprocess.run(['udevadm', 'settle', '--timeout=10'], check=False, timeout=12)
    tracker = Insertions(devices(ignored=settings['ignore_usb_ids']))
    activate(settings, enabled)
    while True:
        for device in tracker.update(devices(ignored=settings['ignore_usb_ids']), time.monotonic()):
            LOG.info('USB insertion: %s', device[0])
            try:
                activate(settings, not enabled)
                enabled = not enabled
            except (subprocess.SubprocessError, OSError):
                LOG.exception('Wi-Fi change failed; restore previous state')
                activate(settings, enabled)
        time.sleep(0.25)


def diagnose():
    print('Configuration installed:', CONFIG.exists())
    print('USB devices eligible to toggle:')
    settings = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    eligible = devices(ignored=settings.get('ignore_usb_ids', []))
    for path in sorted(SYSFS.iterdir()):
        if ':' in path.name or not (path / 'idVendor').exists():
            continue
        try:
            identity = (path.name, read(path / 'busnum'), read(path / 'devnum'))
            label = read(path / 'product') if (path / 'product').exists() else '(no product name)'
            print(f"  {path.name}: {read(path / 'idVendor')}:{read(path / 'idProduct')} {label} "
                  f"[{'TRIGGER' if identity in eligible else 'IGNORED'}]")
        except OSError:
            continue
    for command in (["systemctl", "is-enabled", "tilik-payload"],
                    ["systemctl", "is-active", "tilik-payload"],
                    ["nmcli", "radio", "wifi"],
                    ["nmcli", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"]):
        print('>', ' '.join(command), flush=True)
        subprocess.run(command, check=False, timeout=10)


def install(interface):
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo on Raspberry Pi.')
    for command in ('nmcli', 'systemctl', 'udevadm'):
        if not shutil.which(command):
            raise SystemExit(f'Missing {command}; requires Raspberry Pi OS with NetworkManager.')
    subprocess.run(['systemctl', 'is-active', '--quiet', 'NetworkManager'], check=True)
    if not Path('/sys/class/net', interface).exists():
        raise SystemExit(f'Wi-Fi interface {interface} does not exist.')
    profile_dir = Path('/etc/NetworkManager/system-connections')
    profile_dir.mkdir(parents=True, exist_ok=True)
    keyfile = profile_dir / 'tilik-payload.nmconnection'
    if CONFIG.exists():
        settings = json.loads(CONFIG.read_text())
        if settings['interface'] != interface or not keyfile.exists():
            raise SystemExit('Existing configuration differs or keyfile missing; inspect /etc/tilik-payload.json first.')
    else:
        if keyfile.exists():
            raise SystemExit('Existing hotspot keyfile found; refusing to overwrite its credentials.')
        password = getpass.getpass('New Tilik-Drone-Payload Wi-Fi password (8–63 ASCII characters): ')
        if not (8 <= len(password) <= 63) or not password.isascii() or not password.isprintable() or password != password.strip():
            raise SystemExit('Password must be 8–63 printable ASCII characters without leading/trailing spaces.')
        if password != getpass.getpass('Repeat password: '):
            raise SystemExit('Passwords do not match.')
        settings = {'interface': interface, 'connection_uuid': str(uuid.uuid4()), 'ignore_usb_ids': []}
        # Password stays in this root-only keyfile, never Git or command-line arguments.
        escaped = password.replace('\\', '\\\\').replace(';', '\\;')
        content = f'''[connection]
id=Tilik-Drone-Payload
uuid={settings['connection_uuid']}
type=wifi
interface-name={interface}
autoconnect=true
autoconnect-priority=999

[wifi]
mode=ap
ssid=Tilik-Drone-Payload
band=bg

[wifi-security]
key-mgmt=wpa-psk
psk={escaped}

[ipv4]
method=shared
address1=10.42.0.1/24

[ipv6]
method=disabled
'''
        with os.fdopen(os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
            stream.write(content)
        CONFIG.write_text(json.dumps(settings, indent=2) + '\n')
        CONFIG.chmod(0o600)
    nm('connection', 'load', str(keyfile))
    destination = Path('/opt/tilik-network')
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(__file__, destination / 'wifi_usb.py')
    shutil.copyfile(Path(__file__).with_name('tilik-payload.service'), '/etc/systemd/system/tilik-payload.service')
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', 'tilik-payload.service'], check=True)
    print('Installed. Reboot from local console/VNC when ready; hotspot starts at boot.')
    print('Existing Wi-Fi profiles and thermal captures are preserved. No network switch performed now.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--install', action='store_true')
    mode.add_argument('--diagnose', action='store_true')
    parser.add_argument('--interface', default='wlan0')
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if options.diagnose:
        diagnose()
    elif options.install:
        install(options.interface)
    else:
        run()
