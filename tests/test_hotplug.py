import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from edge.api import make_server
from edge.settings import load
from hotspot.engine import RadiometricAutoCaptureEngine, celsius_to_centikelvin
from hotspot.live import LiveCamera, resolve_device

ROOT = Path(__file__).resolve().parents[1]


class HotplugTests(unittest.TestCase):
    def test_only_purethermal_capture_node_and_reject_multiple(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sysfs, dev = root / 'class', root / 'dev'
            sysfs.mkdir(); dev.mkdir()
            def add(number, vendor='1e4e', product='0100', index='0'):
                node = dev / f'video{number}'
                node.touch()
                entry = sysfs / node.name
                entry.mkdir()
                (entry / 'index').write_text(index)
                usb = root / f'usb-{number}'
                usb.mkdir()
                (usb / 'idVendor').write_text(vendor)
                (usb / 'idProduct').write_text(product)
                interface = usb / 'interface'
                interface.mkdir()
                (entry / 'device').symlink_to(interface)
                return node
            webcam = add(0, vendor='9999')
            add(1, index='1')  # UVC metadata, not the stream.
            self.assertIsNone(resolve_device('auto', sysfs, dev))
            node = add(4)
            self.assertEqual(resolve_device('auto', sysfs, dev), str(node))
            self.assertIsNone(resolve_device(str(webcam), sysfs, dev))
            stable = root / 'stable-purethermal'
            stable.symlink_to(node)
            self.assertEqual(resolve_device(str(stable), sysfs, dev), str(stable))
            add(6)
            with self.assertRaisesRegex(ValueError, 'Multiple PureThermal'):
                resolve_device('auto', sysfs, dev)
            node.unlink()
            self.assertIsNone(resolve_device(str(stable), sysfs, dev))

    def test_missing_attach_warmup_unplug_and_replug_with_new_node(self):
        clock, present, opened = [0.0], [None], []
        class Stream:
            def __init__(self, device):
                self.device = device
                self.closed = self.failed = False
                self.received_at = 0
                self.received_utc = 100
                self.consumed = self.dropped = 0
                opened.append(self)
            def read(self):
                if self.failed:
                    raise EOFError('unplugged')
                self.consumed += 1
                self.received_at = clock[0]
                return [30000] * 19200
            def close(self):
                self.closed = True
        camera = LiveCamera('auto', 10, factory=Stream,
                            resolver=lambda _: present[0], clock=lambda: clock[0])
        self.assertIsNone(camera.read())
        self.assertEqual(camera.state, 'WAITING_CAMERA')
        self.assertEqual(opened, [])
        present[0], clock[0] = '/dev/video4', 3
        self.assertIsNone(camera.read())
        self.assertEqual(camera.state, 'WARMUP')
        clock[0] = 13
        self.assertEqual(len(camera.read()), 19200)
        sequence_before_unplug = camera.consumed
        opened[0].failed = True
        self.assertIsNone(camera.read())
        self.assertTrue(opened[0].closed)
        self.assertEqual(camera.state, 'CAMERA_ERROR')
        self.assertIsNone(camera.received_at)
        present[0], clock[0] = None, 16
        self.assertIsNone(camera.read())
        self.assertEqual(camera.state, 'WAITING_CAMERA')
        present[0], clock[0] = '/dev/video8', 19
        self.assertIsNone(camera.read())
        self.assertEqual(camera.state, 'WARMUP')
        self.assertEqual(len(opened), 2)
        clock[0] = 29
        self.assertEqual(len(camera.read()), 19200)
        self.assertEqual(camera.selected_device, '/dev/video8')
        self.assertGreater(camera.consumed, sequence_before_unplug)
        camera.close()
        self.assertTrue(opened[1].closed)

    def test_failed_open_backs_off_instead_of_busy_loop(self):
        clock, attempts = [0.0], []
        def fail(device):
            attempts.append(device)
            raise PermissionError('video group required')
        camera = LiveCamera('auto', 10, factory=fail, resolver=lambda _: '/dev/video0',
                            clock=lambda: clock[0])
        self.assertIsNone(camera.read())
        self.assertEqual(camera.state, 'CAMERA_ERROR')
        self.assertIn('video group', camera.error)
        for _ in range(20):
            self.assertIsNone(camera.read())
        self.assertEqual(len(attempts), 1)
        clock[0] = 3
        camera.read()
        self.assertEqual(len(attempts), 2)

    def test_waiting_worker_handles_sigterm_and_releases_camera_lock(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'rules.json').write_bytes((ROOT / 'config/landfill.json').read_bytes())
            config = root / 'edge.json'
            config.write_text(json.dumps(dict(data_dir='data', rules='rules.json', source='live',
                device='/dev/tilik-missing-camera-for-test', confirm_centikelvin=True,
                host='127.0.0.1', port=0)))
            with (root / 'worker.log').open('w') as log:
                worker = subprocess.Popen([sys.executable, '-m', 'edge', 'capture', '--config', str(config)],
                    cwd=ROOT, stdout=log, stderr=log)
                try:
                    status_path = root / 'data/captures/live/status.json'
                    deadline = time.monotonic() + 5
                    while not status_path.exists() and worker.poll() is None and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertIsNone(worker.poll(), (root / 'worker.log').read_text())
                    self.assertEqual(json.loads(status_path.read_text())['state'], 'WAITING_CAMERA')
                    worker.terminate()
                    self.assertEqual(worker.wait(timeout=10), 0)
                    self.assertEqual(json.loads(status_path.read_text())['state'], 'STOPPED')
                    self.assertEqual(list(status_path.parent.glob('*/metadata.json')), [])
                    with (root / 'data/captures/.capture.lock').open('a') as lock:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    if worker.poll() is None:
                        worker.kill()
                        worker.wait()

    def test_rule_persistence_does_not_bridge_usb_disconnect(self):
        from types import SimpleNamespace
        from hotspot.__main__ import run
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = json.loads((ROOT / 'config/landfill.json').read_text())
            rules.update(debounce_frames=2, persistence_seconds=0, cooldown_seconds=0)
            (root / 'rules.json').write_text(json.dumps(rules))
            raw = [celsius_to_centikelvin(25)] * 19200
            for y in range(20, 30):
                for x in range(20, 30): raw[y * 160 + x] = celsius_to_centikelvin(75)
            class Connection:
                def __init__(self, *args):
                    self.frames = iter((raw, None, raw, raw))
                    self.received_at = time.monotonic()
                    self.received_utc = time.time()
                    self.consumed = self.dropped = 0
                    self.error = self.selected_device = None
                def read(self):
                    value = next(self.frames)
                    self.received_at += 1
                    self.received_utc += 1
                    self.consumed += 1
                    self.state = 'WAITING_CAMERA' if value is None else 'STREAMING'
                    return value
                def close(self): pass
            args = SimpleNamespace(config=root / 'rules.json', output=root / 'captures',
                source='live', confirm_centikelvin=True, warmup_seconds=0, frames=3,
                min_free_mb=0, device='auto', device_id='test-pi')
            with patch('hotspot.__main__.LiveCamera', Connection), patch('hotspot.__main__.signal.signal'):
                self.assertEqual(run(args), 0)
            captures = list((root / 'captures/live').glob('*/metadata.json'))
            self.assertEqual(len(captures), 1)
            metadata = json.loads(captures[0].read_text())
            self.assertEqual(metadata['source'], 'live')
            self.assertEqual(metadata['frame_timing']['frame_sequence'], 4)
            self.assertEqual(metadata['trigger_kind'], 'rule')

    def test_api_stays_available_and_retains_evidence_when_camera_absent(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'TILIK_API_TOKEN': ''}):
            root = Path(directory)
            (root / 'rules.json').write_bytes((ROOT / 'config/landfill.json').read_bytes())
            config = root / 'edge.json'
            config.write_text(json.dumps(dict(data_dir='data', rules='rules.json', source='live',
                device='auto', confirm_centikelvin=True, host='127.0.0.1', port=0)))
            settings = load(config)
            engine = RadiometricAutoCaptureEngine(output_dir=str(root / 'data/captures/live'),
                source='live', persistence_seconds=0, debounce_frames=1)
            engine.device_id = settings['device_id']
            raw = [celsius_to_centikelvin(25)] * 19200
            for y in range(20, 30):
                for x in range(20, 30): raw[y * 160 + x] = celsius_to_centikelvin(75)
            saved = Path(engine.process_frame(raw)['capture_path'])
            original = {p.name: p.read_bytes() for p in saved.iterdir()}
            with patch('edge.api.advertise', return_value=None):
                server = make_server(settings)
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                for state in ('WAITING_CAMERA', 'CAMERA_ERROR', 'WARMUP', 'STOPPED'):
                    (saved.parent / 'status.json').write_text(json.dumps(dict(
                        state=state, updated_at=time.time(), source='live')))
                    status = json.load(urlopen(base + '/api/status'))
                    self.assertTrue(status['fresh'])
                    self.assertEqual(status['camera']['connected'], state == 'WARMUP')
                    self.assertFalse(status['camera']['ready'])
                    captures = json.load(urlopen(base + '/api/v1/captures'))['items']
                    self.assertEqual(len(captures), 1)
                    capture_id = captures[0]['id']
                    image = urlopen(base + f'/api/captures/{capture_id}/preview').read()
                    self.assertEqual(image, original['frame_visual.png'])
                    for request in (base + '/api/preview', Request(base + '/api/capture',
                            data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')):
                        with self.assertRaises(HTTPError) as error:
                            urlopen(request)
                        self.assertEqual(error.exception.code, 503)
                        error.exception.close()
                (saved.parent / 'status.json').write_text(json.dumps(dict(
                    state='IDLE', updated_at=time.time(), source='live')))
                status = json.load(urlopen(base + '/api/status'))
                self.assertTrue(status['camera']['ready'])
                self.assertTrue(status['camera']['connected'])
                self.assertEqual(original, {p.name: p.read_bytes() for p in saved.iterdir()})
            finally:
                server.shutdown()
                worker.join()
                server.stopping.set()
                server.scanner.join()
                server.server_close()
                server.catalog.db.close()
