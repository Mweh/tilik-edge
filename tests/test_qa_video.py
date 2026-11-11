import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import hotspot.qa_video as video

class VideoTests(unittest.TestCase):
    def test_schedule_does_not_touch_camera_and_expired_request_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / 'control'; control.mkdir()
            with patch.object(video, 'CONTROL', control):
                qa = video.VideoQA(Path(tmp))
                (control / 'start.json').write_text(json.dumps({'id': 'test', 'created': time.time()}))
                qa.update(10)
                self.assertEqual(qa.status()['state'], 'running')
                qa.update(10 + 901)
                self.assertEqual(qa.status()['state'], 'idle')
                self.assertEqual(len(qa.schedule.missed) if qa.schedule else 5, 5)
                (control / 'start.json').write_text(json.dumps({'id': 'old', 'created': time.time() - 100}))
                qa.update(920)
                self.assertIsNone(qa.schedule)

    def test_missing_encoder_does_not_crash_monitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / 'control'; control.mkdir()
            with patch.object(video, 'CONTROL', control), patch.object(video.shutil, 'which', return_value=None):
                qa = video.VideoQA(Path(tmp))
                (control / 'start.json').write_text(json.dumps({'id': 'test', 'created': time.time()}))
                qa.update(1)
                qa.update(121, [30000]*19200)
                qa.worker.join(timeout=2)
                self.assertEqual(qa.records[0]['state'], 'failed')
                self.assertIsNotNone(qa.schedule)

    def test_inferno_palette_matches_image_renderer(self):
        from hotspot.engine import inferno_colormap
        for value in (0, 64, 128, 192, 255):
            self.assertEqual(video.PALETTE[value], bytes(inferno_colormap(value / 255)))
        self.assertNotEqual(video.PALETTE[128][0], video.PALETTE[128][1])

    def test_immediate_starts_without_two_minute_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / 'control'; control.mkdir()
            with patch.object(video, 'CONTROL', control), patch.object(video.shutil, 'which', return_value=None):
                qa = video.VideoQA(Path(tmp))
                (control / 'start.json').write_text(json.dumps({'id': 'test', 'created': time.time(), 'immediate': True}))
                qa.update(100, [30000] * 19200)
                self.assertEqual(len(qa.records), 1)
                self.assertEqual(qa.records[0]['minute'], 2)
                qa.worker.join(timeout=2)
                qa.update(105.1)
                self.assertIsNone(qa.schedule)

    def test_immediate_keeps_existing_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / 'control'; control.mkdir()
            with patch.object(video, 'CONTROL', control), patch.object(video.shutil, 'which', return_value=None):
                qa = video.VideoQA(Path(tmp))
                (control / 'start.json').write_text(json.dumps({'id': 'scheduled', 'created': time.time()}))
                qa.update(0)
                schedule = qa.schedule
                (control / 'start.json').write_text(json.dumps({'id': 'manual', 'created': time.time(), 'immediate': True}))
                qa.update(10, [30000]*19200)
                qa.worker.join(timeout=2)
                qa.update(15.1)
                self.assertIs(qa.schedule, schedule)
                self.assertEqual(qa.session, 'scheduled')
