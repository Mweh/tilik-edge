"""Linux USB UVC Y16 acquisition through FFmpeg or v4l2-ctl.

No pixel-format conversion: stream-copy the explicitly requested gray16le input.
No shell interpolation; stderr remains visible in the service journal.
"""
import os
import sys
from pathlib import Path
import select
import shutil
import struct
import subprocess
import time
import threading
from collections import deque
from .engine import RAW_FRAME_BYTES


def command(device):
    if sys.platform != 'linux':
        raise RuntimeError('Live capture is supported on Raspberry Pi/Linux only. Use simulation on Mac.')
    if not shutil.which('ffmpeg'):
        if shutil.which('v4l2-ctl'):
            # FourCC includes a trailing space. stdout is binary frames only;
            # --stream-to=- automatically enables silent mode in v4l2-ctl.
            return ['v4l2-ctl', '--device', device,
                    '--set-fmt-video=width=160,height=120,pixelformat=Y16 ',
                    '--stream-mmap=3', '--stream-poll', '--stream-to=-']
        raise RuntimeError('Live capture needs ffmpeg or v4l2-ctl. Install ffmpeg and v4l-utils on the Pi.')
    return ['ffmpeg' , '-nostdin', '-hide_banner', '-loglevel', 'warning',
            '-f', 'v4l2', '-input_format', 'gray16le', '-video_size', '160x120',
            '-i', device, '-map', '0:v:0', '-c:v', 'copy', '-f', 'rawvideo', 'pipe:1']


def read_exact(fd, size, timeout=5.0):
    """Accumulate partial pipe reads; EOF/stall must never become a frame."""
    deadline = time.monotonic() + timeout
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], max(0, remaining))[0]:
            raise TimeoutError('Camera stalled; no complete frame within timeout')
        chunk = os.read(fd, size - len(data))
        if not chunk:
            raise EOFError('Camera stream ended or format negotiation failed')
        data.extend(chunk)
    return bytes(data)


class Camera:
    def __init__(self, device):
        self.process = None
        self.errors = deque(maxlen=12)
        self.stderr_thread = None
        if device == '-':
            self.fd = os.dup(sys.stdin.fileno())
        else:
            self.process = subprocess.Popen(command(device), stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            self.fd = self.process.stdout.fileno()
            self.stderr_thread = threading.Thread(target=self._drain_errors, daemon=True)
            self.stderr_thread.start()

    def _drain_errors(self):
        for line in iter(self.process.stderr.readline, b''):
            message = line.decode(errors='replace').strip()
            self.errors.append(message)
            print(message, file=sys.stderr, flush=True)

    def read(self):
        try:
            return list(struct.unpack('<19200H', read_exact(self.fd, RAW_FRAME_BYTES)))
        except (EOFError, TimeoutError) as error:
            if self.stderr_thread:
                self.stderr_thread.join(timeout=.2)
            details = ' '.join(self.errors)
            if 'Access denied' in details:
                raise RuntimeError('Camera access denied. Check device permissions and video group membership on the Pi.') from error
            raise RuntimeError(details or str(error)) from error

    def close(self):
        if self.process is None:
            os.close(self.fd)
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        if self.stderr_thread:
            self.stderr_thread.join(timeout=1)
        self.process.stderr.close()


class LatestCamera:
    """Drain USB continuously into one slot so processing cannot build a backlog."""
    def __init__(self, device, factory=Camera):
        self.camera = factory(device)
        self.condition = threading.Condition()
        self.stopping = False
        self.latest = None
        self.error = None
        self.sequence = 0
        self.consumed = 0
        self.dropped = 0
        self.received_at = None
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        try:
            while not self.stopping:
                raw = self.camera.read()
                with self.condition:
                    self.sequence += 1
                    self.latest = (raw, time.monotonic())
                    self.condition.notify_all()
        except Exception as error:
            with self.condition:
                self.error = error
                self.condition.notify_all()

    def read(self):
        with self.condition:
            ready = self.condition.wait_for(lambda: self.error or self.stopping or self.sequence > self.consumed, timeout=5)
            if self.error:
                raise self.error
            if not ready or self.stopping:
                raise TimeoutError('No fresh camera frame')
            raw, received = self.latest
            if time.monotonic() - received > 2:
                raise TimeoutError('Camera frame is stale')
            self.dropped += max(0, self.sequence-self.consumed-1)
            self.consumed = self.sequence
            self.received_at = received
            return raw

    def close(self):
        self.stopping = True
        self.camera.close()
        with self.condition:
            self.condition.notify_all()
        self.thread.join(timeout=6)
