import hmac
import json
import os
import ssl
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from hotspot.__main__ import FIELDS, atomic_json
from hotspot.engine import RadiometricAutoCaptureEngine
from .storage import Catalog, digest
from .commands import Commands
from .discovery import advertise


class EdgeHTTPServer(ThreadingHTTPServer):
    advertisement = None

    def server_close(self):
        process = self.advertisement
        self.advertisement = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except Exception:
                process.kill()
                process.wait()
        super().server_close()


def serve(settings):
    server = make_server(settings)
    try:
        server.serve_forever()
    finally:
        server.stopping.set()
        server.scanner.join()
        server.server_close()
        server.catalog.db.close()


def make_server(settings):
    catalog = Catalog(settings)
    catalog.refresh()
    server = EdgeHTTPServer((settings['host'], settings['port']), Handler)
    if settings.get('tls_cert') and settings.get('tls_key'):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(settings['tls_cert'], settings['tls_key'])
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.settings, server.catalog = settings, catalog
    server.commands = Commands(settings['data_dir'])
    server.token = os.environ.get('TILIK_API_TOKEN', '')
    server.rules_lock = threading.Lock()
    server.timeout = .5
    server.stopping = threading.Event()
    def scan():
        while not server.stopping.wait(2):
            try:
                catalog.refresh()
            except Exception as error:
                catalog.errors = [str(error)]
    server.scanner = threading.Thread(target=scan, daemon=True)
    server.scanner.start()
    server.advertisement = advertise(settings, server.server_port)
    return server


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, value, code=200, mime='application/json', headers=None):
        body = value if isinstance(value, bytes) else json.dumps(value, allow_nan=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for key, value in (headers or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        token = self.server.token
        if token and not hmac.compare_digest(self.headers.get('Authorization', '').encode('utf-8'), ('Bearer ' + token).encode('utf-8')):
            self.send({'error': 'Authentication required'}, 401)
            return False
        return True

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == '/':
            self.send(Path(__file__).with_name('dashboard.html').read_bytes(), mime='text/html; charset=utf-8')
            return
        if not self.authorized():
            return
        path = parsed.path.rstrip('/')
        versioned = path.startswith('/api/v1/')
        path = path.replace('/api/v1/', '/api/', 1)
        settings, catalog = self.server.settings, self.server.catalog
        try:
            if path.startswith('/api/requests/'):
                receipt = self.server.commands.get(path.split('/')[-1])
                if receipt['state'] == 'saved':
                    try:
                        catalog.get(receipt['capture_id'])
                    except FileNotFoundError:
                        receipt['state'] = 'indexing'
                self.send(receipt)
            elif path == '/api/preview':
                status_path = settings['data_dir'] / 'captures' / settings['source'] / 'status.json'
                status = json.loads(status_path.read_text()) if status_path.exists() else {}
                if time.time() - status.get('updated_at', 0) > 2 or status.get('state') in ('STOPPED', 'ERROR', 'WARMUP', 'INVALID_FRAME'):
                    self.send({'error': 'No fresh preview. Check capture service status.'}, 503)
                    return
                image_path = status_path.with_name('preview.png')
                image_time = image_path.stat().st_mtime
                if time.time() - image_time > 2:
                    self.send({'error': 'Preview stale'}, 503)
                    return
                blob = image_path.read_bytes()
                tag = '"' + digest(blob) + '"'
                headers = {'ETag': tag, 'X-Preview-Age-Ms': round((time.time()-image_time)*1000)}
                self.send(b'' if self.headers.get('If-None-Match') == tag else blob,
                          304 if self.headers.get('If-None-Match') == tag else 200,
                          'image/png', headers)
            elif path == '/api/status':
                status_path = settings['data_dir'] / 'captures' / settings['source'] / 'status.json'
                status = json.loads(status_path.read_text()) if status_path.exists() else {}
                fresh = time.time() - status.get('updated_at', 0) < 5
                connected = (settings['source'] == 'live' and fresh and status.get('state') not in ('ERROR', 'STOPPED'))
                self.send({'server': 'Tilik Edge', 'version': 2, 'device_id': settings['device_id'],
                    'source': settings['source'], 'camera': {'connected': connected if fresh else None},
                    'capture': status, 'fresh': fresh, 'catalog_errors': catalog.errors,
                    'controls': {'token_required': bool(self.server.token),
                                 'allowed': bool(self.server.token) or self.client_address[0] in ('127.0.0.1', '::1')}})
            elif path == '/api/rules':
                self.send(json.loads(settings['rules'].read_text()))
            elif path == '/api/captures':
                if versioned:
                    q = parse_qs(parsed.query)
                    after = max(0, int(q.get('after', ['0'])[0]))
                    limit = min(200, max(1, int(q.get('limit', ['100'])[0])))
                    snapshot = int(q['snapshot'][0]) if 'snapshot' in q else None
                    self.send(catalog.list(after, limit, snapshot))
                else:
                    # Compatibility for the existing POC iOS decoder (array, newest first).
                    items, cursor = [], 0
                    snapshot = catalog.list(limit=1)['snapshot']
                    while cursor < snapshot:
                        page = catalog.list(cursor, 200, snapshot)
                        if not page['items']:
                            break
                        items.extend(page['items'])
                        cursor = page['next_cursor']
                    self.send(list(reversed(items)))
            else:
                parts = path.strip('/').split('/')
                if len(parts) not in (3, 4) or parts[:2] != ['api', 'captures']:
                    self.send({'error': 'Not found'}, 404)
                    return
                cid = parts[2]
                role = parts[3] if len(parts) == 4 else 'metadata'
                role = 'preview' if role == 'visual' else role
                if role == 'manifest':
                    self.send(catalog.get(cid))
                elif role in ('metadata', 'preview', 'raw'):
                    self.send(catalog.artifact(cid, role), mime={'metadata': 'application/json', 'preview': 'image/png', 'raw': 'application/octet-stream'}[role])
                else:
                    self.send({'error': 'Not found'}, 404)
        except FileNotFoundError:
            self.send({'error': 'Capture unavailable'}, 404)
        except (ValueError, KeyError, OSError, TypeError) as error:
            self.send({'error': str(error)}, 409)

    def do_POST(self):
        if not self.authorized():
            return
        # Without a configured token, control is local-only. No CORS bypass.
        if not self.server.token and self.client_address[0] not in ('127.0.0.1', '::1'):
            self.send({'error': 'Rule changes require local access or API token'}, 403)
            return
        origin = self.headers.get('Origin')
        scheme = 'https://' if self.server.settings.get('tls_cert') else 'http://'
        if origin and origin != scheme + self.headers.get('Host', ''):
            self.send({'error': 'Origin rejected'}, 403)
            return
        if self.path not in ('/api/rules', '/api/v1/rules', '/api/capture', '/api/v1/capture'):
            self.send({'error': 'Use the capture service to generate captures'}, 405)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 8192:
                raise ValueError('Invalid request size')
            config = json.loads(self.rfile.read(size))
            if self.path in ('/api/capture', '/api/v1/capture'):
                if not isinstance(config, dict):
                    raise ValueError('Request must be a JSON object')
                settings = self.server.settings
                source = settings['source']
                request_id = str(uuid.UUID(config.get('request_id', str(uuid.uuid4()))))
                try:
                    existing = self.server.commands.get(request_id)
                except FileNotFoundError:
                    existing = None
                if existing:
                    if existing['source'] != source:
                        raise ValueError('Request belongs to a different source')
                    self.send(existing, 202)
                    return
                if config.get('source', source) != source:
                    raise ValueError('Capture source changed. Reconnect before requesting a capture.')
                status_path = settings['data_dir'] / 'captures' / source / 'status.json'
                status = json.loads(status_path.read_text()) if status_path.exists() else {}
                if time.time() - status.get('updated_at', 0) > 2 or status.get('state') in (None, 'ERROR', 'STOPPED', 'WARMUP', 'INVALID_FRAME'):
                    self.send({'error': 'Capture worker is not ready. Start it and wait for warmup.'}, 503)
                    return
                receipt = self.server.commands.submit(request_id, source)
                self.send(receipt, 202)
                return
            if not isinstance(config, dict) or set(config) != FIELDS:
                raise ValueError('Rules must contain exactly the supported fields')
            # The real engine validates the proposed values; no camera is opened.
            RadiometricAutoCaptureEngine(output_dir=str(self.server.settings['data_dir'] / '.validation'), **config)
            with self.server.rules_lock:
                atomic_json(self.server.settings['rules'], config)
            self.send({'saved': True, 'message': 'Capture service applies rules on the next frame and resets persistence.'})
        except (ValueError, TypeError, OSError, OverflowError) as error:
            self.send({'error': str(error)}, 400)
