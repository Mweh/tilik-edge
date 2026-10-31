# Tilik — boot, PureThermal hotplug, and iOS sync

Audit/update: 31 October 2026. Local branch: `fix/camera-hotplug-lifecycle`.

## Intended user experience

| Event | Hotspot / Wi-Fi | API | Camera capture |
|---|---|---|---|
| Pi boots without camera | Existing Tilik-Payload boot service | Starts automatically | WAITING_CAMERA; no fake frames |
| Verified PureThermal streaming node appears | Unchanged | Remains available | Opens camera, WARMUP, then rule-based capture |
| PureThermal unplugged/stalls | Unchanged | Remains available; previous PNG/JSON/BIN downloadable | Closes failed stream; retries without manual restart |
| PureThermal replugged | Unchanged | Remains available | Rediscovers, warms up again, resets persistence |
| Flash drive / phone / another webcam plugged | Unchanged | Unchanged | Not accepted as PureThermal |
| Wi-Fi deliberately switched off for flight | OFF until user/boot/timer turns it on | Process keeps running; cannot be reached over Wi-Fi | Continues saving locally |
| Wi-Fi enabled / network switched after flight | Existing network policy | Same listener on 0.0.0.0; iOS rediscovers/retries | Continues |

Do not bind the API lifecycle to the USB camera. Stopping it is not intrinsically harmful to Linux, but interrupts iOS downloads and blocks access to already stored captures. Stop only camera work when the camera is absent; keep API/downloads independent. No Wi-Fi toggle, firmware, EEPROM, boot partition, GPIO, or SD recovery changes are included here.

## One-time provisioning, not a per-flight Mac task

Existing verified live installations: deploy the reviewed runtime **and** updated installer, then on Pi:

```sh
cd ~/tilik-edge
# Preserve the active runtime config if this is the path shown by systemctl cat.
sudo python3 deploy/install-autostart.py --capture --config /etc/tilik-runtime.json
sudo systemctl restart tilik-api tilik-capture
systemctl is-enabled tilik-api tilik-capture tilik-hotspot
systemctl --no-pager status tilik-api tilik-capture
curl http://127.0.0.1:8080/api/status
```

If active units use another config path, pass THAT path. Installer preserves store, device identity, rules, token environment file, source, and explicit by-id camera pins, and backs up overwritten units/runtime config. A generic live `/dev/videoN` is migrated to `device: "auto"`; this avoids renumbering after replug. Explicit by-id pins still select the same camera and do not silently choose another.

For a **fresh** Pi, after independently verifying TLinear 0.01 K, AGC off, telemetry off on the camera:

```sh
sudo sh deploy/setup-pi.sh --device auto --confirm-centikelvin
```

The explicit confirmation is a prerequisite. USB ID/Y16 alone does not prove radiometric calibration. Setup without live arguments remains explicitly SIMULATION; it must not be mistaken for deployment of the real camera. No live-to-simulation fallback is added.

Hotspot boot behavior is managed by the existing `deploy/install-boot-hotspot.py`. Do not rerun it just to update camera recovery; it changes network configuration and is outside this patch. Verify the current `tilik-hotspot` unit instead. Once provisioning is complete, normal flight usage needs power + camera, not a Mac running start.sh. Do not run start.sh concurrently with services.

## Implementation and compatibility

- `hotspot/live.py` reads Linux video sysfs and accepts USB VID:PID **1e4e:0100**, streaming index **0**, as verified on this project's PureThermal. An unrelated USB device cannot trigger camera start. Identity alone does not verify radiometry.
- `device: "auto"` discovers the capture node even if video0 becomes video4. With multiple accepted cameras it reports an explicit configuration error instead of selecting arbitrarily. Other PureThermal VID/PID variants require separate validation before adding them.
- Retry is bounded to at most one open attempt every three seconds. Waiting status updates are limited to once a second, or on state changes. No frame rendering/acquisition occurs while the camera is absent.
- On disconnect/stall, the previous capture subprocess/threads are closed before a new stream is opened. There is one camera owner, protected by the existing capture lock. A stream read can take up to its existing five-second timeout to report failure, followed by bounded cleanup/retry.
- Warmup runs after each new stream; no captures during warmup. Rule persistence is reset on gaps, and frame sequence stays increasing across reconnects in the same worker. Timing/session metadata remains in place.
- `/api/status` keeps the existing identity/schema and adds `camera.ready`. `camera.connected` is stream readiness/health, not merely USB enumeration. WAITING_CAMERA/CAMERA_ERROR must not be advertised as a working stream.
- Manual capture and live preview return 503 until the worker is ready, rather than accepting requests on a waiting worker or displaying a stale live image. Stored capture download endpoints stay available.
- Existing token and loopback control protections remain intact; no authentication bypass is introduced. On a shared Wi-Fi without token, read access is still exposed by the existing configuration; that is a deployment policy requiring a controlled network or token/TLS provisioning, not changed silently here.
- The iOS sync/discovery sources inspected verify API/device identity; they do not require a live camera to read the stored catalog. No iOS source changes are needed for this server lifecycle.
- Installer units are independent of the camera and network-online state; reboot enables API/capture automatically. Shutdown gets a 20-second bound and the whole camera process group is cleaned up by systemd.

## Capture durability and rule audit

Existing atomic capture publication writes raw BIN, PNG and JSON into a hidden pending folder and fsyncs the files before rename. This patch also fsyncs that pending directory before publishing; the parent directory is fsynced afterward. Incomplete bundles stay hidden from the catalog. Do not interpret this as protection against SD/controller hardware faults or permission to unplug power while writing.

The previously reviewed per-cluster persistence fix from commit `024ec8a` was missing from the current main lineage. It is reapplied locally, with its regression tests, to avoid accumulating duration across unrelated clusters. Existing threshold, cluster limits, cooldown, hysteresis and radiometry requirements are retained. Site-specific detection accuracy/fire confirmation is not established by these software tests.

## Acceptance checks on the actual Pi

1. Boot with camera absent: hotspot and `/api/status` reachable; state WAITING_CAMERA; old captures download.
2. Plug only a flash drive/phone: capture stays waiting. Plug the verified PureThermal: WARMUP then IDLE/DEBOUNCING/COOLDOWN depending scene.
3. Create a controlled manual capture; verify original PNG/JSON/BIN and download from iOS. Repeat sync: completed files skipped.
4. Unplug camera while viewing/reading an existing stored capture: API remains available, camera.ready becomes false, new manual capture returns 503, existing evidence remains intact.
5. Replug, including after video-node renumbering: automatic reconnect + warmup, then another capture. Confirm no second camera process and no synthetic fallback.
6. Turn Wi-Fi OFF using the agreed procedure: local capture continues. Restore Wi-Fi through an independent recovery method, rediscover in iOS, sync. Do not change Wi-Fi while relying on that same SSH connection for recovery.
7. Reboot only when landed and writes have stopped/using proper shutdown: hotspot/API/capture start again without start.sh.

Useful Pi commands:

```sh
systemctl is-enabled tilik-hotspot tilik-api tilik-capture
sudo journalctl -u tilik-api -u tilik-capture -b -n 80 --no-pager
v4l2-ctl --list-devices
find ~/tilik-edge/data/captures/live -maxdepth 2 -name metadata.json
```

## Verification limits

Automated tests use fake USB/sysfs and camera streams plus the real HTTP/catalog/storage implementation. They verify missing/attach/replug/warmup, no arbitrary-webcam fallback, bounded retry, capture persistence reset across a USB gap, readiness/503 behavior, unchanged downloadable evidence, and installer invariants. These are not a substitute for the above physical boot/unplug/replug/iOS acceptance checks. Remote deployment and physical tests must be recorded separately after the Pi is reachable.

Technical reference: [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html), restart and service lifecycle behavior. The API intentionally has no USB BindsTo dependency.

### Recorded verification — 31 October 2026

- Mac: 62 automated tests pass. Pi staged candidate (Python 3.13): the same 62 tests pass. No active runtime was replaced by these staging tests.
- Actual Pi audit: existing API active, capture repeatedly restarting because PureThermal was absent, hotspot unit enabled/active, runtime live/by-id, 24 GB free, `get_throttled=0x0` at audit time.
- Baseline recorded before installation: 36 evidence artifact files, device identity, rules checksum, runtime config and hotspot service checksum.
- Candidate and a rollback-capable installer staged at `~/tilik-edge/.hotplug-update/`. Installation requires one sudo command from the user because passwordless sudo is unavailable. Deployment is now complete following the user-run sudo installer. API/capture are active with NRestarts=0; all three boot services are enabled. Runtime/config/rules/device identity/hotspot unit and all 36 original artifact files match the recorded baseline. The 15 active edge/hotspot runtime files match the Mac by SHA256. A stored PNG/JSON/BIN bundle downloaded successfully over Wi-Fi with matching manifest hashes. Physical unplug/replug acceptance is now passed (details below). An actual reboot remains untested; boot enablement/unit configuration is verified.

### Physical unplug acceptance — passed

With PureThermal physically disconnected on 31 October 2026, API PID2632 and capture PID2633 remained active with NRestarts=0. State changed to WAITING_CAMERA; camera.connected/ready=false; source remained live. All 11 catalog captures remained listed; one complete PNG/JSON/BIN downloaded and matched manifest SHA256. POST manual capture returned503, without generating fake frames. Replug recovery subsequently passed (below).

### Physical replug acceptance — passed

After replug, the journal recorded PureThermal attach and warmup, then live IDLE/ready. API PID2632 and capture PID2633 remained unchanged with NRestarts=0. A manual capture saved as `20261031_130356_598_event_001_cd1428a3` (source live, no detected hotspot at ambient temperature). Its PNG24284 bytes, JSON1945 bytes and BIN38400 bytes downloaded and matched the new manifest SHA256. Catalog increased from11 to12 captures; all36 original evidence files remained byte-for-byte unchanged. The three services remain enabled for boot. No reboot, Wi-Fi profile or production threshold change was performed in this acceptance test.

### Network manual capture update (local candidate)

Manual capture is now permitted from any reachable Pi address by default, without an SSH tunnel. `allow_remote_capture: false` can disable network manual capture. Token authentication, camera readiness and same-origin checks remain enforced. Rules and clock/session control are not opened by this permission. Dashboard uses the additive `controls.capture_allowed` status field; `controls.allowed` retains its old privileged-control meaning. All68 tests pass locally, including LAN capture, origin rejection, token requirement, local-only opt-out, and camera-unavailable503. Installed on Pi31 October2026 with backup `.lan-capture-backup-1793429887147040682`. Actual LAN POST from Mac to192.168.0.105:8080 returned202 then saved; PNG/JSON/BIN hashes match. Capture ID `b894c454e71e3d71f8832de01e223b0317f0a22bb3cb4f19036441ae0c57868a`. API alone restarted; camera worker untouched. Hotspot address uses the same wildcard listener; hotspot network switching was not performed in this test.
