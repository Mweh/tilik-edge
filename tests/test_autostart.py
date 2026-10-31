import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('autostart', Path(__file__).parents[1] / 'deploy/install-autostart.py')
autostart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autostart)


class AutostartTests(unittest.TestCase):
    def test_existing_store_rules_and_live_source_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'config' / 'pi.local.json'
            source.parent.mkdir()
            source.write_text(json.dumps({'data_dir': '../data', 'rules': 'rules.local.json',
                'host': '192.168.0.128', 'source': 'live', 'device': '/dev/video0', 'port': 8080}))
            settings = autostart.configuration(source)
            self.assertEqual(settings['host'], '0.0.0.0')
            self.assertEqual(settings['data_dir'], str((Path(directory) / 'data').resolve()))
            self.assertEqual(settings['rules'], str((source.parent / 'rules.local.json').resolve()))
            self.assertEqual(settings['source'], 'live')
            self.assertEqual(settings['device'], 'auto')
            self.assertEqual(json.loads(source.read_text())['device'], '/dev/video0')
            self.assertEqual(json.loads(source.read_text())['host'], '192.168.0.128')

    def test_service_survives_network_off_and_escapes_paths(self):
        service = autostart.unit('/home/pi/edge 100%', '/etc/runtime.json', 'pi', 'pi', ['/home/pi/data'], 'api')
        self.assertIn('Restart=always', service)
        self.assertIn('TimeoutStopSec=20', service)
        self.assertIn('KillMode=control-group', service)
        self.assertIn('WantedBy=multi-user.target', service)
        self.assertIn('edge 100%%', service)
        self.assertIn('WorkingDirectory=/home/pi/edge 100%%\n', service)
        self.assertNotIn('WorkingDirectory="', service)
        self.assertNotIn('BindsTo=', service)
        self.assertNotIn('Requires=NetworkManager', service)
        self.assertNotIn('network-online.target', service)

    def test_pinned_identity_and_simulation_are_not_silently_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'edge.json'
            for mode, device in [('live', '/dev/v4l/by-id/purethermal-video-index0'),
                                 ('simulation', '/dev/video0')]:
                source.write_text(json.dumps(dict(data_dir='data', rules='rules.json',
                    source=mode, device=device)))
                self.assertEqual(autostart.configuration(source)['device'], device)
