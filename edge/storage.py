"""Catalog committed bundles. API requests never traverse the capture tree."""
import hashlib
import json
import math
import sqlite3
import struct
import threading
import zlib
from pathlib import Path


def digest(blob):
    return hashlib.sha256(blob).hexdigest()


def valid_png(blob):
    if blob[:8] != b'\x89PNG\r\n\x1a\n':
        return False
    pos, first, compressed = 8, True, bytearray()
    try:
        while pos < len(blob):
            size = struct.unpack('>I', blob[pos:pos+4])[0]
            kind = blob[pos+4:pos+8]
            data = blob[pos+8:pos+8+size]
            crc = struct.unpack('>I', blob[pos+8+size:pos+12+size])[0]
            if len(data) != size or zlib.crc32(kind + data) & 0xffffffff != crc:
                return False
            if first:
                if kind != b'IHDR' or size != 13:
                    return False
                width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', data)
                if (width, height) != (160, 120) or depth != 8 or color not in (0, 2, 6) or (compression, filtering, interlace) != (0, 0, 0):
                    return False
                first = False
            if kind == b'IDAT':
                compressed.extend(data)
            pos += 12 + size
            if kind == b'IEND':
                expected = height * (1 + width * {0:1, 2:3, 6:4}[color])
                decoder = zlib.decompressobj()
                pixels = decoder.decompress(bytes(compressed), expected + 1)
                stride = 1 + width * {0:1, 2:3, 6:4}[color]
                return (size == 0 and pos == len(blob) and len(pixels) == expected
                        and decoder.eof and not decoder.unused_data
                        and all(pixels[row * stride] <= 4 for row in range(height)))
    except (ValueError, struct.error, zlib.error, UnboundLocalError):
        pass
    return False


class Catalog:
    def __init__(self, settings):
        self.root = settings['data_dir'] / 'captures'
        self.root.mkdir(exist_ok=True)
        self.device_id = settings['device_id']
        self.lock = threading.RLock()
        self.db = sqlite3.connect(settings['data_dir'] / 'catalog.sqlite', check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS captures (sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, manifest TEXT NOT NULL)')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(captures)')}
        if 'available' not in columns:
            self.db.execute('ALTER TABLE captures ADD COLUMN available INTEGER NOT NULL DEFAULT 1')
        self.db.commit()
        self.seen = {}
        self.errors = []

    def safe(self, path):
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root.resolve()) or path.is_symlink():
            raise ValueError('Artifact outside capture store')
        return resolved

    def refresh(self):
        errors = []
        # Availability belongs to the derived catalog. Keep IDs/sequences as tombstones so
        # an in-flight pagination snapshot stays stable when a folder is moved or removed.
        with self.lock:
            known = self.db.execute('SELECT id,manifest FROM captures').fetchall()
        by_metadata = {}
        for cid, serialized in known:
            manifest = json.loads(serialized)
            by_metadata[self.root / manifest['artifacts']['metadata']['path']] = cid
            try:
                available = all(self.safe(self.root / item['path']).is_file()
                                for item in manifest['artifacts'].values())
            except (OSError, ValueError):
                available = False
            if not available:
                self.seen.pop(self.root / manifest['artifacts']['metadata']['path'], None)
                with self.lock:
                    self.db.execute('UPDATE captures SET available=0 WHERE id=?', (cid,))
        for path in self.root.rglob('metadata.json'):
            if any(part.startswith('.') for part in path.relative_to(self.root).parts):
                continue
            try:
                paths = {'metadata': path, 'preview': path.parent / 'frame_visual.png', 'raw': path.parent / 'frame_raw.bin'}
                stamp = tuple((self.safe(p).stat().st_mtime_ns, p.stat().st_size) for p in paths.values())
                if self.seen.get(path) == stamp:
                    continue
                blobs = {key: self.safe(p).read_bytes() for key, p in paths.items()}
                meta = json.loads(blobs['metadata'])
                if not isinstance(meta, dict):
                    raise ValueError('Metadata must be an object')
                if not valid_png(blobs['preview']):
                    raise ValueError('Incomplete or unsupported PNG')
                if len(blobs['raw']) != 38400:
                    raise ValueError('Invalid raw length')
                encoding = meta.get('radiometry', {})
                raw_valid = encoding == {'valid': True, 'encoding': 'centikelvin', 'width': 160, 'height': 120, 'dtype': 'uint16', 'byte_order': 'little'}
                if not raw_valid:
                    raise ValueError('Missing supported radiometry declaration; import legacy data first')
                event = meta['event_id']
                if not isinstance(event, str) or not event:
                    raise ValueError('Missing event ID')
                device = meta.get('device_id') or self.device_id
                if not isinstance(device, str) or not device:
                    raise ValueError('Invalid device identity')
                if not isinstance(meta.get('background_stats', {}), dict):
                    raise ValueError('Invalid background statistics')
                epoch = meta.get('unix_timestamp')
                if not isinstance(epoch, (int, float)) or not math.isfinite(epoch):
                    raise ValueError('Invalid capture timestamp')
                if not isinstance(meta.get('source', 'unknown'), str):
                    raise ValueError('Invalid source')
                cid = digest((device + ':' + event).encode())
                source = meta.get('source', 'unknown')
                mock = True if source in ('simulation', 'replay') else False if source == 'live' else None
                manifest = {'id': cid, 'device_id': device, 'event_id': event,
                    'session_id': meta.get('session_id'),
                    'timestamp': meta.get('timestamp_iso', ''), 'epoch': meta.get('unix_timestamp'),
                    'max_temp_c': meta.get('background_stats', {}).get('max_celsius'),
                    'mock': mock, 'source': source, 'trigger_mode': meta.get('trigger_mode', 'unspecified'),
                    'trigger_kind': meta.get('trigger_kind', 'rule'), 'request_id': meta.get('request_id'),
                    'has_raw_bin': True, 'schema_version': 2,
                    'artifacts': {key: {'path': str(p.relative_to(self.root)), 'size': len(blobs[key]), 'sha256': digest(blobs[key])} for key, p in paths.items()}}
                serialized = json.dumps(manifest, allow_nan=False)
                with self.lock:
                    previous = self.db.execute('SELECT manifest FROM captures WHERE id=?', (cid,)).fetchone()
                    if previous:
                        old = json.loads(previous[0])
                        if any(old['artifacts'][k]['sha256'] != manifest['artifacts'][k]['sha256'] for k in paths):
                            raise ValueError('Immutable capture changed: ' + cid)
                        self.db.execute('UPDATE captures SET manifest=?, available=1 WHERE id=?', (serialized, cid))
                    else:
                        self.db.execute('INSERT INTO captures(id,manifest) VALUES (?,?)', (cid, serialized))
                    self.db.commit()
                self.seen[path] = stamp
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors.append(f'{path.name}: {error}')
                self.seen.pop(path, None)
                if path in by_metadata:
                    with self.lock:
                        self.db.execute('UPDATE captures SET available=0 WHERE id=?', (by_metadata[path],))
        with self.lock:
            self.db.commit()
        self.errors = errors[-20:]

    def list(self, after=0, limit=100, snapshot=None):
        with self.lock:
            maximum = self.db.execute('SELECT COALESCE(MAX(sequence),0) FROM captures').fetchone()[0]
            snapshot = maximum if snapshot is None else min(snapshot, maximum)
            rows = self.db.execute('SELECT sequence,manifest FROM captures WHERE available=1 AND sequence>? AND sequence<=? ORDER BY sequence LIMIT ?', (after, snapshot, limit + 1)).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {'items': [dict(json.loads(row[1]), sequence=row[0]) for row in rows],
                'snapshot': snapshot, 'next_cursor': rows[-1][0] if has_more else snapshot}

    def get(self, cid):
        with self.lock:
            row = self.db.execute('SELECT manifest FROM captures WHERE id=?', (cid,)).fetchone()
        if not row:
            raise FileNotFoundError('Capture not found')
        return json.loads(row[0])

    def artifact(self, cid, role):
        manifest = self.get(cid)
        artifact = manifest['artifacts'][role]
        blob = self.safe(self.root / artifact['path']).read_bytes()
        if len(blob) != artifact['size'] or digest(blob) != artifact['sha256']:
            raise ValueError('Artifact integrity mismatch')
        return blob
