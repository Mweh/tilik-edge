# Boot hotspot and USB toggle

Optional feature for Raspberry Pi OS with NetworkManager, a built-in AP-capable
Wi-Fi interface (default `wlan0`), Python 3 and systemd. Install from the Pi's
local terminal or VNC; switching networks disconnects SSH, VNC and iOS sync.
A single Wi-Fi interface hosts the payload hotspot instead of joining SwiftFun
or another router. Internet requires Ethernet or another configured interface.

```sh
cd ~/tilik-edge
sudo python3 deploy/wifi_usb.py --install
sudo reboot
```

The installer prompts for a password locally and enables a boot service. It does
not immediately change the active network. Keep your Wi-Fi country configured
in Raspberry Pi OS. Join `Tilik-Drone-Payload` from iPhone/Mac using that password.
The hotspot address is `10.42.0.1`; API remains on port 8080 if separately started.
This service configures networking only; capture/API services remain independent.
Ensure the API binds `0.0.0.0`, not localhost.

To reuse an existing secured hotspot with the same SSID and password:

```sh
nmcli -f NAME,UUID connection show
sudo python3 deploy/wifi_usb.py --install --existing-connection '<hotspot UUID>'
```

This validates the AP name/security and retains its network/password settings.

## Behavior

- Each boot: hotspot ON, including after it was OFF before shutdown.
- Devices already enumerated when the service starts: baseline, no toggle.
- New physical USB device, stable for two seconds: toggle Wi-Fi OFF/ON.
- Unplug: no toggle. Replug: next toggle. Wait at least three seconds between actions.
- USB interfaces, root controllers and hubs are excluded. Hub children are counted
  as individual devices; plugging a hub with several children can toggle several times.
- PureThermal devices named PureThermal and VID:PID `1e4e:0100` are excluded.
  Custom firmware with another name/ID must be added to `ignore_usb_ids` in
  `/etc/tilik-payload.json` (lowercase `vvvv:pppp`). Confirm using `lsusb`.
- A charging-only cable/device that does not enumerate cannot act as a switch.
- A device's USB reconnect/reset can also count as insertion; a software watcher
  cannot distinguish every electrical reset from a physical replug.
- OFF disables all Wi-Fi radios via NetworkManager, including USB Wi-Fi adapters.
  Use an ordinary USB peripheral as the switch, not the Wi-Fi adapter itself.
- Service restart preserves its last successful ON/OFF state until the next reboot.

No flash drive is mounted or read. No captures, API credentials, or camera
settings are modified. Saved router profiles remain available. Hotspot password
lives only in a root-only NetworkManager keyfile, not Git.

## Check, recover, uninstall

```sh
sudo systemctl status tilik-payload
sudo journalctl -u tilik-payload -b -n 60
```

If OFF, replug a nonthermal device and wait three seconds. Recovery from the
local console (stopping the service first prevents further USB toggles):

```sh
sudo systemctl stop tilik-payload
sudo nmcli radio wifi on
sudo nmcli connection up Tilik-Drone-Payload
```

Disable the feature and return to a saved router profile:

```sh
sudo systemctl disable --now tilik-payload
sudo nmcli connection modify Tilik-Drone-Payload connection.autoconnect no
sudo nmcli radio wifi on
nmcli connection show
# sudo nmcli connection up '<your existing router profile>'
```

The installer is idempotent and preserves existing credentials. After Git updates,
rerun it to refresh the installed script; restart the service locally to apply.
Physical Pi validation is required: AP startup, DHCP, iPhone discovery, PureThermal
replug with no toggle, peripheral replug OFF/ON, and reboot restoring ON.
