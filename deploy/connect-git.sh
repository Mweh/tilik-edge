#!/bin/sh
# Run once ON THE PI after stopping start.sh. Preserve local settings and all evidence.
set -eu
[ "$(uname -s)" = Linux ] || { echo 'Run this script on the Raspberry Pi, not the Mac.'; exit 1; }
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"
REPOSITORY=${1:-https://github.com/Mweh/tilik-edge.git}
if [ -e .git ]; then
    echo 'This folder already has Git. No files changed. Inspect git status before pulling.'
    git remote -v
    exit 0
fi
command -v git >/dev/null || { echo 'Install Git first: sudo apt-get install git'; exit 1; }
BACKUP=$(mktemp -d "$PWD/.git-bootstrap-backup.XXXXXX")
for item in edge hotspot config deploy start.sh README.md requirements.txt; do
    if [ -e "$item" ]; then cp -R "$item" "$BACKUP/"; fi
done
python3 - <<'CONFIG'
import json
import shutil
from pathlib import Path
source = Path('config/edge.json')
local = Path('config/pi.local.json')
if source.is_file() and not local.exists():
    config = json.loads(source.read_text())
    rules = (source.parent / config['rules']).resolve()
    if rules.is_file():
        shutil.copyfile(rules, source.parent / 'rules.local.json')
        config['rules'] = 'rules.local.json'
    config['data_dir'] = str((source.parent / config['data_dir']).resolve())
    local.write_text(json.dumps(config, indent=2) + '\n')
    local.chmod(0o600)
CONFIG
git init -b main
git remote add origin "$REPOSITORY"
git fetch origin main
# Populate the fresh index, then install only repository-tracked files. Ignored
# pi.local.json, rules.local.json, data/ and existing capture folders stay untouched.
git reset --mixed origin/main
git restore --source=origin/main --worktree -- .
git branch --set-upstream-to=origin/main main
PYTHONDONTWRITEBYTECODE=1 python3 -c 'import edge.api, edge.commands, edge.discovery, hotspot.__main__'
printf 'Git connected. Local configuration: config/pi.local.json\nCode/config backup: %s\n' "$BACKUP"
echo 'Next: sh start.sh. Future updates: stop capture, git pull --ff-only, sh start.sh.'
