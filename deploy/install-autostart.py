#!/usr/bin/env python3
"""Install boot services against the existing checkout and capture store."""
import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import time


def quote(value):
    # systemd specifiers must be escaped even within quotes.
    return json.dumps(str(value).replace('%', '%%'), ensure_ascii=False)


def unit(root, config, username, group, writable, component):
    paths = ' '.join(quote(path) for path in writable)
    return f'''[Unit]
Description=Tilik {component} (existing capture store)
After=network.target
StartLimitIntervalSec=0
RequiresMountsFor={quote(root)} {paths}

[Service]
Type=simple
User={username}
Group={group}
SupplementaryGroups=video
WorkingDirectory={quote(root)}
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile=-/etc/tilik-edge.env
ExecStart=/usr/bin/python3 -m edge {component} --config {quote(config)}
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={paths}
PrivateTmp=true
UMask=0027

[Install]
WantedBy=multi-user.target
'''


def configuration(source):
    values = json.loads(source.read_text())
    for key in ('data_dir', 'rules', 'tls_cert', 'tls_key'):
        if values.get(key):
            values[key] = str((source.parent / values[key]).resolve())
    values['host'] = '0.0.0.0'
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', help='Existing runtime config; defaults to config/pi.local.json then config/edge.json')
    parser.add_argument('--user', default=os.environ.get('SUDO_USER', 'pi'))
    parser.add_argument('--capture', action='store_true', help='Also install/enable the independent camera capture service')
    args = parser.parse_args()
    if os.geteuid() != 0 or os.uname().sysname != 'Linux':
        raise SystemExit('Run using sudo on Raspberry Pi/Linux.')
    root = Path(__file__).resolve().parents[1]
    source = Path(args.config).resolve() if args.config else root / 'config/pi.local.json'
    if not args.config and not source.exists():
        source = root / 'config/edge.json'
    if not shutil.which('systemctl'):
        raise SystemExit('systemd is required.')
    if args.capture and not (shutil.which('ffmpeg') or shutil.which('v4l2-ctl')):
        raise SystemExit('Install ffmpeg or v4l-utils before enabling capture.')
    account = pwd.getpwnam(args.user)
    if account.pw_uid == 0:
        raise SystemExit('Choose a non-root runtime user using --user.')
    values = configuration(source)
    data, rules = Path(values['data_dir']), Path(values['rules'])
    if not data.is_dir() or not rules.is_file():
        raise SystemExit('Existing data directory and rules file must exist; refusing to select a new capture store.')
    config = Path('/etc/tilik-runtime.json')
    if config.exists():
        backup = Path(str(config) + f'.backup-{time.time_ns()}')
        shutil.copy2(config, backup)
    config.write_text(json.dumps(values, indent=2) + '\n')
    os.chown(config, 0, account.pw_gid)
    config.chmod(0o640)
    group = grp.getgrgid(account.pw_gid).gr_name
    for component in (['api', 'capture'] if args.capture else ['api']):
        destination = Path(f'/etc/systemd/system/tilik-{component}.service')
        if destination.exists():
            shutil.copy2(destination, Path(str(destination) + f'.backup-{time.time_ns()}'))
        destination.write_text(unit(root, config, args.user, group, [data, rules.parent], component))
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    services = ['tilik-api'] + (['tilik-capture'] if args.capture else [])
    subprocess.run(['systemctl', 'enable', *services], check=True)
    print('Enabled for boot:', ', '.join(services))
    print('Existing data store:', data)
    print('API binds 0.0.0.0; network changes do not require an API restart.')
    print('Stop any old start.sh process before starting services to avoid port/camera conflicts.')
    print('Then run: sudo systemctl restart ' + ' '.join(services))


if __name__ == '__main__':
    main()
