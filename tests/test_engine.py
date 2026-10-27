import json
import tempfile
import unittest
from pathlib import Path
from hotspot.engine import *

class RulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = RadiometricAutoCaptureEngine(mode=TriggerMode.LANDFILL_PROD, debounce_frames=3,
            persistence_seconds=1, cooldown_seconds=2, output_dir=self.tmp.name, source='simulation')
    def tearDown(self):
        self.tmp.cleanup()
    def frame(self, temp=70, x=10, side=3, bg=30):
        raw=[celsius_to_centikelvin(bg)]*19200
        for y in range(10,10+side):
            for xx in range(x,x+side): raw[y*160+xx]=celsius_to_centikelvin(temp)
        return raw
    def test_duration_capture_and_rearm(self):
        e=self.engine
        for t in (0,.2,.4): self.assertIsNone(e.process_frame(self.frame(),clock=t)['action_taken'])
        result=e.process_frame(self.frame(),clock=1)
        folder=Path(result['capture_path'])
        self.assertEqual((folder/'frame_raw.bin').stat().st_size,38400)
        self.assertEqual((folder/'frame_visual.png').read_bytes()[:8],b'\x89PNG\r\n\x1a\n')
        self.assertEqual(json.loads((folder/'metadata.json').read_text())['source'],'simulation')
        self.assertIsNone(e.process_frame(self.frame(),clock=5)['action_taken'])
        self.assertEqual(e.process_frame(self.frame(30),clock=6)['state'],'IDLE')
        for t in (7,7.5):e.process_frame(self.frame(),clock=t)
        self.assertEqual(e.process_frame(self.frame(),clock=8)['action_taken'],'AUTO_CAPTURED')
    def test_noise_and_moving_regions(self):
        e=self.engine
        self.assertEqual(e.process_frame(self.frame(side=1),clock=0)['hotspot_count'],0)
        for t,x in enumerate((10,30,50,70)):
            self.assertIsNone(e.process_frame(self.frame(x=x),clock=t)['action_taken'])
        self.assertEqual(e.consecutive_exceed_count,1)
    def test_hot_background_and_invalid(self):
        self.assertEqual(self.engine.compute_dynamic_threshold([60]*19200)[0],70)
        with self.assertRaises(ValueError):self.engine.process_frame([0]*19200)
        with self.assertRaises(ValueError):self.engine.process_frame([30000])
        with self.assertRaises(ValueError):RadiometricAutoCaptureEngine(cooldown_seconds=float('nan'))
    def test_write_failure_retries(self):
        e=self.engine
        e._save_capture_bundle=lambda **kwargs: (_ for _ in ()).throw(OSError('disk full'))
        e.process_frame(self.frame(),clock=0);e.process_frame(self.frame(),clock=.5)
        with self.assertRaises(OSError):e.process_frame(self.frame(),clock=1)
        self.assertNotEqual(e.state,CaptureState.COOLDOWN)
        self.assertEqual(e.total_captures,0)

    def test_gap_resets_persistence(self):
        e=self.engine
        e.process_frame(self.frame(),clock=0)
        e.process_frame(self.frame(),clock=.5)
        r=e.process_frame(self.frame(),clock=4)
        self.assertIsNone(r['action_taken'])
        self.assertEqual(e.consecutive_exceed_count,1)
    def test_wall_clock_jump_does_not_trigger_early(self):
        e=self.engine
        e.process_frame(self.frame(),timestamp=1000,clock=0)
        e.process_frame(self.frame(),timestamp=100000,clock=.1)
        self.assertIsNone(e.process_frame(self.frame(),timestamp=1,clock=.2)['action_taken'])
        self.assertEqual(e.process_frame(self.frame(),timestamp=2,clock=1)['action_taken'],'AUTO_CAPTURED')
    def test_upper_area_gate(self):
        e=self.engine
        e.max_cluster_pixels=8
        self.assertEqual(e.process_frame(self.frame(side=3),clock=0)['hotspot_count'],0)
    def test_inclusive_temperature_boundary(self):
        e=self.engine
        self.assertEqual(e.process_frame(self.frame(temp=55),clock=0)['hotspot_count'],1)
        self.assertEqual(e.process_frame(self.frame(temp=54.99),clock=.1)['hotspot_count'],0)

    def combined(self, *xs):
        raw = self.frame(temp=30)
        for x in xs:
            for y in range(10, 13):
                for xx in range(x, x+3):
                    raw[y*160+xx] = celsius_to_centikelvin(70)
        return raw

    def test_new_cluster_not_saved_as_persistent(self):
        e = self.engine
        e.process_frame(self.combined(10), clock=0)
        e.process_frame(self.combined(10), clock=.5)
        result = e.process_frame(self.combined(10, 40), clock=1)
        self.assertEqual(result['action_taken'], 'AUTO_CAPTURED')
        metadata = json.loads((Path(result['capture_path'])/'metadata.json').read_text())
        self.assertEqual(metadata['hotspot_count'], 1)
        self.assertEqual(metadata['hotspots'][0]['bounding_box_pixels']['x'], 10)

    def test_parallel_cluster_keeps_own_start_when_first_disappears(self):
        e = self.engine
        e.persistence_seconds = 2
        e.process_frame(self.combined(10), clock=0)
        e.process_frame(self.combined(10, 40), clock=.5)
        e.process_frame(self.combined(40), clock=1)
        e.process_frame(self.combined(40), clock=2)
        self.assertEqual(e.process_frame(self.combined(40), clock=2.5)['action_taken'], 'AUTO_CAPTURED')

    def test_statistics_scaling(self):
        threshold, median, mad = self.engine.compute_dynamic_threshold([30, 32, 34, 36, 80])
        self.assertEqual((median, mad), (34, 2))
        self.assertEqual(threshold, 55)
        threshold, median, mad = self.engine.compute_dynamic_threshold([30, 35, 40, 45, 50])
        self.assertAlmostEqual(threshold, 40 + 3.5 * 1.4826 * 5)
