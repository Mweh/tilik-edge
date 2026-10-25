import unittest
from unittest.mock import patch, Mock
from edge.discovery import advertise

class DiscoveryTests(unittest.TestCase):
    def test_loopback_does_not_advertise_unreachable_api(self):
        with patch('edge.discovery.shutil.which') as which:
            self.assertIsNone(advertise({'host': '127.0.0.1'}, 8080))
            which.assert_not_called()

    def test_advertises_actual_port_without_secrets(self):
        process = Mock()
        with patch('edge.discovery.shutil.which', return_value='/usr/bin/avahi-publish-service'), patch('edge.discovery.subprocess.Popen', return_value=process) as spawn:
            result = advertise({'host': '0.0.0.0', 'device_id': 'device-123', 'token': 'private-secret'}, 8766)
            self.assertIs(result, process)
            command = spawn.call_args.args[0]
            self.assertIn('_tilik-edge._tcp', command)
            self.assertIn('--no-fail', command)
            self.assertIn('8766', command)
            self.assertNotIn('private-secret', str(command))

    def test_missing_avahi_is_nonfatal(self):
        with patch('edge.discovery.shutil.which', return_value=None):
            self.assertIsNone(advertise({'host': '0.0.0.0'}, 8080))
