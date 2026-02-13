#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_DIR="${REPO_ROOT}/custom_components/saj_esolar_cloud/"
REMOTE_HOST="ubuntu"
REMOTE_DIR="/var/homeassistant.stage/custom_components/saj_esolar_cloud/"
REMOTE_COMPOSE_DIR="/home/santiago/services/ubuntu2/home-assistant-stage"

if ! command -v rsync >/dev/null 2>&1; then
  echo "Error: rsync is required but not installed." >&2
  exit 1
fi

if [[ ! -d "${SOURCE_DIR}" ]]; then
  echo "Error: source directory not found: ${SOURCE_DIR}" >&2
  exit 1
fi

echo "Deploying ${SOURCE_DIR} -> ${REMOTE_HOST}:${REMOTE_DIR}"

rsync -rlz --delete --omit-dir-times --no-perms --no-owner --no-group \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude ".DS_Store" \
  -e "ssh" \
  "${SOURCE_DIR}" \
  "${REMOTE_HOST}:${REMOTE_DIR}"

echo "Deploy complete."
