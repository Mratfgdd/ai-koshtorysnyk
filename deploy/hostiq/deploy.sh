#!/usr/bin/env bash
# Deploy to HostIQ over SSH.
#
# Configure once in deploy/hostiq/deploy.env (gitignored), then run:
#   bash deploy/hostiq/deploy.sh
#
# It never stores a password: use an SSH key, or let ssh prompt. Passing a
# password on the command line would put it in your shell history and in the
# process list of a shared machine.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

# shellcheck disable=SC1091
[ -f "$HERE/deploy.env" ] && source "$HERE/deploy.env"

: "${SSH_HOST:?Set SSH_HOST, e.g. srv42.hostiq.ua}"
: "${SSH_USER:?Set SSH_USER — the cPanel username, not the email}"
SSH_PORT="${SSH_PORT:-22}"
APP_DIR="${APP_DIR:-koshtorysnyk}"          # ~/koshtorysnyk
WEB_DIR="${WEB_DIR:-public_html}"           # ~/public_html
API_URL="${API_URL:?Set API_URL, e.g. https://api.example.com}"

SSH="ssh -p $SSH_PORT ${SSH_USER}@${SSH_HOST}"
say() { printf '\n\033[1;32m==>\033[0m %s\n' "$1"; }

# --- 0. reachable? -----------------------------------------------------------
say "Checking SSH access to ${SSH_USER}@${SSH_HOST}:${SSH_PORT}"
$SSH -o ConnectTimeout=15 -o BatchMode=no 'echo "connected as $(whoami) on $(hostname)"'

# --- 1. build the frontend against the real API ------------------------------
say "Building frontend with VITE_API_BASE_URL=${API_URL}"
cd "$ROOT/frontend"
npm ci --no-audit --no-fund
VITE_API_BASE_URL="$API_URL" npm run build

grep -rq "$(printf '%s' "$API_URL" | sed 's#https\?://##')" dist/assets/*.js \
  || { echo "API base did not reach the bundle — aborting"; exit 1; }

# --- 2. upload the frontend ---------------------------------------------------
say "Uploading frontend to ~/${WEB_DIR}"
$SSH "mkdir -p ~/${WEB_DIR}"
scp -P "$SSH_PORT" -r dist/. "${SSH_USER}@${SSH_HOST}:~/${WEB_DIR}/"
scp -P "$SSH_PORT" "$HERE/public_html.htaccess" \
    "${SSH_USER}@${SSH_HOST}:~/${WEB_DIR}/.htaccess"

# --- 3. upload the backend ----------------------------------------------------
say "Uploading backend to ~/${APP_DIR}"
cd "$ROOT/backend"
$SSH "mkdir -p ~/${APP_DIR}"
# Source only: no local database, venv, cache or __pycache__.
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='data' \
    --exclude='.pytest_cache' --exclude='venv' --exclude='*.db*' \
    -czf - . | $SSH "tar -xzf - -C ~/${APP_DIR}"

scp -P "$SSH_PORT" "$HERE/backend.htaccess" \
    "${SSH_USER}@${SSH_HOST}:~/${APP_DIR}/.htaccess"
scp -P "$SSH_PORT" "$HERE/user.ini" \
    "${SSH_USER}@${SSH_HOST}:~/${APP_DIR}/.user.ini"

# --- 4. dependencies ----------------------------------------------------------
say "Installing dependencies in the cPanel virtualenv"
$SSH bash -s <<EOF
set -e
cd ~/${APP_DIR}
VENV=\$(ls -d ~/virtualenv/${APP_DIR}/*/ 2>/dev/null | head -1)
if [ -z "\$VENV" ]; then
  echo "No cPanel virtualenv found — create the app in cPanel first" >&2
  exit 1
fi
source "\${VENV}bin/activate"
pip install --upgrade pip -q
pip install -r requirements.txt
pip install a2wsgi          # ASGI -> WSGI bridge for Passenger
mkdir -p data
python -c "from app.main import bootstrap; bootstrap(); print('database ready')"
EOF

# --- 5. restart and verify ----------------------------------------------------
say "Restarting the application"
$SSH "mkdir -p ~/${APP_DIR}/tmp && touch ~/${APP_DIR}/tmp/restart.txt"
sleep 6

say "Verifying"
code=$(curl -s -o /tmp/health.json -w '%{http_code}' "${API_URL}/api/health" || true)
echo "GET ${API_URL}/api/health -> HTTP ${code}"
cat /tmp/health.json 2>/dev/null || true
echo
[ "$code" = "200" ] || { echo "Backend is not answering — see cPanel error log"; exit 1; }

say "Done. Frontend: check the site root in a browser."
