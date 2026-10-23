#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"
DEFAULT_CONFIG=config/edge.json
if [ -f config/pi.local.json ]; then DEFAULT_CONFIG=config/pi.local.json; fi
exec python3 -m edge run --config "${1:-$DEFAULT_CONFIG}"
