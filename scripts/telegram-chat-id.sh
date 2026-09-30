#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$DIR/secrets/telegram.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE"
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  echo "Paste TELEGRAM_BOT_TOKEN into $ENV_FILE first (chmod 600), then message the bot in Telegram."
  exit 1
fi
echo "Recent chats (open the bot, tap Start, send any message, then run this):"
curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates"
echo
echo "Copy result[].message.chat.id into TELEGRAM_CHAT_ID in $ENV_FILE, then restart."
