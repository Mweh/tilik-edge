import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from hotspot import qa_threshold

class ThresholdTests(unittest.TestCase):
    def test_apply_preserves_other_rules_and_rejects_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); config = root / 'rules.json'
            rules = json.loads(Path('config/landfill.json').read_text())
            config.write_text(json.dumps(rules))
            with patch.object(qa_threshold, 'CONTROL', root):
                request = root / 'threshold-request.json'
                request.write_text(json.dumps({'id': 'one', 'created': time.time(), 'value': 42}))
                qa_threshold.apply_pending(config)
                updated = json.loads(config.read_text())
                self.assertEqual(updated['custom_threshold'], 42)
                self.assertEqual(updated['mode'], rules['mode'])
                self.assertEqual(updated['delta_celsius'], rules['delta_celsius'])
                self.assertEqual(json.loads((root/'threshold-result.json').read_text())['state'], 'applied')
                request.write_text(json.dumps({'id': 'two', 'created': time.time(), 'value': 999}))
                with self.assertLogs('hotspot.qa_threshold', level='ERROR'):
                    qa_threshold.apply_pending(config)
                self.assertEqual(json.loads(config.read_text()), updated)
                self.assertEqual(json.loads((root/'threshold-result.json').read_text())['state'], 'failed')
