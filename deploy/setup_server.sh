#!/bin/bash
# One-time server setup for equities-bot on Ubuntu 22.04/24.04.
# Run as root (or with sudo) on a fresh server:
#   bash deploy/setup_server.sh
set -e

REPO_URL="https://github.com/kevincolbeck/Schwab-Autotradah.git"
APP_DIR="/home/ubuntu/equities_bot"
SERVICE_FILE="equities-bot.service"

echo "========================================"
echo " Equities Bot — Server Setup"
echo "========================================"

# ── 1. System packages ────────────────────────────────────────────────────────
echo ""
echo "==> [1/8] Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    git curl wget build-essential \
    python3.12 python3.12-venv python3.12-dev \
    python3-pip \
    nginx \
    ufw

# ── 2. Node.js 20 LTS (for frontend build) ────────────────────────────────────
echo ""
echo "==> [2/8] Installing Node.js 20 LTS..."
curl -fsSL https://deb.nodesource.com/setup_20.x | bash - > /dev/null 2>&1
apt-get install -y -qq nodejs

echo "    Node $(node --version) | npm $(npm --version)"
echo "    Python $(python3.12 --version)"

# ── 3. Create ubuntu user if not present ─────────────────────────────────────
echo ""
echo "==> [3/8] Ensuring ubuntu user exists..."
if ! id ubuntu &>/dev/null; then
    useradd -m -s /bin/bash ubuntu
    echo "    Created user: ubuntu"
else
    echo "    User ubuntu already exists"
fi

# ── 4. Clone repo ─────────────────────────────────────────────────────────────
echo ""
echo "==> [4/8] Cloning repository..."
if [ -d "$APP_DIR" ]; then
    echo "    $APP_DIR already exists — pulling latest..."
    sudo -u ubuntu git -C "$APP_DIR" pull
else
    sudo -u ubuntu git clone "$REPO_URL" "$APP_DIR"
fi

# ── 5. Python virtualenv + dependencies ───────────────────────────────────────
echo ""
echo "==> [5/8] Setting up Python virtualenv..."
sudo -u ubuntu python3.12 -m venv "$APP_DIR/venv"
sudo -u ubuntu "$APP_DIR/venv/bin/pip" install --upgrade pip -q
sudo -u ubuntu "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
echo "    Python dependencies installed"

# ── 6. Build React frontend ───────────────────────────────────────────────────
echo ""
echo "==> [6/8] Building React frontend..."
sudo -u ubuntu bash -c "cd $APP_DIR/frontend && npm install --silent && npm run build"
echo "    Frontend built → frontend/dist/"

# ── 7. .env file ─────────────────────────────────────────────────────────────
echo ""
echo "==> [7/8] Checking .env..."
if [ ! -f "$APP_DIR/.env" ]; then
    sudo -u ubuntu cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo ""
    echo "  *** ACTION REQUIRED ***"
    echo "  Created $APP_DIR/.env from .env.example."
    echo "  Edit it now with your real credentials:"
    echo "    nano $APP_DIR/.env"
    echo ""
    echo "  Required fields:"
    echo "    SCHWAB_CLIENT_ID=..."
    echo "    SCHWAB_CLIENT_SECRET=..."
    echo "    SCHWAB_REDIRECT_URI=https://127.0.0.1:8182/callback"
    echo "    FINNHUB_API_KEY=..."
    echo ""
    echo "  Press ENTER when done, then the script will continue."
    read -r
else
    echo "    .env already exists — skipping"
fi

# ── 8. Systemd service ────────────────────────────────────────────────────────
echo ""
echo "==> [8/8] Installing systemd service..."
cp "$APP_DIR/deploy/$SERVICE_FILE" "/etc/systemd/system/$SERVICE_FILE"
systemctl daemon-reload
systemctl enable equities-bot
systemctl start equities-bot

echo ""
echo "========================================"
echo " Setup complete!"
echo "========================================"
echo ""
echo " Service status:   sudo systemctl status equities-bot"
echo " Live logs:        sudo journalctl -u equities-bot -f"
echo " Restart:          sudo systemctl restart equities-bot"
echo ""
echo " Bot API:          http://$(curl -s ifconfig.me):8001"
echo " Auth flow:        http://$(curl -s ifconfig.me):8001/auth/url"
echo " Dashboard:        http://$(curl -s ifconfig.me):8001"
echo ""
echo " NEXT: Visit the auth URL above in your browser to connect Schwab."
echo "========================================"
