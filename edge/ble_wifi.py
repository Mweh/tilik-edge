"""Authenticated BLE Wi-Fi control; never opens the camera or GPS device."""
import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import subprocess
import time
from pathlib import Path

SERVICE = 'e7d60001-72a3-4f9e-9148-7d1a6c8b2100'
CHARACTERISTIC = 'e7d60002-72a3-4f9e-9148-7d1a6c8b2100'


def signature(key, nonce, command):
    return hmac.new(key, f'{nonce}:{command}'.encode(), hashlib.sha256).hexdigest()


class Controller:
    def __init__(self, key, run=None, clock=time.monotonic, sleep=time.sleep):
        self.key, self.clock, self.sleep = key, clock, sleep
        self.run = run or self._run
        self.busy = False
        self.result = 'ready'
        self.request = None
        self.rotate()

    @staticmethod
    def _run(*args):
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              timeout=70).stdout.strip()

    def rotate(self):
        self.nonce = secrets.token_hex(16)
        self.expires = self.clock() + 60

    def accept(self, payload):
        if self.busy or len(payload) > 512:
            return None
        try:
            data = json.loads(payload)
            command, nonce = data['command'], data['nonce']
            if command not in ('on', 'off', 'status') or nonce != self.nonce:
                return None
            if self.clock() > self.expires:
                return None
            if not hmac.compare_digest(signature(self.key, nonce, command), data['mac']):
                return None
        except (ValueError, KeyError, TypeError):
            return None
        self.rotate()  # Consume before execution, including failed commands.
        self.busy, self.result, self.request = True, 'working', nonce
        return command

    def wait_wifi_ready(self):
        # nmcli radio on returns before the Linux Wi-Fi device is usable.
        for _ in range(30):
            state = self.run('nmcli', '-g', 'GENERAL.STATE', 'device', 'show', 'wlan0')
            code = state.split(' ', 1)[0]
            if code.isdigit() and int(code) >= 30:
                return
            self.sleep(0.5)
        raise TimeoutError('wlan0 did not become ready after enabling Wi-Fi')

    def execute(self, command):
        try:
            if command == 'off':
                # Stop the boot unit first so its retry cannot re-enable Wi-Fi.
                self.run('systemctl', 'stop', 'tilik-hotspot.service')
                self.run('nmcli', '--wait', '10', 'radio', 'wifi', 'off')
            elif command == 'on':
                self.run('nmcli', '--wait', '10', 'radio', 'wifi', 'on')
                self.wait_wifi_ready()
                self.run('systemctl', 'restart', 'tilik-hotspot.service')
            wifi = self.run('nmcli', '-t', '-f', 'WIFI', 'general')
            active = self.run('nmcli', '-t', '-f', 'NAME', 'connection', 'show', '--active')
            self.result = {'wifi': wifi, 'hotspot': 'tilik-boot-hotspot' in active.splitlines()}
            if command == 'on' and (wifi != 'enabled' or not self.result['hotspot']):
                self.result['error'] = 'hotspot_not_ready'
            if command == 'off' and wifi != 'disabled':
                self.result['error'] = 'wifi_not_disabled'
        except (subprocess.SubprocessError, OSError) as error:
            logging.warning('Wi-Fi command %s failed: %s', command, error)
            self.result = {'error': 'system_command_failed'}
        finally:
            self.busy = False

    def read(self):
        if not self.busy and self.clock() > self.expires:
            self.rotate()
        return bytearray(json.dumps({'nonce': self.nonce, 'busy': self.busy,
                                     'request': self.request, 'result': self.result}).encode())


async def serve(key):
    from bless import BlessServer, GATTCharacteristicProperties as P, GATTAttributePermissions as A
    controller = Controller(key)
    server = BlessServer(name='Tilik-Control', loop=asyncio.get_running_loop())
    tasks = set()

    async def execute(command):
        await asyncio.to_thread(controller.execute, command)

    def write(characteristic, value, **kwargs):
        command = controller.accept(bytes(value))
        if command:
            task = asyncio.create_task(execute(command))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    server.read_request_func = lambda characteristic, **kwargs: controller.read()
    server.write_request_func = write
    await server.add_new_service(SERVICE)
    await server.add_new_characteristic(SERVICE, CHARACTERISTIC, P.read | P.write,
                                        controller.read(), A.readable | A.writeable)
    await server.start()
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()


if __name__ == '__main__':
    key = bytes.fromhex(Path('/etc/tilik-ble.key').read_text().strip())
    if len(key) != 32:
        raise SystemExit('Expected a 32-byte key')
    asyncio.run(serve(key))
