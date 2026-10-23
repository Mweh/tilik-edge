import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class GitWorkflowTests(unittest.TestCase):
    def test_existing_pi_connects_without_losing_settings_rules_or_captures(self):
        with tempfile.TemporaryDirectory() as temporary:
            area = Path(temporary)
            source = area / 'remote source'; source.mkdir()
            files = {
                'edge/__init__.py': '', 'edge/api.py': 'from .commands import Commands\n',
                'edge/commands.py': 'class Commands: pass\n', 'edge/discovery.py': '',
                'hotspot/__init__.py': '', 'hotspot/__main__.py': '',
                '.gitignore': (ROOT / '.gitignore').read_text(),
                'start.sh': (ROOT / 'start.sh').read_text(),
                'deploy/connect-git.sh': (ROOT / 'deploy/connect-git.sh').read_text(),
                'config/edge.json': json.dumps({'data_dir': '../data', 'rules': 'landfill.json', 'source': 'simulation'}),
                'config/landfill.json': json.dumps({'custom_threshold': 55})
            }
            for name, data in files.items():
                target = source / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(data)
            def git(*args, cwd=source):
                return subprocess.check_output(['git', *args], cwd=cwd, stderr=subprocess.STDOUT, text=True).strip()
            git('init', '-b', 'main'); git('add', '.')
            git('-c', 'user.name=Runtime Test', '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-m', 'test: seed runtime')
            revision = git('rev-parse', 'HEAD')
            pi = area / 'pi install'; pi.mkdir()
            for folder in ['edge', 'hotspot', 'config', 'deploy']:
                shutil.copytree(source / folder, pi / folder)
            shutil.copyfile(source / 'start.sh', pi / 'start.sh')
            (pi / 'config/edge.json').write_text(json.dumps({'data_dir': '../data', 'rules': 'landfill.json', 'source': 'live', 'confirm_centikelvin': True}))
            (pi / 'config/landfill.json').write_text(json.dumps({'custom_threshold': 30}))
            (pi / 'data/captures').mkdir(parents=True)
            (pi / 'data/captures/evidence.bin').write_bytes(b'original evidence')
            (pi / 'data/catalog.sqlite').write_bytes(b'original catalog')
            binary = area / 'bin'; binary.mkdir()
            (binary / 'uname').write_text('#!/bin/sh\nprintf "Linux\n"\n'); (binary / 'uname').chmod(0o755)
            env = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH']}
            result = subprocess.run(['/bin/sh', str(pi / 'deploy/connect-git.sh'), str(source)], cwd=pi, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(git('rev-parse', 'HEAD', cwd=pi), revision)
            self.assertEqual(git('status', '--porcelain', cwd=pi), '')
            local = json.loads((pi / 'config/pi.local.json').read_text())
            self.assertEqual(local['source'], 'live'); self.assertTrue(local['confirm_centikelvin'])
            self.assertEqual(local['rules'], 'rules.local.json')
            self.assertEqual(Path(local['data_dir']), (pi / 'data').resolve())
            self.assertEqual(json.loads((pi / 'config/rules.local.json').read_text())['custom_threshold'], 30)
            self.assertEqual((pi / 'data/captures/evidence.bin').read_bytes(), b'original evidence')
            self.assertEqual((pi / 'data/catalog.sqlite').read_bytes(), b'original catalog')
            self.assertEqual(len(list(pi.glob('.git-bootstrap-backup.*'))), 1)
            second = subprocess.run(['/bin/sh', str(pi / 'deploy/connect-git.sh'), str(source)], cwd=pi, env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(second.returncode, 0)
            self.assertEqual(len(list(pi.glob('.git-bootstrap-backup.*'))), 1)

    def test_start_uses_private_pi_configuration_when_present(self):
        with tempfile.TemporaryDirectory() as temporary:
            area = Path(temporary)
            shutil.copyfile(ROOT / 'start.sh', area / 'start.sh')
            (area / 'config').mkdir(); (area / 'config/pi.local.json').write_text('{}')
            binary = area / 'bin'; binary.mkdir()
            python = binary / 'python3'
            python.write_text('#!/bin/sh\nprintf "%s\n" "$@"\n'); python.chmod(0o755)
            env = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH']}
            result = subprocess.run(['/bin/sh', str(area / 'start.sh')], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn('config/pi.local.json', result.stdout)
            explicit = subprocess.run(['/bin/sh', str(area / 'start.sh'), 'config/bench.json'], env=env, capture_output=True, text=True)
            self.assertIn('config/bench.json', explicit.stdout)
