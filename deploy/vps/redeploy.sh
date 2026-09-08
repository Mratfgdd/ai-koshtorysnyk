#!/usr/bin/env bash
# Re-deploy to the KVM VPS over SSH.
#
#   bash deploy/vps/redeploy.sh
#
# Ships the current working tree, rebuilds the frontend on the server and
# restarts uvicorn. The server keeps its own .env and its data directory, so
# neither the API key nor the database is touched.
#
# It never stores a password: use an SSH key, or let ssh prompt. Passing a
# password on the command line would put it in your shell history and in the
# process list of a shared machine.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

SSH_HOST="${SSH_HOST:-45.94.157.110}"
SSH_USER="${SSH_USER:-root}"
SSH_PORT="${SSH_PORT:-22}"
APP_DIR="${APP_DIR:-/opt/estimator/src}"
WEB_DIR="${WEB_DIR:-/var/www/html}"

# Deliberately empty: src/api.ts builds every request as `${API_BASE}/api${path}`,
# so this must be the ORIGIN, never the origin plus /api — that yields
# /api/api/dashboard and a 404 on every call. Empty means the SPA uses relative
# paths, which is right here because Nginx serves the app and proxies /api on
# the same origin, and which keeps working unchanged behind a domain or HTTPS.
# Set it only when the frontend is hosted apart from the API (e.g. Vercel),
# and then to the bare origin: API_URL=https://api.example.com
API_URL="${API_URL-}"
SITE_URL="${SITE_URL:-http://$SSH_HOST}"

SSH=(ssh -p "$SSH_PORT" "$SSH_USER@$SSH_HOST")

echo "==> packing the working tree"
TARBALL="$(mktemp -t koshtorysnyk-XXXXXX).tar.gz"
trap 'rm -f "$TARBALL"' EXIT
tar -czf "$TARBALL" -C "$ROOT" \
    --exclude='./frontend/node_modules' \
    --exclude='./frontend/dist' \
    --exclude='./.git' \
    --exclude='./data' \
    --exclude='./backend/data' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    --exclude='./.env' \
    .

echo "==> uploading"
scp -P "$SSH_PORT" "$TARBALL" "$SSH_USER@$SSH_HOST:/tmp/koshtorysnyk.tar.gz"

echo "==> unpacking, building and restarting"
"${SSH[@]}" APP_DIR="$APP_DIR" WEB_DIR="$WEB_DIR" API_URL="$API_URL" bash -s <<'REMOTE'
set -euo pipefail

# The server's .env holds the API key and the deployment paths; the tarball
# deliberately does not carry it, so keep the copy that is already there.
cp "$APP_DIR/.env" /tmp/estimator.env.keep

rm -rf "$APP_DIR.new" && mkdir -p "$APP_DIR.new"
tar -xzf /tmp/koshtorysnyk.tar.gz -C "$APP_DIR.new"
cp /tmp/estimator.env.keep "$APP_DIR.new/.env"

echo "--> python dependencies"
/opt/estimator/venv/bin/pip install -q -r "$APP_DIR.new/backend/requirements.txt"

echo "--> frontend build"
cd "$APP_DIR.new/frontend"
echo "VITE_API_BASE_URL=$API_URL" > .env.production
npm ci --no-audit --no-fund --silent
npm run build

echo "--> swapping in"
rm -rf "$APP_DIR.old"
mv "$APP_DIR" "$APP_DIR.old"
mv "$APP_DIR.new" "$APP_DIR"
chown -R estimator:estimator "$APP_DIR"
chmod 600 "$APP_DIR/.env"

rm -rf "${WEB_DIR:?}"/*
cp -r "$APP_DIR/frontend/dist/." "$WEB_DIR/"
chown -R nginx:nginx "$WEB_DIR"
chmod -R a+rX "$WEB_DIR"

systemctl restart estimator
sleep 4
rm -f /tmp/koshtorysnyk.tar.gz /tmp/estimator.env.keep
REMOTE

echo "==> checking"
# uvicorn needs a few seconds to import the app and open the database, so the
# first probe after a restart legitimately gets a 502 from nginx. Retry, and
# fail loudly if it never comes up — reporting "done" over a dead service is
# worse than no check at all.
health=""
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if health="$(curl -fsS --max-time 15 "$SITE_URL/api/health" 2>/dev/null)"; then
    break
  fi
  echo "    not up yet (attempt $attempt/10)…"
  sleep 3
done
if [ -z "$health" ]; then
  echo "FAILED: $SITE_URL/api/health did not answer after the restart." >&2
  echo "Check: ssh $SSH_USER@$SSH_HOST journalctl -u estimator -n 50" >&2
  exit 1
fi
echo "$health"
# The bundle must not carry an absolute API origin unless one was asked for:
# a stray /api in it is the /api/api/... failure mode.
if [ -z "$API_URL" ] && ssh -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" \
     "grep -q '/api/api' $WEB_DIR/assets/*.js"; then
  echo "WARNING: the built bundle contains /api/api — check VITE_API_BASE_URL" >&2
  exit 1
fi
echo "done: $SITE_URL"
