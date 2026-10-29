#!/usr/bin/env bash
# Pull journal snaps from the live algo-flat server.
# Usage:
#   LIVE_SSH_HOST=root@200.97.162.221 LIVE_SSH_PASS='...' ./scripts/pull_live_data.sh 2026-08-07
#   ./scripts/pull_live_data.sh 2026-08-05 2026-08-07   # date range
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${LIVE_SSH_HOST:-root@200.97.162.221}"
REMOTE_DIR="${LIVE_REMOTE_DIR:-/root/algo-flat}"
SNAP_ROOT="$ROOT/reports/snaps"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 YYYY-MM-DD [END-DATE]" >&2
  exit 1
fi

START="$1"
END="${2:-$1}"

if ! command -v sshpass >/dev/null 2>&1; then
  echo "Install sshpass first (brew install hudochenkov/sshpass/sshpass)" >&2
  exit 1
fi

if [[ -z "${LIVE_SSH_PASS:-}" ]]; then
  echo "Set LIVE_SSH_PASS for password auth, or configure SSH keys." >&2
  exit 1
fi

ssh_rsync() {
  SSHPASS="$LIVE_SSH_PASS" sshpass -e rsync -az --progress \
    -e "ssh -o StrictHostKeyChecking=no" \
    "$@"
}

current="$START"
while [[ "$current" != "$(date -j -v+1d -f '%Y-%m-%d' "$END" '+%Y-%m-%d' 2>/dev/null || date -I -d "$END + 1 day")" ]]; do
  for ul in NIFTY; do
    remote="$REMOTE_DIR/reports/snaps/$current/$ul/"
    local="$SNAP_ROOT/$current/$ul/"
    if SSHPASS="$LIVE_SSH_PASS" sshpass -e ssh -o StrictHostKeyChecking=no "$HOST" \
      "test -d $remote" 2>/dev/null; then
      mkdir -p "$local"
      echo "==> Pulling snaps $current/$ul"
      ssh_rsync "$HOST:$remote" "$local"
    else
      echo "==> No remote snap for $current/$ul (skip)"
    fi
  done
  current="$(date -j -v+1d -f '%Y-%m-%d' "$current" '+%Y-%m-%d' 2>/dev/null || date -I -d "$current + 1 day")"
done

echo "Done. Run journal replay with:"
echo "  LIVE_DATABASE_URL='postgresql://algoflat:algoflat@HOST:5432/algoflat' \\"
echo "  CONFIG_DIR=./config PYTHONPATH=trading-engine/src:./scripts \\"
echo "  trading-engine/.venv/bin/python scripts/journal_replay.py --date $START --mode full"
