import json
import tempfile
import unittest
import uuid
from pathlib import Path
from edge.sessions import Sessions
from hotspot.engine import RadiometricAutoCaptureEngine, celsius_to_centikelvin


class SessionTimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sessions = Sessions(self.root, 'pi', boot='boot')
        self.request = dict(session_id=str(uuid.uuid4()), device_id='pi', boot_id='boot',
                            clock_mapping_id='clock', calibration_id='mount')

    def tearDown(self):
        self.tmp.cleanup()

    def test_idempotent_ack_and_finalize(self):
        first = self.sessions.bind(self.request, now=10)
        self.assertEqual(first, self.sessions.bind(self.request, now=11))
        end = self.sessions.finalize(self.request, now=20)
        self.assertEqual(end, self.sessions.finalize(self.request, now=21))
        with self.assertRaises(ValueError):
            self.sessions.bind(self.request, now=22)

    def test_acquisition_time_owns_session_not_processing_or_download(self):
        self.sessions.bind(self.request, now=10)
        self.sessions.finalize(self.request, now=20)
        second = dict(self.request, session_id=str(uuid.uuid4()))
        self.sessions.bind(second, now=30)
        self.assertEqual(self.sessions.frame_timing(15, 1, False)['session_id'], self.request['session_id'])
        self.assertIsNone(self.sessions.frame_timing(25, 2, False)['session_id'])
        self.assertEqual(self.sessions.frame_timing(35, 3, False)['session_id'], second['session_id'])

    def test_reboot_never_inherits_ack(self):
        self.sessions.bind(self.request, now=10)
        rebooted = Sessions(self.root, 'pi', boot='new-boot')
        self.assertFalse(rebooted.frame_timing(15, 1, False)['session_acknowledged'])
        with self.assertRaises(ValueError):
            rebooted.bind(self.request, now=20)
        with self.assertRaises(ValueError):
            self.sessions.bind(dict(self.request, device_id='another-pi'), now=20)

    def test_conflicting_sortie_does_not_replace_active(self):
        self.sessions.bind(self.request, now=10)
        with self.assertRaises(ValueError):
            self.sessions.bind(dict(self.request, session_id=str(uuid.uuid4())), now=11)
        with self.assertRaises(ValueError):
            self.sessions.bind(dict(self.request, calibration_id='changed'), now=11)

    def test_capture_persists_frame_envelope_before_hashing(self):
        self.sessions.bind(self.request, now=10)
        timing = self.sessions.frame_timing(12, 42, True)
        engine = RadiometricAutoCaptureEngine(output_dir=str(self.root / 'captures'), source='simulation',
            persistence_seconds=0, debounce_frames=1, cooldown_seconds=0)
        raw = [celsius_to_centikelvin(30)] * 19200
        for y in range(10, 20):
            for x in range(10, 20):
                raw[y * 160 + x] = celsius_to_centikelvin(80)
        result = engine.process_frame(raw, timestamp=1234567890, clock=12, frame_timing=timing)
        metadata = json.loads((Path(result['capture_path']) / 'metadata.json').read_text())
        self.assertEqual(metadata['unix_timestamp'], 1234567890)
        self.assertEqual(metadata['frame_timing'], timing)
        self.assertEqual(metadata['session_id'], self.request['session_id'])
        self.assertIsNone(metadata['frame_timing']['receive_to_exposure_uncertainty_seconds'])


class SessionHTTPTests(unittest.TestCase):
    def test_authorized_clock_binding_and_durable_inventory(self):
        import os
        import threading
        from unittest.mock import patch
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        from edge.api import make_server
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules = root / 'rules.json'
            rules.write_text('{}')
            settings = dict(data_dir=root, device_id='pi', host='127.0.0.1', port=0,
                            source='simulation', rules=rules)
            with patch.dict(os.environ, {'TILIK_API_TOKEN': 'test-token'}):
                server = make_server(settings)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = 'http://127.0.0.1:' + str(server.server_port)
            def post(path, body, token='test-token', origin=None):
                headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
                if origin:
                    headers['Origin'] = origin
                return json.loads(urlopen(Request(base + path, json.dumps(body).encode(), headers), timeout=3).read())
            try:
                with self.assertRaises(HTTPError) as denied:
                    post('/api/v1/clock', {}, token='wrong')
                self.assertEqual(denied.exception.code, 401)
                denied.exception.close()
                with self.assertRaises(HTTPError) as cross_origin:
                    post('/api/v1/clock', {}, origin='http://untrusted.invalid')
                self.assertEqual(cross_origin.exception.code, 403)
                cross_origin.exception.close()
                clock = post('/api/v1/clock', {})
                self.assertLessEqual(clock['receive_monotonic_seconds'], clock['send_monotonic_seconds'])
                body = dict(session_id=str(uuid.uuid4()), device_id='pi', boot_id=clock['boot_id'],
                            clock_mapping_id='clock', calibration_id='mount')
                first = post('/api/v1/session/bind', body)
                self.assertEqual(first, post('/api/v1/session/bind', body))
                final = post('/api/v1/session/finalize', body)
                path = '/api/v1/session/' + body['session_id']
                def inventory():
                    return json.loads(urlopen(Request(base + path,
                        headers={'Authorization': 'Bearer test-token'}), timeout=3).read())
                self.assertFalse(inventory()['isFinalized'])
                status = root / 'captures/simulation/status.json'
                status.parent.mkdir(parents=True, exist_ok=True)
                status.write_text(json.dumps(dict(boot_id=clock['boot_id'],
                    processed_monotonic_seconds=final['finalized_monotonic_seconds'] + 1)))
                receipt = inventory()
                self.assertTrue(receipt['isFinalized'])
                self.assertEqual(receipt['captureIDs'], [])
                status.unlink()
                self.assertEqual(receipt, inventory())
            finally:
                server.stopping.set()
                server.scanner.join()
                server.shutdown()
                thread.join()
                server.server_close()
                server.catalog.db.close()


if __name__ == '__main__':
    unittest.main()
