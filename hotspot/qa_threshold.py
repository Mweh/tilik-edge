"""Apply authenticated BLE threshold requests from the capture owner."""
import json
import logging
import math
import time
from pathlib import Path
from .engine import RadiometricAutoCaptureEngine

LOG = logging.getLogger('hotspot.qa_threshold')
CONTROL = Path('/run/tilik-qa-video')


def apply_pending(config_path):
    request = CONTROL / 'threshold-request.json'
    if not request.exists():
        return
    try:
        data = json.loads(request.read_text())
        request.unlink(missing_ok=True)
        if time.time() - data['created'] > 10:
            raise ValueError('request_expired')
        value = float(data['value'])
        if not math.isfinite(value) or not -40 <= value <= 300:
            raise ValueError('invalid_threshold')
        path = Path(config_path)
        rules = json.loads(path.read_text())
        rules['custom_threshold'] = value
        # Validate all rules before replacing the existing configuration.
        RadiometricAutoCaptureEngine(output_dir=str(path.parent), **rules)
        temp = path.with_name(path.name + '.ble.tmp')
        temp.write_text(json.dumps(rules, indent=2) + '\n')
        temp.replace(path)
        result = {'request_id': data['id'], 'state': 'applied', 'value': value}
    except Exception as error:
        LOG.exception('Threshold request rejected')
        result = {'request_id': data.get('id') if isinstance(locals().get('data'), dict) else None,
                  'state': 'failed', 'error': str(error)}
    result['updated_at'] = time.time()
    try:
        target = CONTROL / 'threshold-result.json'
        tmp = CONTROL / 'threshold-result.tmp'
        tmp.write_text(json.dumps(result))
        tmp.replace(target)
    except OSError:
        LOG.exception('Threshold result unavailable')
