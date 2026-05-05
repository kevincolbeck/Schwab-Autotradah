#!/bin/bash
# Deploy latest code to server and restart the bot.
# Run from your LOCAL machine: bash deploy/update.sh
# Requires SSH key access to the server.
set -e

SERVER="ubuntu@YOUR_SERVER_IP"
REMOTE_DIR="/home/ubuntu/equities_bot"
SSH_KEY="$HOME/.ssh/id_equities"   # update with your key path

echo "==> Syncing code..."
rsync -avz --exclude='.git' \
  --exclude='.env' \
  --exclude='*.db' \
  --exclude='*.duckdb' \
  --exclude='__pycache__' \
  --exclude='venv' \
  --exclude='frontend/node_modules' \
  --exclude='frontend/dist' \
  ./ "$SERVER:$REMOTE_DIR/"

echo "==> Building frontend..."
ssh -i "$SSH_KEY" "$SERVER" "
  cd $REMOTE_DIR/frontend
  npm install --silent
  npm run build
"

echo "==> Installing Python deps..."
ssh -i "$SSH_KEY" "$SERVER" "
  cd $REMOTE_DIR
  source venv/bin/activate
  pip install -q -r requirements.txt
"

echo "==> Restarting service..."
ssh -i "$SSH_KEY" "$SERVER" "sudo systemctl restart equities-bot"

echo "==> Tailing logs (Ctrl+C to stop)..."
ssh -i "$SSH_KEY" "$SERVER" "sudo journalctl -u equities-bot -f --no-pager"
