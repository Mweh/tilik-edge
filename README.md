> Repository runtime: https://github.com/Mweh/tilik-edge — see [Mac and Raspberry Git workflow](GIT_WORKFLOW.md). Local Pi settings belong in ignored `config/pi.local.json` and `config/rules.local.json`.

# Tilik Edge — thermal capture + iOS sync

For a newly flashed Pi, follow [Fresh Pi setup](deploy/FRESH_PI.md).

One project, two processes, one shared capture store. The capture worker owns the
USB camera and evaluates rules continuously. The API serves committed bundles,
shows the live preview, and accepts rule changes. Closing the browser does not
stop capture. Existing `landfill-pi` and `leptontest` projects are not modified.

Requires Python 3.9+. Live capture runs on Raspberry Pi/Linux with `ffmpeg` and
`v4l-utils`. The core app uses the Python standard library. NumPy is optional for
importing legacy NPY captures only. Native Mac USB capture is deliberately disabled.

## Try on Mac (simulation only)

From this directory:

```bash
sh start.sh
```

The launcher works from any current directory; an optional first argument selects
a different shared config file.

Open http://127.0.0.1:8080. Both processes start from the same configuration.
The default simulation has a hot region between seconds 3–10 of a 40-second cycle;
the default rule requires persistence, so allow about 6 seconds for the first
capture. The dashboard labels it simulation. Ctrl+C stops both processes.
`run` is a development supervisor: if either child exits, it stops the other and
returns an error. Use the independent systemd services for ongoing Pi operation.

The dashboard provides numeric condition fields, a bench preset, Apply conditions,
current state, and saved PNG downloads. It saves validated rules atomically. The
capture worker applies them on its next frame, resetting persistence while
preserving capture cooldown. Preview stale state is visible. Conditions affect
actual capture and subsequent API results, not a separate browser simulation.

## Copy to Raspberry Pi

Run on the Mac, replacing the hostname if necessary:

```bash
scp -r "yourpath/Tilik/edge/tilik-edge" pi@raspberrypi.local:~/
```

Copy source before running simulation, or exclude the local `data/` folder if
using rsync. Never clone one Pi's data/device-id onto another physical Pi.

On the Pi:

```bash
cd ~/tilik-edge
sudo apt-get install ffmpeg v4l-utils
```

Stop the old camera worker/GUI and the old API on port 8080. Do not run two camera
owners. Edit `config/edge.json`:

- `source`: `live`
- `device`: the verified camera device, preferably a stable `/dev/v4l/by-id/...`
- `confirm_centikelvin`: `true` **only after** verifying TLinear 0.01 K, AGC off,
  telemetry off, and the expected 160×120 Y16 stream
- `host`: `0.0.0.0` for access from the iPhone

Then:

```bash
python3 -m edge run --config config/edge.json
```

Both services now use `data/captures/live`. The API, dashboard, and iOS all read
these same captures. Open `http://PI_IP:8080`. Existing iOS clients can use that
address and the existing `/api/captures` endpoints. This does not install or modify
the iOS app itself.

For remote rule editing on a trusted LAN, configure an API token:

```bash
export TILIK_API_TOKEN='replace-with-a-long-random-secret'
python3 -m edge run --config config/edge.json
```

Enter it in the dashboard. API clients must send `Authorization: Bearer TOKEN`.
The earlier POC iOS client has no token field; leave token mode off only for the
controlled LAN compatibility test, or add token support to that client first.
With no token, rule writes are permitted only from loopback. Use a tunnel for
local control: `ssh -L 8081:127.0.0.1:8080 pi@raspberrypi.local`, then open
http://127.0.0.1:8081. Capture reads remain accessible on the configured interface.

## Install independent boot services

```bash
cd ~/tilik-edge
sudo sh deploy/install.sh
```

Installation does not start services or stop old software. Review
`/etc/tilik-edge.json`; it initially uses simulation. Configure live input as above.
It uses `/var/lib/tilik-edge` for identity, captures, catalog, and writable rules.
Existing installed configuration is preserved on repeat installation.

```bash
sudo systemctl enable --now tilik-capture tilik-api
sudo systemctl status tilik-capture tilik-api
journalctl -u tilik-capture -u tilik-api -f
```

API failure does not stop recording; capture failure does not prevent reading
stored captures. To apply source/device/network changes, restart the relevant
services. Rule changes take effect without restart.

```bash
sudo systemctl restart tilik-capture tilik-api
```

For token mode, set `TILIK_API_TOKEN=...` in `/etc/tilik-edge.env`, protect the file
with `sudo chmod 600 /etc/tilik-edge.env`, and restart `tilik-api`.

## Bring old captures forward

The migration command copies bundles; it never changes or deletes originals.
Use only on the Pi whose historical captures you are importing. Old bundles with
no device identity are assigned this Pi's persistent ID. Repeated imports skip
identical captures and reject conflicting content.

For a development checkout:

```bash
python3 -m edge.import_legacy ~/landfill-pi/captures --config config/edge.json --confirm-centikelvin
python3 -m edge.import_legacy ~/leptontest/captures --config config/edge.json
```

Use `--confirm-centikelvin` only for legacy nested bundles whose radiometry you have
verified. Flat leptontest bundles must explicitly declare calibrated radiometry.
NPY import requires NumPy in the interpreter running the import. Missing,
uncalibrated, or malformed artifacts are reported and left in the old project;
they are not converted into fake measurements. Original metadata is retained
inside `legacy_metadata`. The command exits nonzero if any bundle was rejected.

For systemd installation, import into the **installed** store instead:

```bash
cd ~/tilik-edge
sudo python3 -m edge.import_legacy /home/pi/landfill-pi/captures --config /etc/tilik-edge.json --confirm-centikelvin
sudo chown -R tilik:tilik /var/lib/tilik-edge
```

Import before starting services, or stop both briefly during that ownership change.
Do not point the new catalog directly at old schema-1 bundles: use the importer.
The previous POC used path-hash identities. The new device/event identities differ,
so a POC phone library may show one-time duplicate historical captures on first
migration. Production iOS must reconcile that transition; the new API alone cannot
rewrite the phone's old cache.

## Data/API contract

- `GET /api/status`: device identity, capture heartbeat, camera state, index errors.
- `GET /api/captures`: legacy array response, newest published first.
- `GET /api/captures/{id}`: original normalized metadata JSON.
- `GET /api/captures/{id}/preview` (or `/visual`): PNG.
- `GET /api/captures/{id}/raw`: 38,400-byte little-endian radiometric BIN.
- `GET /api/captures/{id}/manifest`: sizes and SHA-256 hashes.
- `GET /api/v1/captures?after=0&limit=100`: indexed page, `next_cursor`, `snapshot`.
- `GET/POST /api/rules`: fetch/apply conditions; no fake trigger endpoint.

For pagination, retain the first page's snapshot across all its pages; advance
`after` using `next_cursor`. Once that snapshot is exhausted, poll using the last
cursor **without** the snapshot to discover newer captures. The catalog sequence
is persistent across process restart; if the store is rebuilt, clients must reset
the cursor and rely on stable IDs for deduplication. Legacy array clients should
migrate to pagination for large capture histories.

Camera health is separate from API reachability. Simulation never reports a
physical camera connected. A missing/stale heartbeat reports unknown camera state.

The capture worker writes `.pending` bundles, fsyncs artifacts, and publishes by
rename. The API excludes hidden directories. Catalog indexing checks PNG CRCs,
decoded frame length, raw length, explicit radiometry declaration, and schema.
The SQLite catalog stores immutable hashes and a publication sequence. Each file
is checked again before transfer. Directory scans happen on a background interval,
not per HTTP request; unchanged bundles are not re-read. Keep data/device-id and
catalog.sqlite when moving/upgrading the installation.

## Validation and remaining release work

```bash
python3 -m unittest discover -s tests -v
```

The current suite contains 18 passing tests, including a combined-launcher
end-to-end simulation with verified downloads and clean shutdown.
Tests cover the rule engine, browser-independent capture, live rule reload,
pending exclusion, malformed PNG/raw, source/temperature mapping, identity after
move/restart, immutability, pagination, legacy import, API downloads and auth.
These tests use simulated frames and temporary local storage, never Mac USB.

This consolidation still needs a physical Pi + PureThermal + iPhone acceptance
run. The installer and Linux service behavior must be validated on the Pi. The
browser JavaScript is syntax-checked; visual interaction has not been exercised
in a real browser in this session.

HTTP compatibility mode is for a controlled LAN. For encrypted deployment, configure
both `tls_cert` and `tls_key` in the shared JSON (absolute paths or relative to the
config directory), supply a certificate trusted by the iPhone for the chosen
hostname, and use HTTPS plus token authentication. No certificate bypass is
implemented. Offline pairing/certificate provisioning and iOS credential UI remain
production integration work; enabling a token alone does not encrypt HTTP.

No automatic capture deletion is performed. Capture stops when disk free space
falls below 256 MB. Keep legacy projects until hardware acceptance succeeds.
For rollback, stop `tilik-capture` and `tilik-api`, then restart the previous
capture/API commands with their original stores.

## Optional boot hotspot and USB switch

See [deploy/HOTSPOT.md](deploy/HOTSPOT.md) to start `Tilik-Drone-Payload` at boot and toggle Wi-Fi on each nonthermal USB insertion. Install locally on the Pi; no automatic network changes are performed by Git updates.

## API autostart and network changes

On an existing Pi checkout, preserve the current capture directory and device ID:

```sh
cd ~/tilik-edge
sudo python3 deploy/install-autostart.py
# Stop the old start.sh process first, then:
sudo systemctl restart tilik-api
sudo systemctl status tilik-api --no-pager
```

Add `--capture` to install/enable an independent camera service too; then restart
both `tilik-api tilik-capture` after stopping the old combined process. The source
configuration is `config/pi.local.json`, falling back to `config/edge.json`, or an
explicit `--config /path/to/existing.json`. A root-owned runtime copy is written
to `/etc/tilik-runtime.json`; edits to the source config require rerunning this
installer. Capture files and device ID stay in the original data directory.
The runtime user defaults to the sudo caller; use `--user pi` when installing as
root. Runtime API tokens can be supplied in `/etc/tilik-edge.env` (root-only).

The API starts at boot even without Wi-Fi and listens on `0.0.0.0`. It stays alive
when Wi-Fi is disabled, when changing router SSIDs, and when entering hotspot
mode. It is reachable over any connected IPv4 interface, not restricted to a
particular SSID. No NetworkManager dispatcher restart is needed. Capture remains
independent from connectivity. systemd restarts the API if the process fails.

While a network is down, clients cannot reach the Pi and in-progress HTTP requests
can fail. After reconnection, iPhone/Mac must be on the same reachable network.
Use Bonjour discovery or `http://raspberrypi.local:8080` rather than a saved IP.
The iOS Retry action rediscovers the endpoint and resumes by skipping valid local
bundles. The API process surviving does not make an interrupted HTTP request
survive. Bonjour uses `--no-fail` to reconnect if Avahi itself restarts. Install
`avahi-daemon avahi-utils` for discovery; hostname alone depends on mDNS support.

Do not run `sh start.sh` alongside these services: it starts a second API/camera.
Check logs with `sudo journalctl -u tilik-api -b -n 60 --no-pager`.

## Automatic camera recovery

See [Camera lifecycle and deployment](docs/CAMERA_LIFECYCLE.md): API stays available for iOS downloads; live capture waits for verified PureThermal USB, reconnects and warms up automatically without a Mac.

## Manual capture over hotspot or Wi-Fi

Open the dashboard on any reachable Pi IPv4 address, for example `http://10.42.0.1:8080/` on Tilik-Payload or the router-assigned Pi IP. Manual capture is permitted over the network by default; an SSH tunnel is not required for that action. The caller still needs a ready camera; an unplugged/warming-up camera returns503. Configured API tokens remain required.

Without a token, rule changes and clock/session administration remain local-only. The dashboard reports `controls.capture_allowed` separately from `controls.allowed`, which continues to mean privileged rule/control access. Cross-origin browser writes remain rejected. To restore local-only manual capture, set the JSON boolean `"allow_remote_capture": false` in the active runtime config and restart only `tilik-api`. All network participants able to reach an unauthenticated API can request captures when this setting is enabled.
