#!/usr/bin/env bash
# Redeploy script -- safe to re-run any time. Pulls main, installs deps,
# migrates, collects static files, restarts the three services. Does NOT
# touch nginx, systemd unit files, Postgres/Redis/Weaviate, or secrets --
# those are one-time setup steps in docs/deployment.md, deliberately kept
# out of this script since they're the parts worth reviewing by hand,
# especially on a server that also hosts other sites.
set -euo pipefail

# Override with e.g. `APP_DIR=/srv/finance-backend ./deploy/deploy.sh` if
# the checkout lives somewhere other than the default below.
APP_DIR="${APP_DIR:-/home/deployer/finance-backend}"
VENV="$APP_DIR/.venv"
BRANCH="main"

cd "$APP_DIR"

echo "==> Fetching $BRANCH"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

echo "==> Installing dependencies"
"$VENV/bin/pip" install --upgrade pip
# requirements.txt is the slim base -- filing ingestion (docling/torch,
# ~1.2GB) and the media app (django-storages) are optional extras split
# into their own files so a fresh clone that wants neither doesn't pay
# for them. This server runs both (filing ingestion, and media -- gated
# behind ENABLE_MEDIA in .env), so install all three here. Drop a line
# below if this deployment stops needing one of them.
"$VENV/bin/pip" install -r requirements.txt -r requirements-filings.txt -r requirements-media.txt

echo "==> Running migrations"
"$VENV/bin/python" manage.py migrate --noinput

echo "==> Collecting static files"
"$VENV/bin/python" manage.py collectstatic --noinput

echo "==> Restarting services"
sudo systemctl restart finance-api
sudo systemctl restart finance-celery-worker
sudo systemctl restart finance-celery-beat

echo "==> Done. Checking service status:"
systemctl is-active finance-api finance-celery-worker finance-celery-beat
