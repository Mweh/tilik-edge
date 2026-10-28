import configparser
import importlib.util
from pathlib import Path
import unittest
import uuid

spec=importlib.util.spec_from_file_location('boot_hotspot',Path(__file__).parents[1]/'deploy/install-boot-hotspot.py')
boot=importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot)


class BootHotspotTests(unittest.TestCase):
    def test_secured_shared_ap_with_stable_address_and_no_usb_trigger(self):
        identity=str(uuid.uuid4())
        text=boot.keyfile('test-password',identity,'wlan0')
        parser=configparser.ConfigParser(interpolation=None)
        parser.read_string(text)
        self.assertEqual(parser['wifi']['ssid'],'Tilik-Payload')
        self.assertEqual(parser['wifi']['mode'],'ap')
        self.assertEqual(parser['wifi-security']['key-mgmt'],'wpa-psk')
        self.assertEqual(parser['ipv4']['address1'],'10.42.0.1/24')
        self.assertEqual(parser['connection']['uuid'],identity)
        unit=boot.service(identity,'wlan0')
        self.assertIn('Restart=on-failure',unit)
        self.assertIn('RemainAfterExit=yes',unit)
        self.assertIn('WantedBy=multi-user.target',unit)
        self.assertNotIn('wifi_usb',unit)
        self.assertNotIn('radio wifi off',unit)
        self.assertNotIn('network-online.target',unit)

    def test_password_and_interface_validation(self):
        identity=str(uuid.uuid4())
        for password in ('short',' password123','password123 ','hello\nworld','é'*10):
            with self.assertRaises(ValueError):boot.keyfile(password,identity,'wlan0')
        for interface in ('wlan0\nExecStart=bad','wlan0 foo',''):
            with self.assertRaises(ValueError):boot.keyfile('test-password',identity,interface)
        with self.assertRaises(ValueError):boot.keyfile('test-password','not-a-uuid','wlan0')

    def test_keyfile_escapes_backslashes_without_interpolation(self):
        text=boot.keyfile('hello\\world%',str(uuid.uuid4()),'wlan0')
        self.assertIn('psk=hello\\\\world%',text)
