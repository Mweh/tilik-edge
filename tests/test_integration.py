import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from edge.api import make_server
from edge.storage import Catalog
from edge.settings import load
from edge.import_legacy import migrate
from hotspot.engine import RadiometricAutoCaptureEngine, celsius_to_centikelvin

ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / 'edge.json'
        self.rules = self.root / 'rules.json'
        self.rules.write_bytes((ROOT/'config/landfill.json').read_bytes())
        self.config.write_text(json.dumps({'data_dir':'data', 'rules':'rules.json', 'source':'simulation', 'device':'/dev/video0', 'confirm_centikelvin':False, 'host':'127.0.0.1', 'port':0}))
        self.settings = load(self.config)
        self.catalog = Catalog(self.settings)

    def tearDown(self):
        self.catalog.db.close()
        self.tmp.cleanup()

    def capture(self, pending=False):
        engine = RadiometricAutoCaptureEngine(output_dir=str(self.catalog.root/'simulation'), source='simulation', cooldown_seconds=0, persistence_seconds=0, debounce_frames=1)
        engine.device_id = self.settings['device_id']
        raw = [celsius_to_centikelvin(25)]*19200
        for y in range(20,30):
            for x in range(20,30):
                raw[y*160+x] = celsius_to_centikelvin(75)
        result = engine.process_frame(raw)
        path = Path(result['capture_path'])
        if pending:
            renamed = path.with_name('.'+path.name+'.pending')
            path.rename(renamed)
            path = renamed
        return path

    def test_pending_never_listed_and_identity_survives_move_restart(self):
        path = self.capture(pending=True)
        self.catalog.refresh()
        self.assertEqual(self.catalog.list()['items'], [])
        published = path.with_name('published')
        path.rename(published)
        self.catalog.refresh()
        first = self.catalog.list()['items'][0]
        self.assertTrue(first['mock'])
        self.assertEqual(first['max_temp_c'],75)
        published.rename(published.with_name('relocated'))
        self.catalog.refresh()
        self.assertEqual(self.catalog.list()['items'][0]['id'],first['id'])
        self.assertEqual(len(self.catalog.list()['items']),1)
        self.assertEqual(len(self.catalog.artifact(first['id'],'raw')),38400)
        restarted = Catalog(load(self.config))
        try:
            restarted.refresh()
            self.assertEqual(restarted.list()['items'][0]['id'],first['id'])
        finally:
            restarted.db.close()

    def test_incomplete_png_and_uncalibrated_raw_rejected(self):
        path = self.capture()
        image = path/'frame_visual.png'
        original = image.read_bytes()
        image.write_bytes(original[:8])
        self.catalog.refresh()
        self.assertEqual(self.catalog.list()['items'],[])
        image.write_bytes(original)
        meta = json.loads((path/'metadata.json').read_text())
        meta['radiometry']['valid'] = False
        (path/'metadata.json').write_text(json.dumps(meta))
        self.catalog.refresh()
        self.assertEqual(self.catalog.list()['items'],[])

    def test_integrity_mismatch_is_not_served(self):
        path = self.capture()
        self.catalog.refresh()
        item = self.catalog.list()['items'][0]
        (path/'frame_raw.bin').write_bytes(bytes(38400))
        self.catalog.refresh()
        self.assertIn('Immutable capture changed',str(self.catalog.errors))
        with self.assertRaises(ValueError):
            self.catalog.artifact(item['id'],'raw')

    def test_snapshot_and_pagination_no_loss(self):
        self.capture(); self.capture()
        self.catalog.refresh()
        page = self.catalog.list(limit=1)
        self.capture(); self.catalog.refresh()
        next_page = self.catalog.list(page['next_cursor'],1,page['snapshot'])
        self.assertEqual(len(next_page['items']),1)
        self.assertEqual(next_page['snapshot'],page['snapshot'])
        later = self.catalog.list(next_page['next_cursor'])
        self.assertEqual(len(later['items']),1)

    def test_deleted_bundles_are_hidden_without_losing_snapshot_or_identity(self):
        import shutil
        path = self.capture()
        self.catalog.refresh()
        original = self.catalog.list()['items'][0]
        snapshot = self.catalog.list()['snapshot']
        backup = self.root / 'backup'
        shutil.copytree(path, backup)
        shutil.rmtree(path)
        self.catalog.refresh()
        page = self.catalog.list(snapshot=snapshot)
        self.assertEqual(page['items'], [])
        self.assertEqual(page['snapshot'], snapshot)
        self.assertEqual(page['next_cursor'], snapshot)
        shutil.copytree(backup, path)
        self.catalog.refresh()
        self.assertEqual(self.catalog.list()['items'][0]['id'], original['id'])
        self.assertEqual(self.catalog.list()['items'][0]['sequence'], original['sequence'])

    def test_pagination_advances_over_retired_tail_records(self):
        import shutil
        self.capture()
        tail = self.capture()
        self.catalog.refresh()
        snapshot = self.catalog.list()['snapshot']
        shutil.rmtree(tail)
        self.catalog.refresh()
        page = self.catalog.list(limit=1, snapshot=snapshot)
        self.assertEqual(len(page['items']), 1)
        self.assertEqual(page['next_cursor'], snapshot)
        self.assertEqual(self.catalog.list(page['next_cursor'], 1, snapshot)['items'], [])

    def test_corrupt_existing_capture_is_not_offered_as_downloadable(self):
        path = self.capture()
        self.catalog.refresh()
        (path / 'frame_visual.png').write_bytes(b'broken')
        self.catalog.refresh()
        self.assertEqual(self.catalog.list()['items'], [])
        self.assertIn('Incomplete or unsupported PNG', str(self.catalog.errors))

    def test_old_catalog_schema_is_migrated_without_losing_rows(self):
        import sqlite3
        self.capture()
        self.catalog.refresh()
        old = self.catalog.list()['items'][0]
        self.catalog.db.close()
        database = self.settings['data_dir'] / 'catalog.sqlite'
        database.unlink()
        connection = sqlite3.connect(database)
        connection.execute('CREATE TABLE captures (sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, manifest TEXT NOT NULL)')
        connection.execute('INSERT INTO captures(id,manifest) VALUES (?,?)', (old['id'], json.dumps(old)))
        connection.commit(); connection.close()
        self.catalog = Catalog(self.settings)
        self.catalog.refresh()
        self.assertEqual(self.catalog.list()['items'][0]['id'], old['id'])

    def test_import_legacy_idempotent_and_preserves_original(self):
        path = self.capture()
        legacy = self.root/'legacy'; legacy.mkdir()
        import shutil
        copied = legacy/'event'; shutil.copytree(path,copied)
        meta = json.loads((copied/'metadata.json').read_text())
        del meta['radiometry']
        (copied/'metadata.json').write_text(json.dumps(meta))
        before = (copied/'metadata.json').read_bytes()
        rejected = migrate(legacy,self.settings)
        self.assertEqual(len(rejected['errors']),1)
        result = migrate(legacy,self.settings,confirm=True)
        self.assertEqual(result['imported'],1)
        self.assertEqual(migrate(legacy,self.settings,confirm=True)['already_present'],1)
        self.assertEqual((copied/'metadata.json').read_bytes(),before)

    def test_http_compatibility_rules_auth_and_no_camera_dependency(self):
        self.capture()
        server = make_server(self.settings)
        thread = threading.Thread(target=server.serve_forever);thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        def fetch(path, data=None, token=None):
            headers = {'Content-Type':'application/json'}
            if token: headers['Authorization']='Bearer '+token
            with urlopen(Request(base+path,data=data,headers=headers),timeout=3) as response:
                return response.read()
        try:
            items = json.loads(fetch('/api/captures'))
            self.assertIsInstance(items,list)
            cid = items[0]['id']
            self.assertEqual(len(fetch('/api/captures/'+cid+'/raw')),38400)
            self.assertEqual(json.loads(fetch('/api/captures/'+cid))['source'],'simulation')
            self.assertIsNone(json.loads(fetch('/api/status'))['camera']['connected'])
            self.assertTrue(fetch('/').startswith(b'<!doctype html>'))
            rules = json.loads(fetch('/api/rules'));rules['custom_threshold']=31
            self.assertTrue(json.loads(fetch('/api/rules',json.dumps(rules).encode()))['saved'])
            self.assertEqual(json.loads(self.rules.read_text())['custom_threshold'],31)
            server.token='test-secret'
            with self.assertRaises(HTTPError) as err:
                fetch('/api/captures')
            self.assertEqual(err.exception.code,401);err.exception.close()
            self.assertEqual(len(json.loads(fetch('/api/captures',token='test-secret'))),1)
        finally:
            server.shutdown();thread.join();server.stopping.set();server.scanner.join();server.server_close();server.catalog.db.close()

    def test_malformed_bundle_does_not_block_good_capture(self):
        bad = self.capture()
        (bad/'metadata.json').write_text('[]')
        self.capture()
        self.catalog.refresh()
        self.assertEqual(len(self.catalog.list()['items']), 1)
        self.assertIn('Metadata must be an object', str(self.catalog.errors))

    def test_import_does_not_override_declared_invalid_encoding(self):
        import shutil
        legacy = self.root/'legacy'; legacy.mkdir()
        path = self.capture()
        copied = legacy/'event'; shutil.copytree(path, copied)
        meta = json.loads((copied/'metadata.json').read_text())
        meta['radiometry']['encoding'] = 'unknown'
        (copied/'metadata.json').write_text(json.dumps(meta))
        result = migrate(legacy, self.settings, confirm=True)
        self.assertEqual(result['imported'], 0)
        self.assertEqual(len(result['errors']), 1)

    def test_one_command_starts_capture_and_api_and_stops_cleanly(self):
        import socket
        with socket.socket() as available:
            available.bind(('127.0.0.1', 0))
            port = available.getsockname()[1]
        config = json.loads(self.config.read_text()); config['port'] = port
        self.config.write_text(json.dumps(config))
        process = subprocess.Popen(['/bin/sh', str(ROOT/'start.sh'), str(self.config)],
            cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        endpoint = f'http://127.0.0.1:{port}'
        try:
            deadline = time.monotonic() + 18
            items = []
            while time.monotonic() < deadline:
                try:
                    with urlopen(endpoint+'/api/captures', timeout=1) as response:
                        items = json.loads(response.read())
                    if items:
                        break
                except OSError:
                    pass
                if process.poll() is not None:
                    self.fail(process.communicate()[1].decode())
                time.sleep(.2)
            self.assertTrue(items, 'Combined launcher did not publish a capture')
            cid = items[0]['id']
            with urlopen(endpoint+'/api/captures/'+cid+'/manifest') as response:
                manifest = json.loads(response.read())
            from edge.storage import digest
            for role in ('preview', 'metadata', 'raw'):
                with urlopen(endpoint+'/api/captures/'+cid+'/'+role) as response:
                    blob = response.read()
                self.assertEqual(digest(blob), manifest['artifacts'][role]['sha256'])
        finally:
            process.terminate()
            process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0)
        with self.assertRaises(OSError):
            urlopen(endpoint+'/api/status', timeout=1)

    def test_manual_capture_preview_and_stopped_worker(self):
        import uuid
        rules = json.loads(self.rules.read_text()); rules['custom_threshold'] = 300
        self.rules.write_text(json.dumps(rules))
        process = subprocess.Popen([sys.executable,'-m','edge','capture','--config',str(self.config)],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        server = make_server(self.settings)
        thread = threading.Thread(target=server.serve_forever); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        def request(path, body=None, headers=None):
            payload = None if body is None else json.dumps(body).encode()
            return urlopen(Request(base+path, data=payload, headers=headers or {}), timeout=3)
        try:
            deadline = time.monotonic()+5
            while time.monotonic()<deadline:
                with request('/api/status') as response:
                    status = json.loads(response.read())
                if status['fresh'] and status['capture'].get('frames_processed',0)>0:
                    break
                time.sleep(.1)
            self.assertTrue(status['fresh'])
            self.assertNotIn('image',status['capture'])
            with request('/api/preview') as response:
                self.assertTrue(response.read().startswith(b'\x89PNG'))
                self.assertIn('X-Preview-Age-Ms',response.headers)
            request_id = str(uuid.uuid4())
            for _ in range(2):
                with request('/api/capture', {'request_id':request_id,'source':'simulation'}) as response:
                    self.assertEqual(response.status,202)
            deadline = time.monotonic()+6
            while time.monotonic()<deadline:
                with request('/api/requests/'+request_id) as response:
                    receipt = json.loads(response.read())
                if receipt['state']=='saved': break
                time.sleep(.1)
            self.assertEqual(receipt['state'],'saved')
            with request('/api/captures') as response:
                captures = json.loads(response.read())
            self.assertEqual(len(captures),1)
            self.assertEqual(captures[0]['trigger_kind'],'manual_test')
            self.assertTrue(captures[0]['mock'])
            with request('/api/captures/'+receipt['capture_id']) as response:
                meta = json.loads(response.read())
            self.assertEqual(meta['request_id'],request_id)
            self.assertEqual(meta['temperature_threshold_celsius'],300)
            process.terminate(); process.communicate(timeout=10)
            with self.assertRaises(HTTPError) as err:
                request('/api/capture', {'request_id':str(uuid.uuid4())})
            self.assertEqual(err.exception.code,503);err.exception.close()
            with self.assertRaises(HTTPError) as err:
                request('/api/preview')
            self.assertEqual(err.exception.code,503);err.exception.close()
            with request('/api/capture', {'request_id':request_id}) as response:
                self.assertEqual(json.loads(response.read())['state'],'saved')
        finally:
            if process.poll() is None:process.terminate()
            process.communicate(timeout=10)
            server.shutdown();thread.join();server.stopping.set();server.scanner.join();server.server_close();server.catalog.db.close()

    def test_capture_without_browser_and_hot_reload(self):
        process = subprocess.Popen([sys.executable,'-m','edge','capture','--config',str(self.config)],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            status = self.settings['data_dir']/'captures/simulation/status.json'
            deadline = time.monotonic()+15
            found = False
            while time.monotonic()<deadline:
                if status.exists():
                    data = json.loads(status.read_text())
                    if data.get('captures',0)>=1:
                        found = True;break
                if process.poll() is not None:
                    self.fail(process.stderr.read().decode())
                time.sleep(.15)
            self.assertTrue(found,'No capture generated without a browser')
            rules = json.loads(self.rules.read_text());rules['delta_celsius']=8
            temp = self.rules.with_suffix('.tmp');temp.write_text(json.dumps(rules));temp.replace(self.rules)
            deadline = time.monotonic()+3
            while time.monotonic()<deadline:
                if json.loads(status.read_text()).get('rule_config',{}).get('delta_celsius')==8:break
                time.sleep(.1)
            self.assertEqual(json.loads(status.read_text())['rule_config']['delta_celsius'],8)
            self.catalog.refresh()
            self.assertGreaterEqual(len(self.catalog.list()['items']),1)
        finally:
            process.terminate();process.communicate(timeout=10)
