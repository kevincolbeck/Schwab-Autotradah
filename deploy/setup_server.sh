#!/bin/bash
# One-time server setup for schwab-momentum-bot on Ubuntu 22.04/24.04.
# Run as root on a fresh server: bash deploy/setup_server.sh
set -e

REPO_URL="https://github.com/kevincolbeck/schwab-momentum-bot.git"
APP_DIR="/home/ubuntu/schwab-momentum-bot"
SERVICE_FILE="schwab-momentum-bot.service"

echo "========================================"
echo " Schwab Momentum Bot — Server Setup"
echo "========================================"

echo ""
echo "==> [1/6] Updating system packages..."
apt-get update -qq
apt-get install -y -qq git python3.12 python3.12-venv python3.12-dev python3-pip build-essential

echo "    Python $(python3.12 --version)"

echo ""
echo "==> [2/6] Ensuring ubuntu user exists..."
if ! id ubuntu &>/dev/null; then
    useradd -m -s /bin/bash ubuntu
fi

echo ""
echo "==> [3/6] Cloning repository..."
if [ -d "$APP_DIR" ]; then
    echo "    Already exists — pulling latest..."
    sudo -u ubuntu git -C "$APP_DIR" pull
else
    sudo -u ubuntu git clone "$REPO_URL" "$APP_DIR"
fi

echo ""
echo "==> [4/6] Setting up Python virtualenv..."
sudo -u ubuntu python3.12 -m venv "$APP_DIR/venv"
sudo -u ubuntu "$APP_DIR/venv/bin/pip" install --upgrade pip -q
sudo -u ubuntu "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements_server.txt" -q
echo "    Dependencies installed"

echo ""
echo "==> [5/6] Checking config and token files..."
if [ ! -f "$APP_DIR/config.yaml" ]; then
    sudo -u ubuntu cp "$APP_DIR/config.yaml.example" "$APP_DIR/config.yaml"
    echo ""
    echo "  *** ACTION REQUIRED ***"
    echo "  Created $APP_DIR/config.yaml from example."
    echo "  Edit it with your real credentials:"
    echo "    nano $APP_DIR/config.yaml"
    echo ""
    echo "  Also upload your token files from your local machine:"
    echo "    scp token.json.enc ubuntu@5.161.236.123:$APP_DIR/"
    echo "    scp .token_key ubuntu@5.161.236.123:$APP_DIR/"
    echo ""
    echo "  Press ENTER when done."
    read -r
else
    echo "    config.yaml already exists"
fi

if [ ! -f "$APP_DIR/token.json.enc" ] && [ ! -f "$APP_DIR/token.json" ]; then
    echo ""
    echo "  WARNING: No token file found at $APP_DIR/token.json.enc"
    echo "  Upload it before starting the service."
fi

echo ""
echo "==> [6/6] Installing systemd service..."
cp "$APP_DIR/deploy/$SERVICE_FILE" "/etc/systemd/system/$SERVICE_FILE"
systemctl daemon-reload
systemctl enable schwab-momentum-bot
systemctl start schwab-momentum-bot

echo ""
echo "========================================"
echo " Setup complete!"
echo "========================================"
echo ""
echo " Service status:   sudo systemctl status schwab-momentum-bot"
echo " Live logs:        sudo journalctl -u schwab-momentum-bot -f"
echo " Restart:          sudo systemctl restart schwab-momentum-bot"
echo "========================================"
