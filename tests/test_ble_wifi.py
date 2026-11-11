import json
import subprocess
import unittest
from edge.ble_wifi import Controller, signature


class BLETests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.now = 0
        self.key = b'x' * 32
        self.wifi = 'enabled'
        def run(*args):
            self.calls.append(args)
            if args[-3:] == ('radio', 'wifi', 'off'):
                self.wifi = 'disabled'
            if args == ('systemctl', 'restart', 'tilik-hotspot.service'):
                self.wifi = 'enabled'
            if args[-1] == 'general':
                return self.wifi
            return 'tilik-boot-hotspot' if self.wifi == 'enabled' else ''
        self.c = Controller(self.key, run, lambda: self.now)

    def packet(self, command='off', key=None):
        nonce = self.c.nonce
        return json.dumps(dict(command=command, nonce=nonce,
                               mac=signature(key or self.key, nonce, command))).encode()

    def test_off_stops_boot_retry_before_radio_and_never_capture(self):
        self.c.execute(self.c.accept(self.packet()))
        self.assertEqual(self.calls[:2], [('systemctl', 'stop', 'tilik-hotspot.service'),
                                         ('nmcli', '--wait', '10', 'radio', 'wifi', 'off')])
        self.assertEqual(self.c.result, {'wifi': 'disabled', 'hotspot': False})
        self.assertNotIn('tilik-capture', repr(self.calls))

    def test_on_restarts_hotspot_and_checks_actual_state(self):
        self.c.execute(self.c.accept(self.packet('on')))
        self.assertEqual(self.c.result, {'wifi': 'enabled', 'hotspot': True})

    def test_replay_and_busy_rejected(self):
        packet = self.packet()
        self.assertEqual(self.c.accept(packet), 'off')
        self.assertIsNone(self.c.accept(packet))
        self.c.execute('off')
        self.assertIsNone(self.c.accept(packet))

    def test_wrong_key_expired_and_malformed_never_execute(self):
        self.assertIsNone(self.c.accept(self.packet(key=b'wrong')))
        for value in [b'null', b'[]', b'{}', b'invalid', b'x'*513]:
            self.assertIsNone(self.c.accept(value))
        packet = self.packet()
        self.now = 61
        self.assertIsNone(self.c.accept(packet))
        old = self.c.nonce
        self.c.read()
        self.assertNotEqual(old, self.c.nonce)
        self.assertEqual(self.calls, [])

    def test_failure_consumes_request_and_reports_failure(self):
        packet = self.packet()
        command = self.c.accept(packet)
        def fail(*args):
            raise subprocess.TimeoutExpired(args, 45)
        self.c.run = fail
        self.c.execute(command)
        self.assertFalse(self.c.busy)
        self.assertIn('error', self.c.result)
        self.assertIsNone(self.c.accept(packet))

    def test_status_does_not_mutate_radio(self):
        self.c.execute(self.c.accept(self.packet('status')))
        self.assertFalse(any('systemctl' in call or 'radio' in call for call in self.calls))

    def test_on_does_not_claim_success_without_ap(self):
        self.c.run = lambda *args: 'enabled' if args[-1] == 'general' else ''
        self.c.execute(self.c.accept(self.packet('on')))
        self.assertEqual(self.c.result['error'], 'hotspot_not_ready')
