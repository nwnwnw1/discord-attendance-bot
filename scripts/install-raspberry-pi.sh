#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="discord-attendance"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${SUDO_USER:-$(id -un)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "python3 is required. Install it with: sudo apt install -y python3 python3-venv"
  exit 1
fi

if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
  echo "python3-venv is required. Install it with: sudo apt install -y python3-venv"
  exit 1
fi

cd "$APP_DIR"

echo "Creating virtual environment in ${APP_DIR}/.venv"
"$PYTHON_BIN" -m venv .venv

echo "Installing Python dependencies"
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created ${APP_DIR}/.env"
  echo "Edit .env and set DISCORD_TOKEN before starting the service."
fi

if [ "$(id -u)" -eq 0 ]; then
  chown -R "${APP_USER}:${APP_USER}" .venv .env
fi

if command -v systemctl >/dev/null 2>&1; then
  if [ "$(id -u)" -ne 0 ]; then
    echo "Installing the systemd service requires sudo:"
    echo "  sudo $0"
    exit 0
  fi

  echo "Writing ${SERVICE_FILE}"
  cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=Discord Attendance Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"

  if grep -q "^DISCORD_TOKEN=your_bot_token$" .env; then
    echo "Service installed but not started because DISCORD_TOKEN is not configured."
    echo "After editing .env, run: sudo systemctl start ${SERVICE_NAME}"
  else
    systemctl restart "$SERVICE_NAME"
    echo "Service started. Check logs with: journalctl -u ${SERVICE_NAME} -f"
  fi
else
  echo "systemd was not found. Start the bot manually with: .venv/bin/python bot.py"
fi
