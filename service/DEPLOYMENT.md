# StoveLink Service - Deployment Guide

## Overview

The StoveLink MQTT Service is a production-ready Python application that processes thermal camera data from your wood stove MCU. This guide covers deployment to a Linux system with systemd.

## Package Contents

```
service/
├── stovelink_service.py           Main application (16 KB)
├── pyproject.toml                 Python package config
├── stovelink-service.service      Systemd service unit
├── install.sh                     Automated installer
├── config.example.yaml            Configuration template
├── README.md                       Full documentation
├── QUICKSTART.md                  5-minute setup guide
├── INSTALLATION_SUMMARY.txt       Summary of components
└── DEPLOYMENT.md                  This file
```

## Automated Installation (Recommended)

### Step 1: Copy files to target system

```bash
# On your development machine
scp -r ~/hearthmate/service user@target-system:~/stovelink-service

# Or if local
cp -r ~/hearthmate/service ~/stovelink-service
```

### Step 2: Run the installer

```bash
cd ~/stovelink-service
sudo ./install.sh
```

The installer will:
1. Create `stovelink` user and group
2. Create `/opt/stovelink` and `/mnt/burnie` directories
3. Set up Python virtual environment
4. Install dependencies: paho-mqtt, h5py, numpy
5. Install systemd service
6. Set correct permissions

### Step 3: Configure

Edit the systemd service to match your MQTT setup:

```bash
sudo nano /etc/systemd/system/stovelink-service.service
```

Key changes in `ExecStart`:
- `--mqtt-host`: Your MQTT broker IP/hostname
- `--input-topic`: Topic from your MCU (e.g., `stove/thermal/data`)
- `--output-topic`: Topic for Home Assistant (e.g., `home/stove/image`)

### Step 4: Start the service

```bash
# Enable on boot
sudo systemctl enable stovelink-service

# Start now
sudo systemctl start stovelink-service

# Verify
sudo systemctl status stovelink-service
sudo journalctl -u stovelink-service -f
```

## Manual Installation

For systems without bash or if you prefer manual setup:

```bash
# 1. Create user and directories
sudo useradd -r -s /bin/false -d /mnt/burnie stovelink
sudo mkdir -p /opt/stovelink /mnt/burnie
sudo chown stovelink:stovelink /opt/stovelink /mnt/burnie

# 2. Copy files
sudo cp service/stovelink_service.py /opt/stovelink/
sudo cp service/pyproject.toml /opt/stovelink/

# 3. Set up Python environment
cd /opt/stovelink
sudo python3 -m venv .venv
sudo .venv/bin/pip install --upgrade pip
sudo .venv/bin/pip install paho-mqtt h5py numpy

# 4. Install systemd service
sudo cp service/stovelink-service.service /etc/systemd/system/
sudo systemctl daemon-reload

# 5. Configure (edit MQTT settings)
sudo nano /etc/systemd/system/stovelink-service.service

# 6. Start service
sudo systemctl enable stovelink-service
sudo systemctl start stovelink-service
```

## Configuration

### MQTT Settings

Edit `/etc/systemd/system/stovelink-service.service`:

```ini
[Service]
ExecStart=/opt/stovelink/.venv/bin/python3 -m stovelink_service \
    --mqtt-host mqtt.example.com \
    --mqtt-port 1883 \
    --input-topic stove/thermal/raw \
    --output-topic stove/thermal/image \
    --hdf5-file /mnt/burnie/thermal_data.h5
```

### With Authentication

```ini
ExecStart=/opt/stovelink/.venv/bin/python3 -m stovelink_service \
    --mqtt-host mqtt.example.com \
    --mqtt-port 8883 \
    --username stovelink_user \
    --password secure_password \
    --input-topic stove/thermal/raw \
    --output-topic stove/thermal/image \
    --hdf5-file /mnt/burnie/thermal_data.h5
```

## Service Management

### Check Status
```bash
sudo systemctl status stovelink-service
```

### View Logs
```bash
# Live view
sudo journalctl -u stovelink-service -f

# Last 50 lines
sudo journalctl -u stovelink-service -n 50

# Since last boot
sudo journalctl -u stovelink-service -b

# By time
sudo journalctl -u stovelink-service --since "2 hours ago"
```

### Restart Service
```bash
sudo systemctl restart stovelink-service
```

### Stop Service
```bash
sudo systemctl stop stovelink-service
```

### Disable from Auto-start
```bash
sudo systemctl disable stovelink-service
```

## Monitoring

### Check if service is running
```bash
systemctl is-active stovelink-service
# Output: active or inactive
```

### Check CPU/Memory usage
```bash
ps aux | grep stovelink_service
# Should use < 5% CPU, ~50-100 MB RAM
```

### Check HDF5 file growth
```bash
watch -n 5 'ls -lh /mnt/burnie/thermal_data.h5'
# File should grow over time as data arrives
```

### Monitor MQTT traffic
```bash
mosquitto_sub -h <mqtt-host> -t "stove/#" | grep -E "thermal|image"
```

## Data Management

### Archive old data

```bash
# Stop service
sudo systemctl stop stovelink-service

# Backup current file
sudo cp /mnt/burnie/thermal_data.h5 \
       /archive/thermal_data_$(date +%Y%m%d_%H%M%S).h5

# Start fresh
sudo rm /mnt/burnie/thermal_data.h5

# Restart service
sudo systemctl start stovelink-service
```

### Compress HDF5 file

```bash
# Install h5py if needed
sudo .venv/bin/pip install h5py

# Compress
h5repack -i /mnt/burnie/thermal_data.h5 \
         -o /mnt/burnie/thermal_data_compressed.h5

# Verify and swap
sudo systemctl stop stovelink-service
sudo mv /mnt/burnie/thermal_data_compressed.h5 \
       /mnt/burnie/thermal_data.h5
sudo chown stovelink:stovelink /mnt/burnie/thermal_data.h5
sudo systemctl start stovelink-service
```

## Troubleshooting

### Service fails to start

```bash
# Check logs
sudo journalctl -u stovelink-service -n 100

# Common issues:
# - MQTT broker not running
# - Wrong MQTT credentials
# - Topic names incorrect
# - Port already in use
# - Permission issues on /mnt/burnie
```

### Fix permission issues

```bash
sudo chown -R stovelink:stovelink /opt/stovelink
sudo chown -R stovelink:stovelink /mnt/burnie
sudo chmod 755 /opt/stovelink /mnt/burnie
```

### Verify MQTT connectivity

```bash
# Test connection
mosquitto_sub -h <mqtt-host> -t "stove/#" -C 1

# Should receive messages from MCU or see timeout
```

### Reset to factory defaults

```bash
sudo systemctl stop stovelink-service
sudo rm /mnt/burnie/thermal_data.h5
sudo systemctl start stovelink-service
```

## Upgrading

To upgrade to a newer version:

```bash
# Download new version
cd ~/stovelink-service
git pull  # or download new files

# Stop service
sudo systemctl stop stovelink-service

# Copy new code
sudo cp stovelink_service.py /opt/stovelink/

# Update dependencies if needed
sudo /opt/stovelink/.venv/bin/pip install --upgrade paho-mqtt h5py numpy

# Start service
sudo systemctl start stovelink-service

# Verify
sudo systemctl status stovelink-service
```

## Performance Tuning

### Increase verbosity for debugging

Add to `ExecStart`:
```bash
# Note: Edit the file, not the command line
# Set environment variable
Environment=PYTHONUNBUFFERED=1
# Output will go to journal with timestamps
```

### Limit resource usage

Edit `/etc/systemd/system/stovelink-service.service`:

```ini
[Service]
# Limit memory to 256 MB
MemoryLimit=256M

# Limit CPU to one core
CPUQuota=100%

# I/O limiting
BlockIOWeight=100
```

### Performance monitoring

```bash
# Monitor system resources
watch -n 1 'ps aux | grep stovelink_service'

# Memory usage
free -h && ps aux | grep stovelink_service

# Disk I/O
iostat -x 1 10 | grep sda
```

## Security Hardening

The systemd service includes security features:

```ini
NoNewPrivileges=true      # Prevent privilege escalation
PrivateTmp=true           # Isolated /tmp
ProtectSystem=strict      # Read-only filesystem
ProtectHome=yes           # Cannot access home directories
ReadWritePaths=/mnt/burnie  # Only writable location
```

For additional security:

```bash
# Run systemd as unprivileged user (already done)
id stovelink

# Restrict file permissions
sudo chmod 750 /mnt/burnie

# Use firewall rules
sudo ufw allow in on eth0 from 192.168.1.100 to any port 1883
```

## Support

For issues:
1. Check `/var/log/syslog` or `journalctl`
2. Verify MQTT broker connectivity
3. Check topic names match MCU configuration
4. See README.md Troubleshooting section

For detailed documentation, see:
- README.md - Full reference
- QUICKSTART.md - 5-minute setup
- stovelink_service.py - Source code documentation
