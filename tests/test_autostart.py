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
            self.assertEqual(json.loads(source.read_text())['host'], '192.168.0.128')

    def test_service_survives_network_off_and_escapes_paths(self):
        service = autostart.unit('/home/pi/edge 100%', '/etc/runtime.json', 'pi', 'pi', ['/home/pi/data'], 'api')
        self.assertIn('Restart=always', service)
        self.assertIn('WantedBy=multi-user.target', service)
        self.assertIn('edge 100%%', service)
        self.assertNotIn('BindsTo=', service)
        self.assertNotIn('Requires=NetworkManager', service)
        self.assertNotIn('network-online.target', service)
