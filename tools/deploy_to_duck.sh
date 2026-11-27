#!/usr/bin/env bash
# Deploy Armature Studio to the duck over SSH/rsync
set -euo pipefail

HOST="${1:-duck-c4ef.local}"
USER="${2:-radxa}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$HERE")"
STUDIO_DIR="$REPO_ROOT/software/studio"

echo "============================================================"
echo "Deploying Armature Studio to $USER@$HOST"
echo "============================================================"

# 1. Sync local studio to staging on duck
echo "[1/3] Syncing files to /home/$USER/studio-dev ..."
rsync -avz --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '_testlogs' \
  --exclude 'state' \
  --exclude 'logs' \
  --exclude '.git' \
  "$STUDIO_DIR/" "$USER@$HOST:/home/$USER/studio-dev/"

# 2. Run selfcheck on duck
echo "[2/3] Running remote selfcheck..."
ssh "$USER@$HOST" "cd /home/$USER/studio-dev && python3 -I run.py --selfcheck" || true

# 3. Cut over and restart
echo "[3/3] Cutting over to production and restarting microduck-studio..."
ssh -t "$USER@$HOST" "sudo /home/radxa/deploy_armature_studio.sh || (sudo rsync -a /home/$USER/studio-dev/ /opt/microduck/software/current/studio/ && sudo systemctl restart microduck-studio && sudo systemctl status microduck-studio --no-pager)"

echo "============================================================"
echo "Deployment Complete! Open http://$HOST:8000/"
echo "============================================================"
