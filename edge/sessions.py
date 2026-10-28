"""Boot-scoped session ledger shared by API and the single camera owner."""
import json
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path


def boot_id():
    path = Path('/proc/sys/kernel/random/boot_id')
    if path.exists():
        return path.read_text().strip()
    if sys.platform == 'darwin':
        try:
            return subprocess.check_output(['sysctl', '-n', 'kern.bootsessionuuid'], text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    # Unknown hosts are replay-only; process restart intentionally invalidates the mapping.
    return _REPLAY_BOOT


_REPLAY_BOOT = str(uuid.uuid4())


class Sessions:
    def __init__(self, root, device_id, boot=None):
        self.path = Path(root) / 'sessions.sqlite'
        self.device_id = device_id
        self.boot = boot or boot_id()
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS inventories (id TEXT PRIMARY KEY, receipt TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, boot TEXT, start REAL, end REAL, receipt TEXT)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def bind(self, request, now=None):
        now = time.monotonic() if now is None else now
        sid = str(uuid.UUID(request['session_id']))
        if request.get('device_id') != self.device_id or request.get('boot_id') != self.boot:
            raise ValueError('Device or boot changed; repeat clock exchange')
        for key in ('clock_mapping_id', 'calibration_id'):
            if not isinstance(request.get(key), str) or not 0 < len(request[key]) <= 128:
                raise ValueError('Missing ' + key)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT boot, end, receipt FROM sessions WHERE id=?', (sid,)).fetchone()
            if old:
                receipt = json.loads(old[2])
                if old[0] != self.boot or old[1] is not None or any(receipt[k] != request[k] for k in ('clock_mapping_id', 'calibration_id')):
                    raise ValueError('Session conflict or finalized; use a new sortie UUID')
                return receipt
            if db.execute('SELECT id FROM sessions WHERE boot=? AND end IS NULL', (self.boot,)).fetchone():
                raise ValueError('Finalize the active sortie first')
            receipt = dict(schema_version=1, session_id=sid, device_id=self.device_id,
                           boot_id=self.boot, clock_mapping_id=request['clock_mapping_id'],
                           calibration_id=request['calibration_id'], receipt_id=str(uuid.uuid4()),
                           acknowledged_monotonic_seconds=now)
            db.execute('INSERT INTO sessions VALUES (?,?,?,NULL,?)', (sid, self.boot, now, json.dumps(receipt)))
            return receipt

    def finalize(self, request, now=None):
        sid = str(uuid.UUID(request['session_id']))
        if request.get('boot_id') != self.boot or request.get('device_id') != self.device_id:
            raise ValueError('Device or boot changed')
        now = time.monotonic() if now is None else now
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT boot, end, receipt FROM sessions WHERE id=?', (sid,)).fetchone()
            if not row or row[0] != self.boot:
                raise ValueError('Unknown session')
            end = now if row[1] is None else row[1]
            db.execute('UPDATE sessions SET end=? WHERE id=?', (end, sid))
            return dict(json.loads(row[2]), finalized_monotonic_seconds=end)

    def inventory(self, session_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT receipt FROM inventories WHERE id=?', (str(uuid.UUID(session_id)),)).fetchone()
        return json.loads(row[0]) if row else None

    def save_inventory(self, receipt):
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO inventories VALUES (?,?)',
                       (receipt['sessionID'], json.dumps(receipt, sort_keys=True)))
        return self.inventory(receipt['sessionID'])

    def receipt(self, session_id):
        session_id = str(uuid.UUID(session_id))
        with closing(self.connect()) as db:
            row = db.execute('SELECT end, receipt FROM sessions WHERE id=?', (session_id,)).fetchone()
        if not row:
            raise FileNotFoundError('Unknown session')
        return dict(json.loads(row[1]), finalized_monotonic_seconds=row[0])

    def frame_timing(self, received, sequence, simulator):
        with closing(self.connect()) as db:
            row = db.execute('SELECT receipt FROM sessions WHERE boot=? AND start<=? AND (end IS NULL OR end>=?) ORDER BY start DESC LIMIT 1', (self.boot, received, received)).fetchone()
        receipt = json.loads(row[0]) if row else {}
        return dict(schema_version=1, device_id=self.device_id, boot_id=self.boot,
                    frame_sequence=sequence, received_monotonic_seconds=received,
                    timestamp_kind='frame_received', receive_to_exposure_uncertainty_seconds=None,
                    session_id=receipt.get('session_id'), session_acknowledged=bool(row),
                    clock_mapping_id=receipt.get('clock_mapping_id'), calibration_id=receipt.get('calibration_id'),
                    is_simulator=simulator)
