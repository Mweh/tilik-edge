import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from edge.commands import Commands
from hotspot.camera import LatestCamera, command
from unittest.mock import patch


class CommandsTest(unittest.TestCase):
    def test_claim_once_and_expire(self):
        with tempfile.TemporaryDirectory() as folder:
            commands = Commands(Path(folder))
            identifier = str(uuid.uuid4())
            commands.submit(identifier, 'live')
            commands.submit(identifier, 'live')
            self.assertIsNone(commands.claim('simulation'))
            self.assertEqual(commands.claim('live'), identifier)
            self.assertIsNone(commands.claim('live'))
            commands.finish(identifier, {'capture_id':'example'})
            self.assertEqual(commands.get(identifier)['state'], 'saved')
            another = str(uuid.uuid4()); commands.submit(another,'live')
            db = commands.connect()
            with db: db.execute('UPDATE requests SET created=? WHERE id=?',(time.time()-30,another))
            db.close()
            self.assertEqual(commands.get(another)['state'],'failed')
            self.assertIsNone(commands.claim('live'))

    def test_latest_frame_drops_backlog(self):
        class Fake:
            def __init__(self, device):
                self.value=0
                self.stop=threading.Event()
            def read(self):
                if self.stop.wait(.005): raise EOFError('closed')
                self.value+=1
                return [self.value]
            def close(self):self.stop.set()
        camera=LatestCamera('fake',factory=Fake)
        try:
            first=camera.read()[0]
            time.sleep(.08)
            second=camera.read()[0]
            self.assertGreater(second-first,1)
            self.assertGreater(camera.dropped,0)
        finally:camera.close()
        self.assertFalse(camera.thread.is_alive())


class CameraCommandTests(unittest.TestCase):
    def test_missing_ffmpeg_uses_raw_v4l2(self):
        with patch('hotspot.camera.sys.platform', 'linux'), patch('hotspot.camera.shutil.which', side_effect=lambda name: '/usr/bin/v4l2-ctl' if name == 'v4l2-ctl' else None):
            args = command('/dev/video0')
        self.assertEqual(args[0], 'v4l2-ctl')
        self.assertIn('--set-fmt-video=width=160,height=120,pixelformat=Y16 ', args)
        self.assertIn('--stream-to=-', args)
        self.assertNotIn('--stream-to-hdr', args)

    def test_ffmpeg_remains_preferred(self):
        with patch('hotspot.camera.sys.platform', 'linux'), patch('hotspot.camera.shutil.which', return_value='/usr/bin/ffmpeg'):
            args = command('/dev/video0')
        self.assertEqual(args[0], 'ffmpeg')
        self.assertEqual(args[args.index('-c:v')+1], 'copy')

    def test_no_capture_tools_has_actionable_error(self):
        with patch('hotspot.camera.sys.platform', 'linux'), patch('hotspot.camera.shutil.which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'ffmpeg or v4l2-ctl'):
                command('/dev/video0')
