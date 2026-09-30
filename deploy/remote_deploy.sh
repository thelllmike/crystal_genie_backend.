#!/usr/bin/env bash
# Runs ON THE VPS (as root), called by .github/workflows/deploy.yml after the
# new code has been copied to $INCOMING.
#
#   1. back up the code that's running now
#   2. copy the new code in (the .env, venv, model weights and uploaded
#      training photos are never touched)
#   3. reinstall packages only if requirements.txt changed
#   4. restart the API and wait for /health
#   5. if it doesn't come up healthy, put the old code back and restart
set -euo pipefail

APP=/opt/crystalgenie/app
INCOMING=/opt/crystalgenie/incoming
BACKUP=/opt/crystalgenie/previous
USER_GROUP=crystalgenie:crystalgenie

# Server-only files that deploys must never overwrite or delete.
# Leading "/" = only at the top of the app folder.
KEEP=(--exclude '/.venv' --exclude '/.env*' --exclude '/best.pt' --exclude '/models'
      --exclude '/training_data' --exclude '/trainer/work' --exclude '/detect'
      --exclude '__pycache__')

log() { echo "==> $*"; }

healthy() {
  for _ in $(seq 1 60); do
    curl -sf http://127.0.0.1:8000/health >/dev/null && return 0
    sleep 2
  done
  return 1
}

changed() { ! cmp -s "$BACKUP/$1" "$APP/$1"; }

log "Backing up current code to $BACKUP"
mkdir -p "$BACKUP"
rsync -a --delete "${KEEP[@]}" "$APP/" "$BACKUP/"

log "Installing new code"
rsync -a --delete "${KEEP[@]}" "$INCOMING/" "$APP/"

if changed requirements.txt; then
  log "requirements.txt changed — installing packages (CPU-only torch)"
  "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt" \
    --extra-index-url https://download.pytorch.org/whl/cpu
fi

chown -R "$USER_GROUP" "$APP"

if changed trainer/crystalgenie-trainer.service; then
  log "Trainer service file changed — reloading systemd"
  cp "$APP/trainer/crystalgenie-trainer.service" /etc/systemd/system/
  systemctl daemon-reload
fi

log "Restarting API"
systemctl restart crystalgenie
if healthy; then
  log "API is healthy: $(curl -s http://127.0.0.1:8000/health)"
else
  log "API did NOT come up — rolling back"
  journalctl -u crystalgenie -n 40 --no-pager || true
  rsync -a --delete "${KEEP[@]}" "$BACKUP/" "$APP/"
  chown -R "$USER_GROUP" "$APP"
  systemctl restart crystalgenie
  healthy && log "Rolled back; the previous version is running again." \
          || log "Rollback also failed to become healthy — check the server!"
  exit 1
fi

# Restart the trainer only when its code changed: a restart ends any
# training run in progress (it's marked failed and can simply be re-run).
if systemctl is-active --quiet crystalgenie-trainer; then
  if changed trainer/train_worker.py; then
    log "Trainer code changed — restarting trainer"
    systemctl restart crystalgenie-trainer
  fi
fi

log "Deployed $(cat "$APP/deploy/.last" 2>/dev/null || echo 'new version')"
