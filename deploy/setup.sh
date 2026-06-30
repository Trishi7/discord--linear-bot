#!/usr/bin/env bash
#
# Idempotent installer for the Discord -> Linear Triage Bot.
# Run this ON THE SERVER, from inside the copied source directory:
#
#     sudo bash deploy/setup.sh
#
# Re-run it any time you push new code to redeploy + restart.
#
set -euo pipefail

APP_DIR=/opt/discord-triage-bot
SERVICE=discord-triage-bot
RUN_USER=botuser

# Resolve the source dir = the repo folder this script lives in (deploy/..).
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Source:  $SRC_DIR"
echo "==> Target:  $APP_DIR"
echo "==> Service: $SERVICE.service (user: $RUN_USER)"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run with sudo (sudo bash deploy/setup.sh)" >&2
  exit 1
fi

# .env must exist somewhere — either freshly copied alongside the source
# (first install) or already living in the target dir (a redeploy).
if [[ ! -f "$SRC_DIR/.env" && ! -f "$APP_DIR/.env" ]]; then
  echo "ERROR: no .env found in $SRC_DIR or $APP_DIR." >&2
  echo "       Copy your .env next to the source before first install." >&2
  exit 1
fi

echo "==> Installing system packages (python3, venv, pip, rsync)..."
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip rsync
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip rsync
else
  echo "WARN: unknown package manager; ensure python3, venv, pip, rsync are installed." >&2
fi

echo "==> Ensuring service user '$RUN_USER' exists..."
if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$RUN_USER"
fi

echo "==> Syncing code to $APP_DIR (preserving live .env and bot_state.db)..."
mkdir -p "$APP_DIR"
# Never clobber the running DB. Keep the existing .env on redeploys; only let a
# fresh .env through when the target doesn't have one yet (first install).
ENV_EXCLUDE=()
if [[ -f "$APP_DIR/.env" ]]; then
  ENV_EXCLUDE=(--exclude '.env')
fi
rsync -a \
  --exclude venv \
  --exclude __pycache__ \
  --exclude '.git' \
  --exclude 'bot_state.db' \
  "${ENV_EXCLUDE[@]}" \
  "$SRC_DIR"/ "$APP_DIR"/

echo "==> Locking down .env permissions..."
chmod 600 "$APP_DIR/.env"

echo "==> Creating / updating virtualenv and dependencies..."
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> Setting ownership to $RUN_USER..."
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

echo "==> Installing systemd unit..."
install -m 644 "$APP_DIR/deploy/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

echo
echo "==> Done. Recent logs:"
sleep 2
systemctl --no-pager --lines=20 status "$SERVICE" || true
echo
echo "Follow live logs with:  journalctl -u $SERVICE -f"
