"""Recover a verified PureThermal stream without coupling capture to Wi-Fi/API.

USB identity checks read sysfs only. The capture adapter remains the sole camera
owner. No udev scripts, Wi-Fi toggles, service commands, or synthetic fallback.
"""
import logging
from pathlib import Path
import time
from .camera import LatestCamera

LOG = logging.getLogger(__name__)
READY_STATES = frozenset(('IDLE', 'DEBOUNCING', 'TRIGGERED', 'COOLDOWN'))
# Verified PureThermal 3 on this project enumerates as Cubeternet 1e4e:0100.
USB_ID = ('1e4e', '0100')


def purethermal_node(node, sysfs=Path('/sys/class/video4linux')):
    """Accept the streaming node, not a webcam, USB hub or UVC metadata node."""
    entry = sysfs / Path(node).resolve().name
    try:
        if (entry / 'index').read_text().strip() != '0':
            return False
        device = (entry / 'device').resolve(strict=True)
        for parent in (device, *device.parents):
            vendor, product = parent / 'idVendor', parent / 'idProduct'
            if vendor.exists() and product.exists():
                return (vendor.read_text().strip().lower(), product.read_text().strip().lower()) == USB_ID
    except OSError:
        return False
    return False


def resolve_device(configured, sysfs=Path('/sys/class/video4linux'), dev=Path('/dev')):
    if configured != 'auto':
        path = Path(configured)
        return str(path) if path.exists() and purethermal_node(path, sysfs) else None
    candidates = [str(dev / entry.name) for entry in sorted(sysfs.glob('video*'))
                  if (dev / entry.name).exists() and purethermal_node(dev / entry.name, sysfs)]
    if len(candidates) > 1:
        raise ValueError('Multiple PureThermal cameras: select one stable /dev/v4l/by-id/...-video-index0 path.')
    return candidates[0] if candidates else None


class LiveCamera:
    """One stream at a time, bounded retry, warmup on EVERY new USB connection."""
    def __init__(self, device, warmup_seconds, factory=LatestCamera, resolver=resolve_device,
                 clock=time.monotonic):
        self.device, self.warmup_seconds = device, warmup_seconds
        self.factory, self.resolver, self.clock = factory, resolver, clock
        self.stream = None
        self.state = 'WAITING_CAMERA'
        self.error = None
        self.selected_device = None
        self.next_attempt = 0
        self.warmup_until = 0
        self.received_at = self.received_utc = None
        self.consumed = self.dropped = 0
        self.sequence_base = self.dropped_base = 0

    def read(self):
        if self.stream is None:
            if self.clock() < self.next_attempt:
                return None
            self.next_attempt = self.clock() + 3
            try:
                self.selected_device = self.resolver(self.device)
                if self.selected_device is None:
                    self.state, self.error = 'WAITING_CAMERA', None
                    return None
                self.stream = self.factory(self.selected_device)
                self.warmup_until = self.clock() + self.warmup_seconds
                LOG.info('PureThermal attached: %s; warming up', self.selected_device)
            except (OSError, RuntimeError, ValueError) as error:
                self.state, self.error = 'CAMERA_ERROR', str(error)
                LOG.warning('Camera open failed; retrying: %s', error)
                return None
        try:
            raw = self.stream.read()
        except (OSError, RuntimeError, EOFError, TimeoutError) as error:
            # Close before retry so a replug never opens a second camera owner.
            self.stream.close()
            self.stream = None
            self.sequence_base, self.dropped_base = self.consumed, self.dropped
            self.state, self.error = 'CAMERA_ERROR', str(error)
            self.received_at = self.received_utc = None
            self.next_attempt = self.clock() + 3
            LOG.warning('Camera disconnected/stalled; waiting to recover: %s', error)
            return None
        self.error = None
        self.received_at, self.received_utc = self.stream.received_at, self.stream.received_utc
        self.consumed = self.sequence_base + self.stream.consumed
        self.dropped = self.dropped_base + self.stream.dropped
        if self.clock() < self.warmup_until:
            self.state = 'WARMUP'
            return None
        self.state = 'STREAMING'
        return raw

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
