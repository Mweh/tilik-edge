"""Exercise bootstrap configuration using mocked Linux package/service commands."""
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest


class FreshSetupTests(unittest.TestCase):
    def test_fresh_setup_and_rerun_preserve_rules_identity_and_captures(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('edge', 'config', 'deploy'):
                shutil.copytree(source / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
            commands = root / 'bin'
            commands.mkdir()
            scripts = {
                'uname': 'echo Linux',
                'id': 'if [ "$1" = -u ]; then echo 0; else echo staff; fi',
                'apt-get': 'exit 0', 'systemctl': 'exit 0', 'chown': 'exit 0',
                'python3': 'if [ "$1" = deploy/install-autostart.py ]; then exit 0; fi\nexec "$REAL_PYTHON" "$@"',
            }
            for name, body in scripts.items():
                path = commands / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(commands) + ':' + os.environ['PATH'],
                       SUDO_USER=pwd.getpwuid(os.getuid()).pw_name, REAL_PYTHON=sys.executable)
            def run(*args):
                result = subprocess.run(['sh', str(root/'deploy/setup-pi.sh'), *args],
                                        cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run()
            config = json.loads((root/'config/pi.local.json').read_text())
            self.assertEqual(config['source'], 'simulation')
            self.assertEqual(config['host'], '0.0.0.0')
            identity = (root/'data/device-id').read_text()
            rules = root/'config/rules.local.json'
            adjusted = json.loads(rules.read_text())
            adjusted['cooldown_seconds'] = 61
            rules.write_text(json.dumps(adjusted))
            capture = root/'data/keep.txt'
            capture.write_text('existing evidence')
            run('--device', '/dev/null', '--confirm-centikelvin')
            self.assertEqual(json.loads((root/'config/pi.local.json').read_text())['source'], 'live')
            run()
            self.assertEqual(json.loads((root/'config/pi.local.json').read_text())['source'], 'live')
            self.assertEqual((root/'data/device-id').read_text(), identity)
            self.assertEqual(json.loads(rules.read_text())['cooldown_seconds'], 61)
            self.assertEqual(capture.read_text(), 'existing evidence')
