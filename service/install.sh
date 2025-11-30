#!/bin/bash
set -e

# StoveLink Service Installation Script
# This script installs the StoveLink MQTT service as a systemd service

INSTALL_PATH="/opt/stovelink"
DATA_PATH="/mnt/burnie"
SERVICE_USER="stovelink"
SERVICE_GROUP="stovelink"

echo "=========================================="
echo "StoveLink Service Installation"
echo "=========================================="
echo

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (use sudo)"
   exit 1
fi

echo "1. Creating service user and group..."
if id "$SERVICE_USER" &>/dev/null; then
    echo "   User $SERVICE_USER already exists"
else
    useradd -r -s /bin/false -d $DATA_PATH $SERVICE_USER
    echo "   Created user $SERVICE_USER"
fi

echo
echo "2. Creating installation directory..."
mkdir -p "$INSTALL_PATH"
mkdir -p "$DATA_PATH"
chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_PATH"
chown "$SERVICE_USER:$SERVICE_GROUP" "$DATA_PATH"
chmod 755 "$INSTALL_PATH"
chmod 755 "$DATA_PATH"
echo "   Created $INSTALL_PATH and $DATA_PATH"

echo
echo "3. Installing service files..."
cd "$(dirname "$0")"

# Copy service file
cp --update=none stovelink-service.service /etc/systemd/system/
chmod 644 /etc/systemd/system/stovelink-service.service
echo "   Installed systemd service file"

# Copy service code and configuration to install path
cp stovelink_service.py "$INSTALL_PATH/"
cp pyproject.toml "$INSTALL_PATH/"
echo "   Copied service code"

echo
echo "4. Installing Python dependencies..."
cd "$INSTALL_PATH"

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Upgrade pip
.venv/bin/pip install --upgrade pip setuptools wheel

# Install the package in development mode from the current installation directory
pip install -e .

echo "   Installed Python dependencies"

echo
echo "5. Setting up service configuration..."
# Create config directory
mkdir -p "$INSTALL_PATH/config"
chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_PATH/config"

# Copy example config if it exists
if [ -f "$(dirname "$0")/config.example.yaml" ]; then
    cp "$(dirname "$0")/config.example.yaml" "$INSTALL_PATH/config/config.yaml"
    chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_PATH/config/config.yaml"
fi

echo
echo "6. Reloading systemd daemon..."
systemctl daemon-reload
echo "   Systemd daemon reloaded"

echo
echo "=========================================="
echo "Installation Complete!"
echo "=========================================="
echo
echo "Next steps:"
echo "1. Review and update the service configuration:"
echo "   sudo vi /etc/systemd/system/stovelink-service.service"
echo
echo "2. Enable the service to start on boot:"
echo "   sudo systemctl enable stovelink-service"
echo
echo "3. Start the service:"
echo "   sudo systemctl start stovelink-service"
echo
echo "4. Check service status:"
echo "   sudo systemctl status stovelink-service"
echo
echo "5. View service logs:"
echo "   sudo journalctl -u stovelink-service -f"
echo
