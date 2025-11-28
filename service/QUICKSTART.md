# StoveLink Service - Quick Start Guide

## Prerequisites

- Linux system with systemd (Ubuntu, Debian, CentOS, etc.)
- MQTT broker running (e.g., Mosquitto)
- Python 3.8+ installed
- `sudo` access for installation

## Installation (5 minutes)

### 1. Download the service

```bash
cd ~/hearthmate/service
```

### 2. Run the installer

```bash
sudo ./install.sh
```

The script will:
- Create a dedicated `stovelink` user
- Set up `/opt/stovelink` installation directory
- Create `/mnt/burnie` for data storage
- Install Python dependencies in a virtual environment
- Set up the systemd service

### 3. Configure MQTT settings

Edit the systemd service to match your MQTT broker:

```bash
sudo nano /etc/systemd/system/stovelink-service.service
```

Change the `ExecStart` line to match your setup:
- `--mqtt-host`: Your MQTT broker hostname/IP
- `--input-topic`: Topic where MCU sends StoveLink packets
- `--output-topic`: Topic where images will be published

Example for Home Assistant users:
```bash
ExecStart=/opt/stovelink/.venv/bin/python3 -m stovelink_service \
    --mqtt-host 192.168.1.100 \
    --input-topic homeassistant/stove/thermal \
    --output-topic homeassistant/stove/image \
    --hdf5-file /mnt/burnie/thermal_data.h5
```

### 4. Start the service

```bash
# Enable on boot
sudo systemctl enable stovelink-service

# Start now
sudo systemctl start stovelink-service

# Check status
sudo systemctl status stovelink-service
```

## Verify It's Working

### Check service status:
```bash
sudo systemctl status stovelink-service
```

Should show: `● stovelink-service.service - StoveLink MQTT Service... [active (running)]`

### View live logs:
```bash
sudo journalctl -u stovelink-service -f
```

Should show connection messages like:
```
INFO - Connected to MQTT broker at localhost:1883
INFO - Subscribed to topic: homeassistant/stove/thermal
```

### Check data storage:
```bash
ls -lh /mnt/burnie/thermal_data.h5
```

File size should grow as data is received.

## Common Issues & Solutions

### Service won't start

**Error**: "Active: failed"

**Solution**:
```bash
sudo journalctl -u stovelink-service -n 50
# Read the error message
# Common issues:
# - MQTT broker not running
# - Wrong topic names
# - Permission issues on /mnt/burnie
```

### No data being stored

**Check MQTT connectivity**:
```bash
mosquitto_sub -h localhost -t "homeassistant/stove/+/#"
# Should show messages from your MCU
```

**Check topic names** match between MCU and service configuration.

### High CPU/Memory usage

The service uses minimal resources:
- ~50 MB RAM base
- <5% CPU per 10 incoming messages/second

If usage is higher:
1. Check for massive HDF5 file (archive old data)
2. Check MQTT broker is not flooding with messages

## Integration with Home Assistant

### MQTT Camera Setup

Add to `configuration.yaml`:

```yaml
camera:
  - platform: mqtt
    name: "Stove Thermal Camera"
    topic: homeassistant/stove/image
    encoding: base64
    unique_id: stove_thermal_camera
```

Restart Home Assistant or reload MQTT entities.

## Next Steps

- Read the full [README.md](README.md) for detailed documentation
- Configure firewall if needed: `sudo ufw allow in on <interface> from <mqtt-ip> to any port 1883`
- Set up automatic data archival for old HDF5 files
- Create automations in Home Assistant based on image updates

## Support

If you encounter issues:

1. Check logs: `sudo journalctl -u stovelink-service -n 100`
2. Verify MQTT connectivity
3. Ensure Python 3.8+ is installed
4. Check file permissions on `/mnt/burnie`

For detailed help, see [README.md](README.md) Troubleshooting section.
