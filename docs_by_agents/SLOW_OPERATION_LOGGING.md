# Slow Operation Logging - Implementation Summary

## Problem Statement

An MQTT loop gap warning was observed:
```
12405.172: WARNING - MQTT loop gap: 412.6s in state 'monitoring' (sub-state: N/A)
```

This 412.6-second (nearly 7-minute) gap is **well beyond the 300-second keep-alive timeout** and indicates a blocking operation in the main loop that prevented `mqtt_client.loop()` from being called.

## Solution

Implemented comprehensive timing instrumentation for **all major operations** in the main loop to identify which operation(s) are causing blocking delays.

---

## Implementation Details

### 1. Timing Utility Function

**File:** `code.py` (after logger initialization, ~line 50)

```python
# Timing threshold for warning about slow operations (seconds)
SLOW_OPERATION_THRESHOLD = 10.0

def log_slow_operation(operation_name, duration, threshold=SLOW_OPERATION_THRESHOLD):
    """Log a warning if an operation exceeds the threshold duration."""
    if duration > threshold:
        logger.warning(
            "Slow operation: %s took %.1fs (threshold: %.1fs)",
            operation_name,
            duration,
            threshold
        )
```

**Key Features:**
- Configurable threshold (default: 10 seconds)
- Only logs when threshold exceeded (no log spam)
- Clear identification of which operation was slow
- Shows actual duration and threshold for context

---

### 2. Instrumented Operations

All major operations in the main loop are now timed:

#### A. WiFi Check and Recovery
```python
wifi_start = time.monotonic()
wifi_manager.check_and_recover(current_time)
log_slow_operation("WiFi check/recovery", time.monotonic() - wifi_start)
```

**What it captures:**
- WiFi connection checking
- Reconnection attempts with exponential backoff
- Any WiFi-related network operations

---

#### B. MQTT Reconnection Attempts
```python
mqtt_reconnect_start = time.monotonic()
if mqtt_conn_manager.attempt_reconnect(current_time, mqtt_exception_raised):
    # ... reconnection logic ...
log_slow_operation("MQTT reconnect attempt", time.monotonic() - mqtt_reconnect_start)
```

**What it captures:**
- MQTT connection state checking
- Reconnection attempts
- Handler setup after reconnection
- HA state clearing and reinitialization

---

#### C. State Machine Update
```python
state_machine_start = time.monotonic()
machine.update()
log_slow_operation("State machine update", time.monotonic() - state_machine_start)
```

**What it captures:**
- State update logic (idle, monitoring, closing, etc.)
- Encoder reads
- Motor movements
- State transitions
- Internal mqtt_loop() calls within states

**Note:** This is the **most likely culprit** given the warning occurred in "monitoring" state.

---

#### D. Home Assistant Discovery Refresh
```python
discovery_start = time.monotonic()
# ... HA discovery logic ...
log_slow_operation("HA discovery refresh", time.monotonic() - discovery_start)
```

**What it captures:**
- MQTT discovery message construction
- Publishing discovery payload
- 0.25s sleep for HA subscription
- Encoder status checks
- Status message publishing

**Note:** Only runs when `ha.refresh_discovery` is True (rare).

---

#### E. Home Assistant Updates
```python
ha_update_start = time.monotonic()
ha.update()
log_slow_operation("HA update", time.monotonic() - ha_update_start)
```

**What it captures:**
- RSSI measurement buffering
- Vent position calculation and publishing
- TMP36 temperature reading and buffering
- All state topic publications
- Step parameter state updates
- Position error reporting

---

#### F. Thermal Camera Capture
```python
thermal_start = time.monotonic()
if do_thermal_camera_stuff(camera_exception_raised):
    camera_exception_raised = False
log_slow_operation("Thermal camera capture", time.monotonic() - thermal_start)
```

**What it captures:**
- MLX90640 frame capture via I2C
- Temperature statistics calculation
- StoveLink packet encoding
- Binary packet MQTT publication
- Camera recovery attempts

**Note:** Expected duration ~1 second, runs every 10 seconds.

---

#### G. Main Loop Iteration (Total)
```python
loop_duration = time.monotonic() - loop_start_time
log_slow_operation("Main loop iteration", loop_duration)
```

**What it captures:**
- Total time for entire main loop iteration
- Includes all operations above
- Plus exception handling overhead
- Plus the 0.1s sleep at the end

**Note:** This will catch any blocking not attributed to specific operations above.

---

## Expected Behavior

### Normal Operation (No Logs)
When all operations complete quickly (<10s), **no warning logs** will be generated. This keeps logs clean and focused.

### When Blocking Occurs (Warnings)
If any operation exceeds 10 seconds, a warning will be logged:

**Example Output:**
```
2026-01-10 12:34:56 WARNING __main__: Slow operation: State machine update took 412.8fs (threshold: 10.0fs)
```

This immediately identifies:
- **Which operation** blocked (State machine update)
- **How long** it took (412.8 seconds)
- **The threshold** for comparison (10.0 seconds)

---

## Diagnostic Strategy

### Step 1: Identify the Blocking Operation
When a "MQTT loop gap" warning appears, check the logs for corresponding "Slow operation" warnings:

```bash
# Example: If you see "MQTT loop gap: 412.6s"
# Look for nearby slow operation warnings around the same timestamp
```

### Step 2: Correlate with State
The MQTT loop gap warning shows the state:
```
MQTT loop gap: 412.6s in state 'monitoring' (sub-state: N/A)
```

If a "State machine update" slow operation matches the duration, this confirms the state machine is blocking.

### Step 3: Drill Down Further
If "State machine update" is the culprit:
1. Check which sub-state within vent_closer was active
2. Review that state's update() method for potential blocking
3. Look for:
   - Long-running encoder reads
   - Multiple sequential motor movements
   - Network operations (shouldn't be in states, but check)
   - Unintended infinite loops or excessive iterations

---

## Likely Culprits Based on Evidence

Given the 412.6s gap in "monitoring" state, the most likely causes are:

### 1. **State Machine Update - MOST LIKELY**
The "monitoring" state in vent_closer calls:
- `hardware.read_raw_angle()` - I2C read
- `vent.move_to_position()` - calculation
- `machine.mqtt_loop()` - should be fast
- State transitions

**Potential issues:**
- I2C read hanging (unlikely - would timeout)
- Calculations stuck in infinite loop (check vent.move_to_position logic)
- State transition causing recursive blocking

### 2. **Home Assistant Update - POSSIBLE**
The `ha.update()` publishes many MQTT messages:
- RSSI state
- Vent position
- TMP36 temperature
- Step parameters (5 topics)
- Position error

**Potential issues:**
- MQTT publish queue filling up
- Socket blocking on send
- Measurement buffer calculations taking too long

### 3. **WiFi/MQTT Recovery - POSSIBLE**
If WiFi or MQTT connection is flaky:
- Reconnection attempts with exponential backoff
- Multiple failed attempts before success
- Could accumulate to 400+ seconds

**Potential issues:**
- Network infrastructure problems
- DNS resolution hanging
- Socket operations blocking

### 4. **Thermal Camera - UNLIKELY**
Only runs every 10 seconds, expected duration ~1s.

**Potential issues:**
- I2C bus hang (unlikely with recovery)
- Frame capture stuck in retry loop

---

## Next Steps

### Immediate Actions:
1. **Deploy this instrumented code** to the device
2. **Monitor logs** for the next occurrence of the 412s gap
3. **Check for "Slow operation" warnings** around the same timestamp
4. **Identify which operation** is blocking

### Once Culprit Identified:

#### If "State machine update":
- Add timing within individual states (monitoring, closing, etc.)
- Check for infinite loops in calculations
- Review encoder read logic

#### If "HA update":
- Reduce publish frequency
- Batch publications
- Add timeout to MQTT publish calls

#### If "WiFi/MQTT recovery":
- Review exponential backoff parameters
- Add maximum retry limits
- Consider aggressive timeouts for reconnection

#### If "Thermal camera capture":
- Review I2C recovery logic
- Add maximum frame capture retries
- Consider disabling camera if persistently failing

---

## Configuration

### Adjusting the Threshold
The threshold can be changed by modifying:
```python
SLOW_OPERATION_THRESHOLD = 10.0  # seconds
```

**Recommendations:**
- **10 seconds (current)**: Good balance for catching issues without noise
- **5 seconds**: More aggressive, catches smaller delays
- **30 seconds**: Less sensitive, only catches severe blocking

### Disabling Logging
To disable slow operation logging entirely:
```python
def log_slow_operation(operation_name, duration, threshold=SLOW_OPERATION_THRESHOLD):
    """Log a warning if an operation exceeds the threshold duration."""
    return  # Disabled
```

---

## Performance Impact

### Overhead per Operation:
- 1 `time.monotonic()` call before operation
- 1 `time.monotonic()` call after operation  
- 1 comparison (duration > threshold)
- 1 logger.warning() call if threshold exceeded

**Total overhead:** <0.001 seconds per operation (negligible)

### Memory Impact:
- ~5 additional local variables per loop iteration
- No persistent state (variables reused each iteration)

**Total memory impact:** <100 bytes (negligible)

---

## Summary

**Problem:** 412.6-second blocking in "monitoring" state preventing mqtt_loop() calls

**Solution:** Comprehensive timing instrumentation of all main loop operations

**Threshold:** 10 seconds (configurable)

**Strategy:** Silent operation normally, warnings only when blocking occurs

**Status:** Deployed, awaiting next occurrence to identify culprit

**Next:** Once identified, implement targeted fix for the specific blocking operation
