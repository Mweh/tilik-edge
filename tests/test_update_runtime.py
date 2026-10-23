import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeUpdateTests(unittest.TestCase):
    def test_full_update_restores_missing_commands_and_preserves_config_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            remote = workspace / 'pi install'
            remote.mkdir()
            (remote / 'edge').mkdir(); (remote / 'edge/__init__.py').write_text('')
            (remote / 'hotspot').mkdir(); (remote / 'hotspot/__init__.py').write_text('')
            (remote / 'config').mkdir(); (remote / 'config/edge.json').write_bytes(b'KEEP LIVE CONFIG')
            (remote / 'data/captures/live').mkdir(parents=True)
            (remote / 'data/captures/live/evidence.bin').write_bytes(b'KEEP EVIDENCE')
            (remote / 'data/catalog.sqlite').write_bytes(b'KEEP CATALOG')
            binary = workspace / 'bin'; binary.mkdir()
            fake_scp = binary / 'scp'
            fake_scp.write_text("""#!/usr/bin/env python3
import os, pathlib, shutil, sys
relative = sys.argv[-1].split(':', 1)[1].removeprefix('tilik-edge/')
shutil.copyfile(sys.argv[-2], pathlib.Path(os.environ['TILIK_TEST_PI_DIR']) / relative)
""")
            fake_ssh = binary / 'ssh'
            fake_ssh.write_text("""#!/usr/bin/env python3
import os, subprocess, sys
script = sys.stdin.read().replace('$HOME/tilik-edge', os.environ['TILIK_TEST_PI_DIR'])
sys.exit(subprocess.run(['/bin/sh', '-s'], input=script, text=True).returncode)
""")
            fake_scp.chmod(0o755); fake_ssh.chmod(0o755)
            result = subprocess.run(['/bin/sh', str(ROOT / 'deploy/update-api.sh'), 'pi@test'],
                env={**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'],
                     'TILIK_TEST_PI_DIR': str(remote)}, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((remote / 'edge/commands.py').is_file())
            self.assertTrue((remote / 'edge/discovery.py').is_file())
            self.assertTrue((remote / 'edge/dashboard.html').is_file())
            self.assertTrue((remote / 'hotspot/camera.py').is_file())
            self.assertEqual((remote / 'config/edge.json').read_bytes(), b'KEEP LIVE CONFIG')
            self.assertEqual((remote / 'data/captures/live/evidence.bin').read_bytes(), b'KEEP EVIDENCE')
            self.assertEqual((remote / 'data/catalog.sqlite').read_bytes(), b'KEEP CATALOG')
            backups = list(remote.glob('.runtime-backup.*'))
            self.assertEqual(len(backups), 1)
            self.assertTrue((backups[0] / 'edge/__init__.py').exists())
            self.assertFalse(list(remote.glob('.runtime-update.*')))

    def test_incomplete_runtime_fails_before_spawning_camera(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            ignore = shutil.ignore_patterns('__pycache__', '*.pyc')
            shutil.copytree(ROOT / 'edge', stage / 'edge', ignore=ignore)
            shutil.copytree(ROOT / 'hotspot', stage / 'hotspot', ignore=ignore)
            (stage / 'edge/commands.py').unlink()
            (stage / 'rules.json').write_bytes((ROOT / 'config/landfill.json').read_bytes())
            (stage / 'edge.json').write_text(json.dumps({'data_dir': 'data', 'rules': 'rules.json',
                'source': 'simulation', 'device': '/dev/video0', 'confirm_centikelvin': False,
                'host': '127.0.0.1', 'port': 0}))
            result = subprocess.run([sys.executable, '-m', 'edge', 'run', '--config', 'edge.json'],
                cwd=stage, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn('Incomplete Tilik runtime', result.stderr)
            self.assertIn('edge.commands', result.stderr)
            self.assertNotIn('Dashboard and iOS API', result.stdout)
            self.assertFalse((stage / 'data/captures').exists())
