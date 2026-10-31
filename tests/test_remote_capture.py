import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from edge.api import Handler, make_server
from edge.settings import load

ROOT = Path(__file__).resolve().parents[1]


class RemoteCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.rules = root / 'rules.json'
        self.rules.write_bytes((ROOT / 'config/landfill.json').read_bytes())
        self.config = root / 'edge.json'
        self.config.write_text(json.dumps(dict(data_dir='data', rules='rules.json',
            source='live', device='auto', confirm_centikelvin=True, host='127.0.0.1', port=0)))
        self.settings = load(self.config)
        with patch.dict(os.environ, {'TILIK_API_TOKEN': ''}), patch('edge.api.advertise', return_value=None):
            self.server = make_server(self.settings)
        status = self.settings['data_dir'] / 'captures/live/status.json'
        status.parent.mkdir(exist_ok=True)
        status.write_text(json.dumps(dict(state='IDLE', updated_at=time.time())))
        # Keep real TCP/HTTP routing, auth, origin validation and command store;
        # only replace the peer address to represent a phone on the LAN.
        original_setup = Handler.setup
        def remote_setup(handler):
            original_setup(handler)
            handler.client_address = ('10.42.0.42', handler.client_address[1])
        self.peer = patch.object(Handler, 'setup', remote_setup)
        self.peer.start()
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.worker.join()
        self.server.stopping.set()
        self.server.scanner.join()
        self.server.server_close()
        self.server.catalog.db.close()
        self.peer.stop()
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None):
        request = Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                          headers={'Content-Type': 'application/json', **(headers or {})})
        with urlopen(request, timeout=3) as response:
            return response.status, json.load(response)

    def expect_http(self, code, path, body, headers=None):
        with self.assertRaises(HTTPError) as error:
            self.request(path, body, headers)
        self.assertEqual(error.exception.code, code)
        error.exception.close()

    def test_lan_capture_and_status_permissions_without_rule_access(self):
        _, status = self.request('/api/status')
        self.assertFalse(status['controls']['allowed'])
        self.assertTrue(status['controls']['capture_allowed'])
        for path in ('/api/capture', '/api/v1/capture'):
            code, receipt = self.request(path, {'source': 'live'}, {'Origin': self.base})
            self.assertEqual(code, 202)
            self.assertEqual(receipt['state'], 'queued')
        old_rules = self.rules.read_bytes()
        self.expect_http(403, '/api/rules', json.loads(old_rules))
        self.expect_http(403, '/api/v1/session/bind', {})
        self.assertEqual(self.rules.read_bytes(), old_rules)

    def test_cross_origin_capture_still_rejected(self):
        self.expect_http(403, '/api/capture', {}, {'Origin': 'http://unrelated.example'})

    def test_config_can_disable_remote_capture(self):
        self.server.settings['allow_remote_capture'] = False
        self.expect_http(403, '/api/capture', {})
        _, status = self.request('/api/status')
        self.assertFalse(status['controls']['capture_allowed'])

    def test_configured_token_remains_required_for_capture(self):
        self.server.token = 'test-token'
        self.expect_http(401, '/api/capture', {})
        self.expect_http(401, '/api/capture', {}, {'Authorization': 'Bearer wrong'})
        code, _ = self.request('/api/capture', {}, {'Authorization': 'Bearer test-token'})
        self.assertEqual(code, 202)

    def test_camera_unavailable_returns_503_not_fake_success(self):
        status = self.settings['data_dir'] / 'captures/live/status.json'
        status.write_text(json.dumps(dict(state='WAITING_CAMERA', updated_at=time.time())))
        self.expect_http(503, '/api/capture', {})

    def test_remote_capture_setting_requires_boolean(self):
        values = json.loads(self.config.read_text())
        values['allow_remote_capture'] = 'false'
        self.config.write_text(json.dumps(values))
        with self.assertRaisesRegex(ValueError, 'allow_remote_capture'):
            load(self.config)
