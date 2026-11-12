"""Optional 10-minute QA frame schedule; does not change hotspot rules."""
import json
import logging
import time
from pathlib import Path

LOG = logging.getLogger('hotspot.qa_spam')
CONTROL = Path('/run/tilik-qa-video')
SLOTS = tuple(range(0, 300, 20)) + (300, 320, 340, 420, 440, 460, 540, 560, 580)

class SpamCapture:
    def __init__(self):
        self.started = None
        self.next_slot = 0
        self.saved = 0
        self.failed = 0
        self.skipped = 0
        self.request_id = None
        self.state = 'idle'
        self.last_write = 0

    def update(self, now, capture=None):
        try:
            return self._update(now, capture)
        except Exception:
            LOG.exception('QA capture failed; hotspot monitoring continues')
            self.failed += 1
            self._write()
            return None

    def _update(self, now, capture):
        request = CONTROL / 'spam-start.json'
        if request.exists():
            data = json.loads(request.read_text())
            request.unlink(missing_ok=True)
            if self.started is None and time.time() - data['created'] < 10:
                self.started = now
                self.request_id = data['id']
                self.next_slot = self.saved = self.failed = self.skipped = 0
                self.state = 'running'
        if self.started is None:
            return None
        elapsed = now - self.started
        path = None
        if elapsed >= 600:
            self.skipped += len(SLOTS) - self.next_slot
            self.started = None
            self.state = 'complete'
        else:
            while self.next_slot < len(SLOTS) and elapsed >= SLOTS[self.next_slot]:
                slot = SLOTS[self.next_slot]
                self.next_slot += 1
                if capture is not None and elapsed - slot < 2:
                    try:
                        path = capture()
                        self.saved += 1
                    except Exception:
                        LOG.exception('Scheduled capture failed')
                        self.failed += 1
                else:
                    self.skipped += 1
        if now - self.last_write >= 1 or path is not None or self.started is None:
            self._write()
            self.last_write = now
        return path

    def _write(self):
        try:
            target = CONTROL / 'spam-status.json'
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps({'state': self.state, 'saved': self.saved,
                'failed': self.failed, 'skipped': self.skipped, 'request_id': self.request_id,
                'updated_at': time.time()}))
            temporary.replace(target)
        except OSError:
            LOG.warning('Cannot write QA capture status')
