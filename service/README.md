# StoveLink MQTT Service

A standalone Python service that processes StoveLink thermal camera packets received via MQTT, stores data in HDF5 format, and generates RGB thermal images for Home Assistant consumption.

## Features

- **MQTT Integration**: Subscribes to StoveLink binary packets and publishes base64-encoded thermal images
- **HDF5 Storage**: Efficiently stores thermal frames and metadata in scalable HDF5 files
- **Image Generation**: Generates colorful RGB images using ironbow colormap from raw thermal data
- **Offload Processing**: Relieves the MCU from image rendering duties
- **Home Assistant Compatible**: Publishes images in formats suitable for HA MQTT camera integration
- **Robust Error Handling**: Graceful handling of malformed packets and connection issues

## Requirements

- Python 3.8+
- MQTT Broker (e.g., Mosquitto)
- See `pyproject.toml` for Python dependencies

## Installation

### Automated Installation (Recommended)

Run the installation script as root:

```bash
sudo service/install.sh
```

This will:
1. Create a dedicated `stovelink` user and group
2. Create installation directories (`/opt/stovelink`, `/mnt/burnie`)
3. Install Python dependencies in a virtual environment
4. Install the systemd service file
5. Configure proper permissions

### Manual Installation

1. Create installation directory:
```bash
sudo mkdir -p /opt/stovelink /mnt/burnie
sudo useradd -r -s /bin/false -d /mnt/burnie stovelink
sudo chown stovelink:stovelink /opt/stovelink /mnt/burnie
```

2. Install to the directory:
```bash
cd service
sudo pip install -e .
```

3. Copy service file:
```bash
sudo cp stovelink-service.service /etc/systemd/system/
```

4. Reload systemd:
```bash
sudo systemctl daemon-reload
```

## Configuration

Edit the systemd service file to customize MQTT settings:

```bash
sudo nano /etc/systemd/system/stovelink-service.service
```

Key configuration options in the `ExecStart` line:

- `--mqtt-host`: MQTT broker hostname (default: localhost)
- `--mqtt-port`: MQTT broker port (default: 1883)
- `--input-topic`: Topic to subscribe for StoveLink packets
- `--output-topic`: Topic to publish thermal images
- `--hdf5-file`: Path to HDF5 storage file
- `--username`: MQTT username (optional)
- `--password`: MQTT password (optional)

Example with authentication:

```bash
ExecStart=/opt/stovelink/.venv/bin/python3 -m stovelink_service \
    --mqtt-host mqtt.example.com \
    --mqtt-port 8883 \
    --username stovelink \
    --password secret_password \
    --input-topic stove/thermal/data \
    --output-topic home/stove/image \
    --hdf5-file /mnt/burnie/data.h5
```

## Running the Service

### Start the service:
```bash
sudo systemctl start stovelink-service
```

### Enable on boot:
```bash
sudo systemctl enable stovelink-service
```

### Check status:
```bash
sudo systemctl status stovelink-service
```

### View logs:
```bash
# Real-time logs
sudo journalctl -u stovelink-service -f

# Last 50 lines
sudo journalctl -u stovelink-service -n 50

# By time
sudo journalctl -u stovelink-service --since "2 hours ago"
```

### Restart the service:
```bash
sudo systemctl restart stovelink-service
```

### Stop the service:
```bash
sudo systemctl stop stovelink-service
```

## Data Storage

The service stores thermal data in HDF5 format with the following structure:

```
thermal_data.h5
├── thermal_frames (shape: [N, 768])     # Raw temperature values in Celsius
├── sequence_ids (shape: [N])            # Frame sequence numbers
├── timestamps_ms (shape: [N])           # Milliseconds since device boot
├── vent_positions (shape: [N])          # Vent position 0-100%
└── combustion_times (shape: [N])        # Seconds since combustion started
```

Where N is the number of frames stored.

### Accessing the data:

```python
import h5py
import numpy as np

with h5py.File('/mnt/burnie/thermal_data.h5', 'r') as f:
    frames = np.array(f['thermal_frames'])
    timestamps = np.array(f['timestamps_ms'])
    vent_pos = np.array(f['vent_positions'])
    
    # Get latest frame
    latest_frame = frames[-1]
    print(f"Latest frame min: {latest_frame.min():.1f}°C, max: {latest_frame.max():.1f}°C")
```

## Home Assistant Integration

Configure MQTT Camera in Home Assistant:

```yaml
camera:
  - platform: mqtt
    name: "Stove Thermal"
    topic: burnie/thermal_camera
    encoding: base64
    unique_id: stove_thermal_camera
```

Or using YAML discovery:

```yaml
mqtt:
  discovery: true
  discovery_prefix: homeassistant
```

The service publishes to the configured topic with base64-encoded BMP images.

## Troubleshooting

### Service fails to start

Check the logs:
```bash
sudo journalctl -u stovelink-service -n 50 --no-pager
```

Common issues:
- MQTT broker not running: Start your MQTT broker
- Permission issues: Check `/mnt/burnie` ownership
- Port in use: Change `--mqtt-port`

### HDF5 file grows too large

The HDF5 file is automatically chunked for efficient storage. To archive old data:

```bash
# Compress the file
h5repack -i /mnt/burnie/thermal_data.h5 \
         -o /mnt/burnie/thermal_data_compressed.h5

# Or rotate the file
sudo systemctl stop stovelink-service
sudo mv /mnt/burnie/thermal_data.h5 /archive/thermal_data_$(date +%Y%m%d).h5
sudo systemctl start stovelink-service
```

### No data being received

1. Verify MQTT broker is running
2. Check topic names match between MCU and service
3. Monitor MQTT traffic:
```bash
mosquitto_sub -h localhost -t "#" | grep stovelink
```

## Development

### Running in development mode:

```bash
cd service
python3 -m pip install -e ".[dev]"
python3 stovelink_service.py \
    --input-topic test/stovelink \
    --output-topic test/thermal \
    --hdf5-file /tmp/test.h5
```

### Running tests:

```bash
pytest tests/
```

## Performance Notes

- Each frame is ~1.5 KB of binary data
- BMP images are ~2-3 KB (base64 encoded)
- HDF5 chunking optimized for sequential access
- Image generation < 5ms per frame on modern hardware

## License

MIT License

## Support

For issues, feature requests, or questions, visit:
https://github.com/hearthmate/hearthmate
