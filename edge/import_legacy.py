"""Copy legacy captures into the new store; never change source files."""
import argparse
import json
import os
import shutil
import uuid
from pathlib import Path
from .settings import load
from .storage import digest, valid_png


def migrate(source, settings, confirm=False):
    source = Path(source).resolve()
    if not source.is_dir():
        raise ValueError('Legacy source directory does not exist')
    target = settings['data_dir'] / 'captures' / 'imported'
    target.mkdir(parents=True, exist_ok=True)
    if target.resolve().is_relative_to(source):
        raise ValueError('Destination cannot be inside the legacy source')
    imported, skipped, errors = 0, 0, []
    for path in source.rglob('*.json'):
        if any(part.startswith('.') for part in path.relative_to(source).parts):
            continue
        flat = path.name.endswith('_meta.json')
        if not flat and path.name != 'metadata.json':
            continue
        stage = None
        try:
            original = path.read_bytes()
            meta = json.loads(original)
            if not isinstance(meta, dict):
                raise ValueError('Metadata must be an object')
            if flat:
                base = path.name[:-10]
                png = path.parent / (base + '_preview.png')
                binary = path.parent / (base + '_raw.bin')
                if meta.get('temperature', {}).get('is_calibrated_radiometric') is not True:
                    raise ValueError('Uncalibrated raw; kept in legacy store')
                if binary.exists():
                    raw = binary.read_bytes()
                else:
                    import numpy as np
                    array = np.load(path.parent / (base + '_raw.npy'), allow_pickle=False)
                    if array.shape != (120, 160) or array.dtype.kind != 'u' or array.dtype.itemsize != 2:
                        raise ValueError('Unsupported raw array')
                    raw = array.astype('<u2').tobytes()
                mock = meta.get('device', {}).get('mock')
                normalized = {'event_id': base, 'timestamp_iso': meta.get('timestamp', ''),
                    'unix_timestamp': meta.get('epoch'), 'source': 'simulation' if mock is True else 'live' if mock is False else 'unknown',
                    'trigger_mode': meta.get('trigger_mode', 'unspecified'),
                    'background_stats': {'max_celsius': meta.get('temperature', {}).get('max_c')}}
            else:
                png = path.parent / 'frame_visual.png'
                raw = (path.parent / 'frame_raw.bin').read_bytes()
                normalized = dict(meta)
                declared = meta.get('radiometry')
                expected = {'valid': True, 'encoding': 'centikelvin', 'width': 160, 'height': 120, 'dtype': 'uint16', 'byte_order': 'little'}
                if declared is not None and declared != expected:
                    raise ValueError('Unsupported or invalid declared radiometry; refusing reinterpretation')
                if declared is None and not confirm:
                    raise ValueError('Legacy radiometry requires --confirm-centikelvin after verifying source configuration')
            preview = png.read_bytes()
            if len(raw) != 38400 or not valid_png(preview):
                raise ValueError('Invalid PNG or raw')
            normalized['schema_version'] = 2
            normalized['device_id'] = meta.get('device_id') or settings['device_id']
            normalized['event_id'] = normalized.get('event_id') or digest(original)
            normalized['radiometry'] = {'valid': True, 'encoding': 'centikelvin', 'width': 160, 'height': 120, 'dtype': 'uint16', 'byte_order': 'little'}
            # Preserve all original metadata, including flat-format telemetry.
            normalized['legacy_metadata'] = meta
            name = digest((normalized['device_id'] + ':' + normalized['event_id']).encode())
            destination = target / name
            content = json.dumps(normalized, indent=2, allow_nan=False).encode()
            if destination.exists():
                if (destination/'metadata.json').read_bytes() != content or (destination/'frame_raw.bin').read_bytes() != raw or (destination/'frame_visual.png').read_bytes() != preview:
                    raise ValueError('Existing import differs; refusing overwrite')
                skipped += 1
                continue
            stage = target / ('.' + uuid.uuid4().hex + '.pending')
            stage.mkdir()
            for filename, blob in [('metadata.json', content), ('frame_raw.bin', raw), ('frame_visual.png', preview)]:
                with (stage / filename).open('wb') as handle:
                    handle.write(blob)
                    handle.flush()
                    os.fsync(handle.fileno())
            stage.rename(destination)
            imported += 1
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ImportError) as error:
            errors.append(f'{path}: {error}')
            if stage and stage.exists():
                shutil.rmtree(stage)
    return {'imported': imported, 'already_present': skipped, 'errors': errors}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('--config', default='config/edge.json')
    parser.add_argument('--confirm-centikelvin', action='store_true')
    args = parser.parse_args()
    result = migrate(args.source, load(args.config), args.confirm_centikelvin)
    print(json.dumps(result, indent=2))
    raise SystemExit(1 if result['errors'] else 0)
