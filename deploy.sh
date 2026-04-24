#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/playtomic-checker}"
BRANCH="${BRANCH:-main}"

if [ ! -d "$APP_DIR/.git" ]; then
  echo "APP_DIR does not look like a git repo: $APP_DIR"
  exit 1
fi

cd "$APP_DIR"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

docker compose build --pull

# Ensure stale/conflicting containers don't block recreation
# (can happen after interrupted deploys with old generated names)
docker compose down --remove-orphans || true
if docker ps -a --format '{{.Names}}' | grep -qx 'playtomic-checker'; then
  docker rm -f playtomic-checker || true
fi

docker compose up -d --force-recreate

echo "Deployed $(git rev-parse --short HEAD)"
