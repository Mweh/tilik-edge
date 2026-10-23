"""Headless capture: python3 -m hotspot --help."""
import argparse
import base64
import json
import logging
import math
import os
from pathlib import Path
import shutil
import signal
import struct
import sys
import time
from .engine import RadiometricAutoCaptureEngine, celsius_to_centikelvin, render_annotated_thermal_image
from .camera import LatestCamera as Camera
from edge.commands import Commands
from edge.storage import digest

LOG = logging.getLogger('hotspot')
FIELDS = {'mode', 'custom_threshold', 'delta_celsius', 'mad_multiplier', 'min_cluster_pixels',
          'max_cluster_pixels', 'debounce_frames', 'persistence_seconds', 'cooldown_seconds', 'hysteresis_celsius'}


def load_config(path):
    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict) or set(config) != FIELDS:
        raise ValueError('Config must contain exactly: ' + ', '.join(sorted(FIELDS)))
    return config


def synthetic_frame(index):
    """40-second cycle: cool, persistent hot patch, then cooling/rearm."""
    raw = [celsius_to_centikelvin(30)] * 19200
    if 3 <= index / 8 % 40 < 10:
        for y in range(50, 60):
            for x in range(70, 80):
                raw[y * 160 + x] = celsius_to_centikelvin(75)
    return raw


def atomic_json(path, content):
    pending = path.with_suffix('.tmp')
    pending.write_text(json.dumps(content, indent=2, allow_nan=False))
    pending.replace(path)


def run(args):
    config = load_config(args.config)
    output = Path(args.output).resolve() / args.source
    engine = RadiometricAutoCaptureEngine(output_dir=str(output), source=args.source, **config)
    engine.device_id = getattr(args, 'device_id', None)
    config_stamp = Path(args.config).stat().st_mtime_ns
    if args.source == 'live' and not args.confirm_centikelvin:
        raise ValueError('Live input requires --confirm-centikelvin after verifying TLinear 0.01 K and AGC disabled')
    if not math.isfinite(args.warmup_seconds) or args.warmup_seconds < 0:
        raise ValueError("warmup-seconds must be finite and non-negative")
    if args.frames < 0 or not math.isfinite(args.min_free_mb) or args.min_free_mb < 0:
        raise ValueError('frames and min-free-mb must be non-negative')
    # Lock prevents a second process from racing the same event/status directory.
    import fcntl
    lock = (output.parent / '.capture.lock').open('a')
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    stopped = False
    def stop(signum, frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    camera = None
    index = 0
    last_event = {}
    commands = Commands(output.parent.parent)
    preview_path = output / 'preview.png'
    last_preview = 0.0
    last_preview_at = None
    last_processing_ms = 0.0
    warmup_until = time.monotonic() + (args.warmup_seconds if args.source == 'live' else 0)
    try:
        if args.source == 'live':
            camera = Camera(args.device)
        while not stopped and (args.frames == 0 or index < args.frames):
            started = time.monotonic()
            stamp = Path(args.config).stat().st_mtime_ns
            if stamp != config_stamp:
                try:
                    updated = load_config(args.config)
                    replacement = RadiometricAutoCaptureEngine(output_dir=str(output), source=args.source, **updated)
                    replacement.device_id = getattr(args, 'device_id', None)
                    replacement.total_captures = engine.total_captures
                    replacement.last_capture_time = engine.last_capture_time
                    engine, config = replacement, updated
                    LOG.info('Rules updated; persistence reset')
                except (ValueError, TypeError, OSError) as error:
                    LOG.error('Rules rejected; retaining last working config: %s', error)
                config_stamp = stamp
            if shutil.disk_usage(output).free < args.min_free_mb * 1024 * 1024:
                raise OSError('Insufficient free disk space; captures retained, service stopping')
            raw = camera.read() if camera else synthetic_frame(index)
            if camera and time.monotonic() < warmup_until:
                atomic_json(output / 'status.json', {'state': 'WARMUP', 'updated_at': time.time()})
                continue
            try:
                result = engine.process_frame(raw)
            except ValueError as error:
                engine.reset_persistence()
                LOG.warning('Invalid frame rejected: %s', error)
                atomic_json(output / 'status.json', {'state': 'INVALID_FRAME', 'error': str(error), 'updated_at': time.time()})
                index += 1
                continue
            request_id = commands.claim(args.source)
            if request_id:
                try:
                    manual_pixels = [v / 100 - 273.15 for v in raw]
                    saved = engine._save_capture_bundle(raw, manual_pixels, result['hotspots'],
                        result['threshold_celsius'], result['bg_median_celsius'], result['mad_celsius'],
                        result['min_celsius'], result['max_celsius'], time.time(),
                        trigger_kind='manual_test', request_id=request_id)
                    engine.total_captures += 1
                    metadata = json.loads((Path(saved) / 'metadata.json').read_text())
                    cid = digest(((engine.device_id or '') + ':' + metadata['event_id']).encode())
                    commands.finish(request_id, {'capture_id': cid})
                    last_event = dict(last_capture_path=saved, last_capture_kind='manual_test')
                except Exception as error:
                    commands.finish(request_id, {'error': str(error)}, failed=True)
            pixels = [v / 100 - 273.15 for v in raw]
            status = {k: v for k, v in result.items() if k != 'hotspots'}
            if time.monotonic() - last_preview >= .2:
                preview = render_annotated_thermal_image(pixels, 160, 120, result['hotspots'], min(pixels), max(pixels))
                pending_preview = preview_path.with_suffix('.tmp')
                pending_preview.write_bytes(preview)
                pending_preview.replace(preview_path)
                last_preview = time.monotonic()
                last_preview_at = time.time()
            if result['capture_path']:
                last_event = dict(last_capture_path=result['capture_path'], last_capture_kind='rule')
            status.update(last_event)
            status.update(updated_at=time.time(), source=args.source, frames_processed=index + 1,
                          captures=engine.total_captures, rule_config=config,
                          preview_updated_at=last_preview_at,
                          processing_ms=round((time.monotonic()-started)*1000, 1),
                          dropped_frames=getattr(camera, 'dropped', 0))
            atomic_json(output / 'status.json', status)
            if result['capture_path']:
                LOG.info('AUTO_CAPTURED %s', result['capture_path'])
            elif index % 80 == 0:
                LOG.info('%s threshold=%.2f peak=%.2f', result['state'], result['threshold_celsius'], result['max_celsius'])
            index += 1
            if not camera:
                time.sleep(max(0, 0.125 - (time.monotonic() - started)))
    except Exception as error:
        atomic_json(output / 'status.json', {'state': 'ERROR', 'error': str(error), 'updated_at': time.time()})
        raise
    finally:
        if camera:
            camera.close()
        lock.close()
    final_status = json.loads((output / 'status.json').read_text()) if (output / 'status.json').exists() else {}
    final_status.update(state='STOPPED', updated_at=time.time(), captures=engine.total_captures)
    atomic_json(output / 'status.json', final_status)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', choices=['simulation', 'live'], default='simulation')
    p.add_argument('--device', default='/dev/video0', help='Prefer /dev/v4l/by-id/... stable device path')
    p.add_argument('--config', default='config/landfill.json')
    p.add_argument('--output', default='captures')
    p.add_argument('--frames', type=int, default=0, help='0 runs until stopped')
    p.add_argument('--min-free-mb', type=float, default=256)
    p.add_argument('--warmup-seconds', type=float, default=10, help='Live startup stabilization period')
    p.add_argument('--confirm-centikelvin', action='store_true')
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        return run(args)
    except Exception as error:
        LOG.error('%s', error)
        return 1

if __name__ == '__main__':
    sys.exit(main())
