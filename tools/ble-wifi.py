#!/usr/bin/env python3
"""Mac BLE controller. Requires bleak. Key stays local; never transmitted."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edge.ble_wifi import SERVICE, CHARACTERISTIC, signature


async def main(args):
    from bleak import BleakClient, BleakScanner
    key = bytes.fromhex(Path(args.key_file).read_text().strip())
    devices = await asyncio.wait_for(
        BleakScanner.discover(timeout=8, service_uuids=[SERVICE]), timeout=25)
    if args.device:
        devices = [d for d in devices if d.address == args.device]
    if len(devices) != 1:
        for d in devices:
            print(d.address, d.name)
        raise SystemExit('Expected one Pi; use --device with its identifier if multiple are visible.')
    async with BleakClient(devices[0]) as client:
        state = json.loads(await client.read_gatt_char(CHARACTERISTIC))
        if state['busy']:
            raise SystemExit('Pi is busy; retry after the current command finishes.')
        nonce = state['nonce']
        packet = {'command': args.command, 'nonce': nonce,
                  'mac': signature(key, nonce, args.command)}
        await client.write_gatt_char(CHARACTERISTIC, json.dumps(packet).encode(), response=True)
        for _ in range(100):
            await asyncio.sleep(1)
            state = json.loads(await client.read_gatt_char(CHARACTERISTIC))
            if state['request'] == nonce and not state['busy']:
                print(json.dumps(state['result'], indent=2))
                if 'error' in state['result']:
                    raise SystemExit(1)
                return
            if state['request'] != nonce:
                raise SystemExit('Command not accepted: check key, challenge expiry, or another client.')
        raise SystemExit('Timed out; state unknown. Reconnect and request status.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['on', 'off', 'status'])
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--device')
    try:
        asyncio.run(main(parser.parse_args()))
    except asyncio.TimeoutError:
        raise SystemExit('Bluetooth timed out. Check macOS Bluetooth permission, Pi service, and range.')
