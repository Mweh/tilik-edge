import unittest
from hotspot.qa_video_schedule import VideoSchedule

class ScheduleTests(unittest.TestCase):
    def test_all_windows_and_stop(self):
        s = VideoSchedule(10)
        for offset in (120, 300, 480, 660, 840):
            self.assertEqual(s.update(10 + offset), ('start', offset, 10 + offset + 5))
            self.assertEqual(s.update(10 + offset + 1)[0], 'waiting')
        self.assertEqual(s.update(910)[0], 'complete')

    def test_missed_clips_never_start_late(self):
        s = VideoSchedule(0)
        self.assertEqual(s.update(125)[0], 'waiting')
        self.assertIn(120, s.missed)
        self.assertEqual(s.update(900)[0], 'complete')
        self.assertEqual(len(s.missed), 5)
