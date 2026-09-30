#!/usr/bin/env bash
# Install or update LAN Watch under /opt/lan-watch and run it as lanwatch.
# Never replaces /etc/lan-watch/config.yaml, /var/lib/lan-watch, or telegram.env.
#
# Usage (from a git checkout of this repo):
#   sudo ./scripts/deploy.sh
set -euo pipefail

APP_USER="${APP_USER:-lanwatch}"
APP_GROUP="${APP_GROUP:-lanwatch}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/lan-watch}"
CONFIG_DIR="${CONFIG_DIR:-/etc/lan-watch}"
DATA_DIR="${DATA_DIR:-/var/lib/lan-watch}"
UNIT_DST="${UNIT_DST:-/etc/systemd/system/lan-watch.service}"
HEALTH_TRIES="${HEALTH_TRIES:-20}"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

if ! getent passwd "$APP_USER" >/dev/null; then
  echo "Missing service account '$APP_USER'. Create it first:" >&2
  echo "  sudo useradd --system --home /home/$APP_USER --shell /usr/sbin/nologin $APP_USER" >&2
  exit 1
fi

if ! command -v python3 >/dev/null; then
  echo "python3 is required" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO_ROOT/lan-watch.service"

if [[ ! -f "$REPO_ROOT/app/main.py" || ! -f "$REPO_ROOT/requirements.txt" || ! -f "$UNIT_SRC" ]]; then
  echo "This script must live in a LAN Watch checkout (missing app/, requirements.txt, or unit file)." >&2
  exit 1
fi

install -d -o "$APP_USER" -g "$APP_GROUP" -m 755 "$INSTALL_ROOT" "$DATA_DIR"
install -d -o root -g "$APP_GROUP" -m 750 "$CONFIG_DIR"

# Preserve live config/secrets before any code copy that might replace /opt files.
if [[ ! -f "$CONFIG_DIR/config.yaml" && -f "$INSTALL_ROOT/config.yaml" ]]; then
  cp -a "$INSTALL_ROOT/config.yaml" "$CONFIG_DIR/config.yaml"
  echo "Kept existing host list at $CONFIG_DIR/config.yaml"
fi

if [[ ! -f "$CONFIG_DIR/telegram.env" && -f "$INSTALL_ROOT/secrets/telegram.env" ]]; then
  cp -a "$INSTALL_ROOT/secrets/telegram.env" "$CONFIG_DIR/telegram.env"
  echo "Kept existing Telegram secrets at $CONFIG_DIR/telegram.env"
fi

copy_tree() {
  local src="$1" dest="$2"
  if command -v rsync >/dev/null; then
    rsync -a --delete \
      --exclude '.git/' \
      --exclude '.venv/' \
      --exclude 'data/' \
      --exclude 'secrets/telegram.env' \
      --exclude '__pycache__/' \
      --exclude '*.pyc' \
      "$src/" "$dest/"
    return
  fi
  mkdir -p "$dest/app" "$dest/scripts" "$dest/secrets"
  cp -a "$src/app/." "$dest/app/"
  cp -a "$src/scripts/." "$dest/scripts/"
  cp -a "$src/requirements.txt" "$src/lan-watch.service" "$dest/"
  cp -a "$src/secrets/telegram.env.example" "$dest/secrets/"
  if [[ -f "$src/README.md" ]]; then
    cp -a "$src/README.md" "$dest/"
  fi
}

if [[ "$REPO_ROOT" != "$INSTALL_ROOT" ]]; then
  copy_tree "$REPO_ROOT" "$INSTALL_ROOT"
fi

if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
  cp -a "$REPO_ROOT/config.yaml" "$CONFIG_DIR/config.yaml"
  echo "Installed $CONFIG_DIR/config.yaml (edit hosts here; later deploys will not replace it)."
fi

ensure_key() {
  local key="$1" line="$2"
  if ! grep -qE "^${key}:" "$CONFIG_DIR/config.yaml"; then
    printf '\n%s\n' "$line" >> "$CONFIG_DIR/config.yaml"
    echo "Added $key to $CONFIG_DIR/config.yaml"
  fi
}

ensure_key "telegram_enabled" "telegram_enabled: true"
ensure_key "telegram_commands" "telegram_commands: true"
ensure_key "alert_on_recovery" "alert_on_recovery: true"
ensure_key "alert_cooldown_seconds" "alert_cooldown_seconds: 900"

if [[ ! -f "$DATA_DIR/lan-watch.db" && -f "$INSTALL_ROOT/data/lan-watch.db" ]]; then
  if systemctl is-active --quiet lan-watch; then
    echo "Stopping lan-watch to move the SQLite database..."
    systemctl stop lan-watch
  fi
  echo "Moving existing SQLite database to $DATA_DIR"
  mv "$INSTALL_ROOT/data/lan-watch.db" "$DATA_DIR/lan-watch.db"
  for suffix in -wal -shm; do
    if [[ -e "$INSTALL_ROOT/data/lan-watch.db${suffix}" ]]; then
      mv "$INSTALL_ROOT/data/lan-watch.db${suffix}" "$DATA_DIR/lan-watch.db${suffix}"
    fi
  done
fi

if [[ ! -f "$CONFIG_DIR/telegram.env" ]]; then
  for candidate in \
    "$REPO_ROOT/secrets/telegram.env" \
    /home/cursor-agent/lan-watch-test/secrets/telegram.env
  do
    if [[ -f "$candidate" ]]; then
      cp -a "$candidate" "$CONFIG_DIR/telegram.env"
      echo "Copied Telegram secrets from $candidate"
      break
    fi
  done
fi

if [[ ! -f "$CONFIG_DIR/telegram.env" ]]; then
  cp -a "$REPO_ROOT/secrets/telegram.env.example" "$CONFIG_DIR/telegram.env"
  echo "Created empty $CONFIG_DIR/telegram.env — paste TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, then rerun this script."
fi

chown -R "$APP_USER:$APP_GROUP" "$INSTALL_ROOT" "$DATA_DIR"
chown "root:$APP_GROUP" "$CONFIG_DIR"
chown "$APP_USER:$APP_GROUP" "$CONFIG_DIR/config.yaml" "$CONFIG_DIR/telegram.env"
chmod 640 "$CONFIG_DIR/config.yaml"
chmod 600 "$CONFIG_DIR/telegram.env"
chmod 750 "$CONFIG_DIR"

if [[ ! -x "$INSTALL_ROOT/.venv/bin/python" ]]; then
  sudo -u "$APP_USER" env HOME="/home/$APP_USER" python3 -m venv "$INSTALL_ROOT/.venv"
fi
sudo -u "$APP_USER" env HOME="/home/$APP_USER" \
  "$INSTALL_ROOT/.venv/bin/pip" install -r "$INSTALL_ROOT/requirements.txt"

install -m 644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable lan-watch
systemctl restart lan-watch

PORT="$(awk '/^port:/{print $2; exit}' "$CONFIG_DIR/config.yaml" 2>/dev/null || true)"
PORT="${PORT:-8080}"
HEALTH_URL="http://127.0.0.1:${PORT}/api/status"

ok=0
for _ in $(seq 1 "$HEALTH_TRIES"); do
  if command -v curl >/dev/null && curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null; then
    ok=1
    break
  fi
  sleep 0.5
done

echo
systemctl --no-pager --full status lan-watch | sed -n '1,12p'
echo

if [[ "$ok" -eq 1 ]]; then
  echo "Deploy OK: $HEALTH_URL"
else
  echo "Service restarted but $HEALTH_URL did not respond yet. Check: journalctl -u lan-watch -n 50" >&2
  exit 1
fi

if grep -qE '^telegram_enabled:[[:space:]]*true' "$CONFIG_DIR/config.yaml"; then
  if ! grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$CONFIG_DIR/telegram.env" \
    || ! grep -qE '^TELEGRAM_CHAT_ID=.+' "$CONFIG_DIR/telegram.env"; then
    echo "Warning: telegram_enabled is true but $CONFIG_DIR/telegram.env is missing token or chat id." >&2
  fi
fi
