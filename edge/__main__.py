import argparse
import logging
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from .settings import load


def main():
    parser = argparse.ArgumentParser(description='Tilik capture + iOS sync')
    parser.add_argument('service', choices=['capture', 'api', 'run'])
    parser.add_argument('--config', default='config/edge.json')
    args = parser.parse_args()
    settings = load(args.config)
    logging.basicConfig(level=logging.INFO)
    if args.service == 'capture':
        from hotspot.__main__ import run
        return run(SimpleNamespace(config=settings['rules'], output=settings['data_dir'] / 'captures',
            source=settings['source'], device=settings['device'], device_id=settings['device_id'],
            confirm_centikelvin=settings['confirm_centikelvin'], warmup_seconds=10,
            frames=0, min_free_mb=256))
    if args.service == 'api':
        from .api import serve
        serve(settings)
        return 0
    # Check the complete runtime before starting the camera child. Partial deployments
    # otherwise report an API failure only after the camera has already opened USB.
    try:
        from .api import serve
    except ImportError as error:
        print(f'Incomplete Tilik runtime: {error}. Run the updated deploy/update-api.sh from the Mac to copy both edge and hotspot packages.', file=sys.stderr)
        return 2
    stopped = False
    def stop(*unused):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    children = []
    try:
        for name in ('capture', 'api'):
            children.append(subprocess.Popen([sys.executable, '-m', 'edge', name, '--config', args.config]))
        print(f"Dashboard and iOS API: port {settings['port']}; source={settings['source']}", flush=True)
        while not stopped:
            if children[1].poll() is not None:
                return 1
            if children[0].poll() is not None:
                print('Capture stopped. API/dashboard remains available; check capture error above. Press Ctrl+C, fix config, and restart.', flush=True)
                while not stopped and children[1].poll() is None:
                    time.sleep(.25)
                return 0 if stopped else 1
            time.sleep(.25)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
