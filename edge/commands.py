"""Durable, idempotent requests consumed only by the camera owner."""
import json
import sqlite3
import time
import uuid
from contextlib import closing


class Commands:
    def __init__(self, root):
        self.path = root / 'commands.sqlite'
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, source TEXT NOT NULL, created REAL NOT NULL, state TEXT NOT NULL, result TEXT)')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def submit(self, request_id, source):
        request_id = str(uuid.UUID(request_id))
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT source FROM requests WHERE id=?', (request_id,)).fetchone()
            if old and old['source'] != source:
                raise ValueError('Request ID already belongs to another source')
            if not old:
                active = db.execute("SELECT COUNT(*) FROM requests WHERE state IN ('queued','processing') AND created>?", (time.time()-15,)).fetchone()[0]
                if active >= 4:
                    raise ValueError('Capture queue full; wait for existing requests')
                db.execute('INSERT INTO requests VALUES (?,?,?,?,NULL)', (request_id,source,time.time(),'queued'))
        return self.get(request_id)

    def get(self, request_id):
        request_id = str(uuid.UUID(request_id))
        with closing(self.connect()) as db, db:
            db.execute("UPDATE requests SET state='failed', result=? WHERE id=? AND state IN ('queued','processing') AND created<?", (json.dumps({'error':'Request expired or worker stopped. Check saved captures before requesting again.'}),request_id,time.time()-15))
            row = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
        if not row:
            raise FileNotFoundError('Unknown capture request')
        return {'request_id': row['id'], 'source': row['source'], 'state': row['state'], **json.loads(row['result'] or '{}')}

    def claim(self, source):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id FROM requests WHERE source=? AND state='queued' AND created>? ORDER BY created LIMIT 1", (source,time.time()-15)).fetchone()
            if row:
                db.execute("UPDATE requests SET state='processing' WHERE id=?", (row['id'],))
                return row['id']
        return None

    def finish(self, request_id, result, failed=False):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE requests SET state=?, result=? WHERE id=? AND state='processing'", ('failed' if failed else 'saved', json.dumps(result),request_id))
