"""Optional QA video: bounded queue; camera owner never waits for encoder."""
import json
import logging
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid
from .qa_video_schedule import VideoSchedule
from .engine import inferno_colormap

# Same false-color palette as hotspot images.
PALETTE = tuple(bytes(inferno_colormap(i / 255)) for i in range(256))

LOG = logging.getLogger('hotspot.qa_video')
CONTROL = Path('/run/tilik-qa-video')

class VideoQA:
    def __init__(self, output):
        self.output = Path(output) / 'qa-videos'
        self.schedule = None
        self.worker = None
        self.queue = None
        self.end = 0
        self.records = []
        self.last_frame = 0
        self.session = None
        self.control_stamp = None
        self.immediate = False

    def status(self):
        return {'state': 'running' if self.schedule else 'idle', 'clips': self.records,
                'session': self.session, 'updated_at': time.time()}

    def update(self, now, raw=None):
        try:
            self._update(now, raw)
        except Exception:
            LOG.exception('QA video failed; thermal monitoring continues')
            self.abort()

    def _update(self, now, raw):
        request = CONTROL / 'start.json'
        if request.exists():
            item = json.loads(request.read_text())
            request_id = item['id']
            request.unlink(missing_ok=True)
            if request_id != self.control_stamp and time.time() - item['created'] < 10:
                self.control_stamp = request_id
                # A duplicate request never restarts an active session.
                if self.schedule is None:
                    self.immediate = item.get('immediate') is True
                    self.schedule = VideoSchedule(now - 120 if self.immediate else now)
                    self.session = request_id
                    self.records = []
                    self.output.mkdir(parents=True, exist_ok=True)
                elif item.get('immediate') is True:
                    elapsed = now - self.schedule.started
                    next_due = min((x for x in (120, 300, 480, 660, 840) if x >= elapsed), default=900)
                    if self.queue or (self.worker and self.worker.is_alive()) or next_due - elapsed < 6:
                        self.records.append({'minute': 2, 'state': 'failed', 'error': 'recorder busy'})
                    else:
                        self._start_clip(120, now + 5, session_id=request_id)
        if not self.schedule:
            return
        state, offset, end = self.schedule.update(now)
        if self.queue and now >= self.end:
            self._finish_clip()
        if state == 'start':
            if self.worker and self.worker.is_alive():
                self.records.append({'minute': offset // 60, 'state': 'failed', 'error': 'encoder busy'})
            else:
                self._start_clip(offset, end)
        if self.queue and raw is not None and now < self.end and now - self.last_frame >= .12:
            # Copy raw sensor values; normalization and color remain on the worker.
            frame = tuple(raw)
            try:
                self.queue.put_nowait(frame)
                self.last_frame = now
            except queue.Full:
                self.records[-1]['dropped'] += 1
        if self.immediate and self.records and self.queue is None:
            self.schedule = None
            self.immediate = False
        if state == 'complete':
            if self.queue:
                self._finish_clip()
            self.schedule = None
        self._write_status()

    def _start_clip(self, offset, end, session_id=None):
        record = {'minute': offset // 60, 'state': 'recording', 'frames': 0, 'dropped': 0}
        self.records.append(record)
        path = self.output / ((session_id or self.session) + '_minute_' + str(offset // 60) + '.mp4')
        partial = path.with_suffix('.partial.mp4')
        q = queue.Queue(maxsize=16)
        self.queue, self.end, self.last_frame = q, end, 0
        def encode():
            process = None
            try:
                if not shutil.which('ffmpeg'):
                    raise RuntimeError('ffmpeg not installed')
                process = subprocess.Popen(['ffmpeg', '-loglevel', 'error', '-y', '-f', 'rawvideo',
                    '-pix_fmt', 'rgb24', '-s', '160x120', '-r', '5', '-i', 'pipe:0',
                    '-vf', 'tpad=stop_mode=clone:stop_duration=5', '-t', '5', '-an', '-c:v', 'mpeg4', '-q:v', '5', str(partial)], stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                while True:
                    frame = q.get()
                    if frame is None:
                        break
                    # Color conversion stays off the capture thread.
                    minimum, maximum = min(frame), max(frame)
                    span = max(100, maximum - minimum)  # same 1°C floor as PNG
                    process.stdin.write(b''.join(PALETTE[max(0, min(255, (value - minimum) * 255 // span))] for value in frame))
                    record['frames'] += 1
                process.stdin.close()
                if process.wait(timeout=20) or record['frames'] < 20:
                    raise RuntimeError('incomplete video or encoder failure')
                partial.replace(path)
                record['state'] = 'saved'
                record['file'] = path.name
                self._write_status()
            except Exception as error:
                if process and process.poll() is None:
                    process.kill(); process.wait()
                partial.unlink(missing_ok=True)
                record['state'] = 'failed'; record['error'] = str(error)
                self._write_status()
        self.worker = threading.Thread(target=encode, daemon=True)
        self.worker.start()

    def _finish_clip(self):
        q, self.queue = self.queue, None
        try:
            q.put_nowait(None)
        except queue.Full:
            # Drop one frame instead of blocking hotspot detection.
            q.get_nowait(); self.records[-1]['dropped'] += 1; q.put_nowait(None)

    def _write_status(self):
        target = CONTROL / 'status.json'
        temp = CONTROL / ('status-' + str(os.getpid()) + '.tmp')
        try:
            temp.write_text(json.dumps(self.status()))
            temp.replace(target)
        except OSError:
            pass

    def abort(self):
        if self.queue:
            self._finish_clip()
        self.schedule = None
        self._write_status()
