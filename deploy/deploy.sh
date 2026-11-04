#!/usr/bin/env bash
# Mise à jour sur le Pi : git pull + réinstall + restart
set -euo pipefail
cd /home/pi/apps/bankwatch
git pull --ff-only
.venv/bin/pip install -q -e .
sudo systemctl restart bankwatch.service
echo "OK — $(git log -1 --format='%h %s')"
