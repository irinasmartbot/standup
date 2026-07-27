#!/usr/bin/env bash
# Bootstrap VK test bot beside Telegram on the same VPS.
# Run ON THE SERVER as user standup (or with sudo where needed).
#
# Usage:
#   bash scripts/bootstrap_vk_server.sh
#
# Prerequisites:
#   - origin/vk-mvp already pushed to GitHub
#   - /home/standup/app/.env exists with working DATABASE_URL
#   - VK_* vars will be merged from existing .env if present; otherwise edit after copy

set -euo pipefail

APP_DIR="${VK_APP_DIR:-/home/standup/vk-app}"
SRC_ENV="${SRC_ENV:-/home/standup/app/.env}"
REPO_URL="${REPO_URL:-https://github.com/irinasmartbot/standup.git}"
BRANCH="${BRANCH:-vk-mvp}"

echo "==> App dir: $APP_DIR"
echo "==> Branch:  $BRANCH"

if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

if [[ ! -d venv ]]; then
  python3 -m venv venv
fi
./venv/bin/pip install -r requirements.txt

if [[ ! -f .env ]]; then
  if [[ -f "$SRC_ENV" ]]; then
    cp "$SRC_ENV" .env
    echo "==> Copied .env from $SRC_ENV"
  else
    echo "ERROR: no .env at $SRC_ENV and no $APP_DIR/.env"
    exit 1
  fi
fi

# Ensure VK is enabled if the key exists; do not invent tokens.
if grep -q '^VK_ENABLED=' .env; then
  sed -i 's/^VK_ENABLED=.*/VK_ENABLED=1/' .env
else
  echo 'VK_ENABLED=1' >> .env
fi

if ! grep -q '^VK_GROUP_TOKEN=.\+' .env; then
  echo
  echo "WARN: VK_GROUP_TOKEN is missing/empty in $APP_DIR/.env"
  echo "      Add VK_GROUP_ID / VK_GROUP_TOKEN / VK_ADMIN_PEER_ID / VK_MANAGER_LINK / VK_COMMUNITY_LINK"
  echo "      then re-run image upload + systemctl start."
fi

echo
echo "==> Optional: upload system images (set VK_ADMIN_PEER_ID first)"
echo "    ./venv/bin/python scripts/upload_vk_system_images.py --peer-id \"\$VK_ADMIN_PEER_ID\""
echo
echo "==> Install systemd unit:"
echo "    sudo cp $APP_DIR/deploy/standup-vk-bot.service /etc/systemd/system/"
echo "    sudo systemctl daemon-reload"
echo "    sudo systemctl enable --now standup-vk-bot"
echo "    sudo systemctl status standup-vk-bot"
echo "    sudo journalctl -u standup-vk-bot -n 80 --no-pager"
echo
echo "Done bootstrap of $APP_DIR on $BRANCH"
