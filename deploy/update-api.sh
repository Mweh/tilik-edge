#!/bin/sh
# Run on the Mac after stopping start.sh on the Pi. Update code, never config/data.
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TARGET=${1:-pi@raspberrypi.local}
UPDATE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/tilik-runtime.XXXXXX")
trap 'rm -rf "$UPDATE_DIR"' EXIT HUP INT TERM
# Ship the dependency closure, including commands.py and dashboard.html.
tar --exclude='__pycache__' --exclude='*.pyc' -czf "$UPDATE_DIR/runtime.tar.gz" -C "$PROJECT_DIR" edge hotspot
scp "$UPDATE_DIR/runtime.tar.gz" "$TARGET:tilik-edge/.tilik-runtime-update.tar.gz"
ssh "$TARGET" sh -s <<'REMOTE'
set -eu
cd "$HOME/tilik-edge"
STAGE=$(mktemp -d "$PWD/.runtime-update.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT HUP INT TERM
tar -xzf .tilik-runtime-update.tar.gz -C "$STAGE"
# Import checks do not open the camera, create an API server, or load Pi settings.
(cd "$STAGE" && PYTHONDONTWRITEBYTECODE=1 python3 -c 'import edge.api, edge.commands, edge.discovery, edge.__main__, hotspot.__main__')
BACKUP=$(mktemp -d "$PWD/.runtime-backup.XXXXXX")
for package in edge hotspot; do
    if [ -d "$package" ]; then cp -R "$package" "$BACKUP/"; fi
    mkdir -p "$package"
    cp -R "$STAGE/$package/." "$package/"
done
PYTHONDONTWRITEBYTECODE=1 python3 -c 'import edge.api, edge.commands, edge.discovery, edge.__main__, hotspot.__main__'
rm -f .tilik-runtime-update.tar.gz
printf 'Runtime updated and imports verified. Backup: %s\n' "$BACKUP"
REMOTE
echo 'On the Pi: cd ~/tilik-edge and run sh start.sh again.'
echo 'Config, rules, catalog, and captures were not replaced.'
