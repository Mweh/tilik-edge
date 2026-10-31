import json
import os
import uuid
from pathlib import Path


def load(path):
    path = Path(path).resolve()
    settings = json.loads(path.read_text())
    for key in ('data_dir', 'rules'):
        settings[key] = (path.parent / settings[key]).resolve()
    if type(settings.get('confirm_centikelvin')) is not bool:
        raise ValueError('confirm_centikelvin must be a JSON boolean')
    if type(settings.get('port')) is not int or not 0 <= settings['port'] <= 65535:
        raise ValueError('port must be an integer from 0 to 65535')
    if not isinstance(settings.get('host'), str) or not settings['host']:
        raise ValueError('host must be a non-empty string')
    if settings['source'] not in ('live', 'simulation'):
        raise ValueError('source must be live or simulation')
    if type(settings.get('allow_remote_capture', True)) is not bool:
        raise ValueError('allow_remote_capture must be a JSON boolean')
    if bool(settings.get('tls_cert')) != bool(settings.get('tls_key')):
        raise ValueError('Configure both tls_cert and tls_key')
    for key in ('tls_cert', 'tls_key'):
        if settings.get(key):
            settings[key] = str((path.parent / settings[key]).resolve())
    root = settings['data_dir']
    root.mkdir(parents=True, exist_ok=True)
    # Both services can start simultaneously. Lock initialization and fsync identity.
    import fcntl
    with (root / '.identity.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        identity = root / 'device-id'
        if not identity.exists():
            pending = root / '.device-id.pending'
            with pending.open('w') as handle:
                handle.write(str(uuid.uuid4()))
                handle.flush()
                os.fsync(handle.fileno())
            pending.replace(identity)
            directory_fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        device_id = identity.read_text().strip()
        if not device_id:
            raise ValueError('Empty device-id; restore this Pi identity from backup')
        settings['device_id'] = device_id
    return settings
