#!/bin/bash
# Deploy latest code to server and restart the bot.
# Run from your LOCAL machine: bash deploy/update.sh
set -e

SERVER="ubuntu@YOUR_SERVER_IP"        # <-- replace with your server IP
REMOTE_DIR="/home/ubuntu/schwab-momentum-bot"
SSH_KEY="$HOME/.ssh/id_equities"       # update if your key path differs

echo "==> Syncing code..."
rsync -avz \
  --exclude='.git' \
  --exclude='config.yaml' \
  --exclude='.token_key' \
  --exclude='token.json' \
  --exclude='token.json.enc' \
  --exclude='*.db' \
  --exclude='*.log' \
  --exclude='__pycache__' \
  --exclude='venv' \
  --exclude='dist' \
  --exclude='build' \
  --exclude='backtest_data.db' \
  --exclude='backtest_runs' \
  -e "ssh -i $SSH_KEY" \
  ./ "$SERVER:$REMOTE_DIR/"

echo "==> Installing any new dependencies..."
ssh -i "$SSH_KEY" "$SERVER" "
  cd $REMOTE_DIR
  source venv/bin/activate
  pip install -q -r requirements_server.txt
"

echo "==> Restarting service..."
ssh -i "$SSH_KEY" "$SERVER" "sudo systemctl restart schwab-momentum-bot"

echo ""
echo "==> Done. Tailing logs (Ctrl+C to stop)..."
ssh -i "$SSH_KEY" "$SERVER" "sudo journalctl -u schwab-momentum-bot -f --no-pager"
