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
docker compose up -d

echo "Deployed $(git rev-parse --short HEAD)"
