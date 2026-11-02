# Timed thermal capture QA

Branch `experiment/timed-thermal-capture`. Opt-in mode, normal capture remains the default.

Run on the Pi from the repository after stopping the existing capture service to release the camera:

```sh
sudo systemctl stop tilik-capture
python3 -m edge capture --timed-capture --config config/edge.json
```

Keep the API service running for iOS sync. Captures use the configured data directory and source subfolder, with PNG, metadata JSON and radiometric BIN bundles.

Schedule from process startup (monotonic clock):

- First five minutes: slots at 0, 20, 40 ... 280 seconds.
- From minute five: groups at 300/320/340, 420/440/460 and 540/560/580 seconds.
- At 600 seconds: capture loop exits and closes the camera. API and Wi-Fi remain available.

Up to 24 captures. Startup warmup may delay the first slot. Camera outages skip missed slots instead of catching up in a burst. Rule-triggered automatic saves are disabled in this mode. Manual requests remain available. The timer continues during camera outages.

This schedule reduces file writes but the camera still streams until capture stops. It does not power off the Pi or USB hardware and does not guarantee a lower temperature. Do not add this command to a service with Restart=always, which would restart the schedule.

Restore normal capture:

```sh
sudo systemctl start tilik-capture
```
