import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from hotspot import qa_spam

class SpamTests(unittest.TestCase):
    def test_schedule(self):
        with tempfile.TemporaryDirectory() as d, patch.object(qa_spam, 'CONTROL', Path(d)):
            (Path(d) / 'spam-start.json').write_text(json.dumps({'id':'test', 'created':time.time()}))
            spam = qa_spam.SpamCapture()
            calls = []
            self.assertEqual(spam.update(100, lambda: calls.append(0) or 'capture'), 'capture')
            for t in qa_spam.SLOTS[1:]:
                spam.update(100 + t, lambda: calls.append(t) or 'capture')
            spam.update(700)
            self.assertEqual(len(calls), 24)
            self.assertEqual(spam.state, 'complete')
            self.assertEqual(spam.saved, 24)

    def test_outage_skips_without_burst(self):
        with tempfile.TemporaryDirectory() as d, patch.object(qa_spam, 'CONTROL', Path(d)):
            (Path(d) / 'spam-start.json').write_text(json.dumps({'id':'test', 'created':time.time()}))
            spam = qa_spam.SpamCapture()
            spam.update(0)
            calls = []
            spam.update(45, lambda: calls.append(1) or 'capture')
            self.assertEqual(calls, [])
            self.assertEqual(spam.skipped, 3)
            self.assertEqual(spam.state, 'running')
