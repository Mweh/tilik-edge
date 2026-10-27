# Fresh Raspberry Pi installation

Use Raspberry Pi OS Lite 64-bit, enable SSH, and configure the actual Wi-Fi in
Imager. Stop any manually started `start.sh` before installing the services.
This setup does not modify EEPROM, boot files, Wi-Fi profiles, or USB toggles.

On the Pi:

```sh
sudo apt-get update
sudo apt-get install -y git
cd ~
git clone https://github.com/Mweh/tilik-edge.git
cd tilik-edge
sudo sh deploy/setup-pi.sh
```

The first run uses clearly labeled simulation, creates `config/pi.local.json`,
`config/rules.local.json`, and `data/`, and starts independent boot services.
Rerunning preserves local rules, captures, and device identity. Do not run
`start.sh` alongside these services.

## Activate the real camera

```sh
v4l2-ctl --list-devices
ls -l /dev/v4l/by-id/
v4l2-ctl -d /dev/video0 --list-formats-ext
```

Select the PureThermal capture node with **Y16, 160x120**, preferably its stable
`/dev/v4l/by-id/...-video-index0` symlink, not the metadata-only interface.
Y16 alone does not prove temperature calibration. Verify the camera is configured
for **TLinear 0.01 K, AGC off, telemetry off** before confirming. Raw values in that
mode use `C = raw / 100 - 273.15`; plausible temperatures alone are not calibration.

```sh
sudo sh deploy/setup-pi.sh --device /dev/v4l/by-id/YOUR_CAMERA_NODE --confirm-centikelvin
sudo journalctl -u tilik-capture -b -n 40 --no-pager
curl http://127.0.0.1:8080/api/status
```

No automatic live-to-simulation fallback is used. If the camera is unplugged, the
capture service retries while the API keeps serving previous captures.

## Rules and QA

Default rules are copied from `config/landfill.json` into the ignored local file.
These are configurable detection heuristics, not proof of a landfill fire.
Keep actual site validation and radiometric calibration separate from UI testing.
Use the dashboard bench/custom preset for a supervised test, then restore site
rules. Changing rules must affect the same running capture worker.

Without an API token, rule writes are loopback-only. On the Mac:

```sh
ssh -N -L 8081:127.0.0.1:8080 pi@raspberrypi.local
```

Open `http://127.0.0.1:8081`. View the normal dashboard on the local network at
`http://raspberrypi.local:8080`. With token mode enabled, clients must supply that
token; HTTP does not encrypt it. Use only a controlled LAN until TLS/pairing is set.

## iOS acceptance

- Pi and iPhone need reachable addresses on the same network; no fixed SSID.
- Allow Tilik Local Network access. The app discovers `_tilik-edge._tcp` through
  Bonjour, validates `/api/status`, then downloads PNG/JSON/BIN and verifies hashes.
- Router client isolation or blocked multicast can prevent discovery; a known
  Pi address is a diagnostic fallback, not a requirement for the normal UI.
- Test a real rule-triggered capture, sync it twice (second sync should skip it),
  interrupt Wi-Fi, reconnect and retry. Completed files must remain intact.
- Switching Wi-Fi can interrupt a request. API stays running; clients rediscover
  and retry. Captures are stored independently of Wi-Fi.

## Updates and health

```sh
cd ~/tilik-edge
git status --short
git pull --ff-only
sudo systemctl restart tilik-api tilik-capture
sudo systemctl status tilik-api tilik-capture --no-pager
vcgencmd get_throttled
vcgencmd measure_temp
```

Do not use `git reset --hard` to update a working device. If repository changes
include service/installer changes, rerun setup with the verified live camera
arguments, then inspect service logs. Do not enable automatic untested code pulls
on a flying payload. Commit reviewed changes on the Mac and deploy intentionally.
Local configuration/data are excluded from Git and need a separate backup.
