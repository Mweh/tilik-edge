import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('wifi_usb', Path(__file__).parents[1] / 'deploy/wifi_usb.py')
wifi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wifi)


class USBToggleTests(unittest.TestCase):
    def test_baseline_unplug_replug_and_debounce(self):
        old, new = ('1-2', '1', '3'), ('1-2', '1', '4')
        watcher = wifi.Insertions({old})
        self.assertEqual(watcher.update({old}, 0), [])
        self.assertEqual(watcher.update(set(), 1), [])
        self.assertEqual(watcher.update({new}, 2), [])
        self.assertEqual(watcher.update({new}, 4), [new])
        self.assertEqual(watcher.update({new}, 10), [])

    def test_unstable_device_does_not_toggle(self):
        device = ('1-2', '1', '3')
        watcher = wifi.Insertions(set())
        watcher.update({device}, 0)
        self.assertEqual(watcher.update(set(), 1), [])
        self.assertEqual(watcher.update({device}, 2), [])
        self.assertEqual(watcher.update({device}, 3), [])
        self.assertEqual(watcher.update({device}, 4), [device])

    def test_excludes_camera_hub_interfaces_and_custom_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def add(name, vendor='1234', product='5678', label='Flash drive', cls='00'):
                path = root / name
                path.mkdir()
                for key, value in {'idVendor': vendor, 'idProduct': product, 'product': label,
                                   'bDeviceClass': cls, 'busnum': '1', 'devnum': name}.items():
                    (path / key).write_text(value)
            add('1-1', '1e4e', '0100', '')
            add('1-2', label='PureThermal (fw:v1.3.0)')
            add('1-3', cls='09')
            add('1-4:1.0')
            add('usb1')
            add('1-5', product='9999')
            add('1-6')
            self.assertEqual(wifi.devices(root, ['1234:9999']), {('1-6', '1', '1-6')})

    def test_each_new_device_emits_once(self):
        watcher = wifi.Insertions(set())
        devices = {('1-2', '1', '2'), ('1-3', '1', '3')}
        self.assertEqual(watcher.update(devices, 0), [])
        self.assertEqual(len(watcher.update(devices, 2)), 2)
        self.assertEqual(watcher.update(devices, 3), [])

    def test_toggle_controls_radio_and_reactivates_hotspot(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state.json'
            with patch.object(wifi, 'STATE', state), patch.object(wifi, 'nm') as nm:
                settings = {'connection_uuid': 'test-uuid', 'interface': 'wlan0'}
                wifi.activate(settings, False)
                nm.assert_called_once_with('radio', 'wifi', 'off')
                self.assertIn('false', state.read_text())
                nm.reset_mock()
                wifi.activate(settings, True)
                self.assertEqual(nm.call_args_list[0].args, ('radio', 'wifi', 'on'))
                self.assertEqual(nm.call_args_list[1].args, ('connection', 'up', 'uuid', 'test-uuid', 'ifname', 'wlan0'))
                self.assertIn('true', state.read_text())
