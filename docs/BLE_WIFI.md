# Bluetooth Wi-Fi control

The Pi runs a BLE GATT service independently of capture, GPS and HTTP. Mac and
future iOS clients use the same protocol. This does not implement iOS UI or DJI
landing detection. Only command Wi-Fi ON after landing and motors stopping.
Bluetooth remains active during flight; interference suitability is unverified.

## Install on a grounded Pi

Existing `tilik-hotspot.service` and `tilik-boot-hotspot` NetworkManager profile
are required. Run `sudo sh deploy/install-ble-wifi.sh`. Installation preserves
current Wi-Fi state, capture data and GPS service. Optional BLE dependencies
live in `/opt/tilik-ble-venv`; capture remains standard-library-only.
For a Pi without internet, supply a directory containing compatible Linux Python
wheels as the installer’s first argument: `sudo sh deploy/install-ble-wifi.sh /tmp/tilik-ble-wheels`.
Server versions: bless 0.2.6 and bleak 0.22.3; the Mac client uses its own bleak environment.
The installer refuses active/enabled legacy USB/automatic hotspot togglers.
Do not run other Wi-Fi controllers concurrently.

A 32-byte random key is created once at `/etc/tilik-ble.key` (root, mode 600).
Provision it over trusted SSH to a private client file (mode 600), never Git,
logs or chat. Future iOS provisioning should store this key in Keychain.
Any holder of the key can toggle Wi-Fi; revoke by rotating the key and restarting
`tilik-ble`. Authentication does not encrypt public status, nor prevent radio
jamming/connection denial. Bluetooth pairing alone is not authorization.

## Mac utility

```sh
python3 -m venv ~/.venvs/tilik-ble
~/.venvs/tilik-ble/bin/pip install bleak
~/.venvs/tilik-ble/bin/python tools/ble-wifi.py status --key-file ~/.config/tilik/ble.key
~/.venvs/tilik-ble/bin/python tools/ble-wifi.py off --key-file ~/.config/tilik/ble.key
~/.venvs/tilik-ble/bin/python tools/ble-wifi.py on --key-file ~/.config/tilik/ble.key
```

Allow Bluetooth access for the terminal/app running the utility. With multiple
Pis, select the Mac BLE identifier using `--device`. Commands do not need Wi-Fi
or SSH once provisioned. `off` stops the boot-hotspot service and disables Wi-Fi
(client and AP). `on` restarts the boot-hotspot service, which explicitly enables
Wi-Fi and brings up its configured AP. Status queries actual NetworkManager state.
A successful BLE write alone does not prove success: poll the matching result.
If activation fails, BLE remains available to query/retry; there is no radio timer.

## Protocol v1

Name: `Tilik-Control`
Service: `e7d60001-72a3-4f9e-9148-7d1a6c8b2100`
Read/write characteristic: `e7d60002-72a3-4f9e-9148-7d1a6c8b2100`

Read JSON contains `nonce`, `busy`, `request` (last accepted nonce), `result`.
Write UTF-8 JSON with `command` (`on`, `off`, `status`), `nonce`, and `mac`:
hex HMAC-SHA256 over UTF-8 `nonce:command`, using the decoded 32-byte key.
Challenge expires in 60 seconds and is consumed before command execution.
Requests are serialized, max 512 bytes, and invalid/replayed commands do nothing.
Use write-with-response and long writes/negotiated MTU for the JSON payload.
Poll reads until `request` matches and `busy` is false; inspect `result.error`.
Service restart rotates challenges and does NOT change Wi-Fi state.

## Recovery and verification

The existing boot-hotspot unit still enables Wi-Fi at boot. This is NOT an
in-flight reboot-safe radio interlock. Do not deploy for flight until restart
policy and Bluetooth interference have been validated on actual hardware.
Never use `rfkill block all`: that also removes Bluetooth recovery.
Test grounded: authenticated status, OFF/ON round trip, wrong-key rejection,
service restart, and verify capture/GPS continuity. Keep Ethernet or physical
access available during initial commissioning. Do not unplug power to toggle.

Rollback: `sudo systemctl disable --now tilik-ble.service`. This does not change
Wi-Fi; `sudo systemctl restart tilik-hotspot.service` restores the AP explicitly.

## Commissioning evidence (2026-11-11)

Mac-to-Pi BLE authenticated STATUS succeeded; OFF reported Wi-Fi disabled and AP
inactive. ON initially reported a system-command failure; a subsequent authenticated
STATUS confirmed Wi-Fi enabled and AP active. Pi logs then established that
NetworkManager initially rejected AP activation because wlan0 was not ready;
the existing boot unit's 15-second retry restored it. The controller now enables
Wi-Fi, waits for wlan0 readiness, then activates the hotspot. Its subprocess wait
also exceeds the hotspot unit's 50-second startup timeout. A wrong-key request
was rejected on the real Pi. Capture retained PID 809 and its original start
time through the radio cycle; the same PID retained the GPS serial port.
This verifies process continuity, not GPS fix quality. The final readiness fix
has unit coverage; the complete radio cycle with that fix must be retested on
the ground. These results do not establish in-flight RF safety or iOS integration.

Debug clients may authenticate `reboot` or `shutdown` with the same nonce/HMAC
protocol. A successful result contains `power_action` and `scheduled: true`;
this means systemd accepted a 10-second delayed action, not that shutdown or
reboot completed. Capture stops when the device powers down. The current boot
hotspot service enables Wi-Fi after reboot; only test on the ground.

## Scheduled thermal capture QA

Authenticated `spam_start` starts a 10-minute schedule, independent of video QA.
Frames are saved at 0–280 seconds every 20 seconds, then 300/320/340,
420/440/460, and 540/560/580. `spam_status` reports saved, failed and skipped.
The normal rule-based hotspot monitor continues; unlike commit 11b379a, the
camera service does not stop after the schedule. Missed slots are skipped.
A service restart interrupts the schedule; it is not resumed after reboot.
