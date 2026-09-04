# ooler-ble-client

[![PyPI](https://img.shields.io/pypi/v/ooler-ble-client)](https://pypi.org/project/ooler-ble-client/)
[![Python](https://img.shields.io/pypi/pyversions/ooler-ble-client)](https://pypi.org/project/ooler-ble-client/)
[![License](https://img.shields.io/pypi/l/ooler-ble-client)](https://github.com/PostLogical/ooler_ble_client/blob/main/LICENSE)

A Python library to communicate with [Ooler Sleep System](https://www.chilisleep.com/) Bluetooth devices via BLE GATT characteristics. Built on [bleak](https://github.com/hbldh/bleak) and [bleak-retry-connector](https://github.com/Bluetooth-Devices/bleak-retry-connector).

Designed for use with the [Home Assistant Ooler integration](https://github.com/PostLogical/ooler), but can be used standalone.

## Installation

```bash
pip install ooler-ble-client
```

## Usage

```python
import asyncio
from bleak import BleakScanner
from ooler_ble_client import OolerBLEDevice

async def main():
    # Discover the device
    device = await BleakScanner.find_device_by_name("OOLER")

    # Create client and connect
    client = OolerBLEDevice(model="OOLER")
    client.set_ble_device(device)
    await client.connect()

    # Read state
    print(client.state)

    # Control the device
    await client.set_power(True)
    await client.set_temperature(72)
    await client.set_mode("Regular")

    # Listen for state changes
    def on_state_change(state):
        print(f"State changed: {state}")

    unsubscribe = client.register_callback(on_state_change)

    # Clean up
    unsubscribe()
    await client.stop()

asyncio.run(main())
```

## API

### `OolerBLEDevice(model: str)`

Main client class.

- `set_ble_device(device)` -- set the BLE device to connect to
- `connect()` -- establish BLE connection, read initial state, subscribe to notifications
- `stop()` -- unsubscribe from notifications and disconnect
- `is_connected` -- whether the device is currently connected
- `state` -- current `OolerBLEState`
- `register_callback(fn)` -- register a state change callback, returns an unsubscribe function
- `async_poll()` -- read all characteristics from the device
- `set_power(bool)` -- turn device on/off (re-sends mode and temperature on power-on)
- `set_mode(OolerMode)` -- set pump mode: `"Silent"`, `"Regular"`, or `"Boost"`. Raises `DeviceOffError` if the device is off
- `set_temperature(int)` -- set target temperature in the current display unit. Raises `DeviceOffError` if the device is off
- `set_clean(bool)` -- start/stop clean cycle. Starting one powers the device on; stopping one does not, since the device drops writes while off
- `fix_setpoint_override(setpoint=None, clean_seconds=None)` -- clear the firmware behaviour described in [Setpoint Override After a Deep Clean](#setpoint-override-after-a-deep-clean). `setpoint=None` keeps whatever the device restores by itself
- `set_temperature_unit(TemperatureUnit)` -- set device display unit: `"C"` or `"F"`. Raises `DeviceOffError` if the device is off
- `address` -- BLE device address
- `register_connection_event_callback(fn)` -- register a connectivity event callback, returns an unsubscribe function

#### Sleep schedule methods

- `read_sleep_schedule()` -- read the schedule from the device (updates cache)
- `set_sleep_schedule(nights)` -- write a structured schedule (list of `SleepScheduleNight`)
- `set_sleep_schedule_events(events)` -- write a flat event list directly
- `clear_sleep_schedule()` -- clear the schedule on the device
- `sync_clock(now=None)` -- sync the device's internal clock (used for schedule execution). Pass a timezone-aware datetime, or omit to use the system timezone.
- `sleep_schedule` -- cached `OolerSleepSchedule` (or `None` if not yet read)
- `sleep_schedule_events` -- cached schedule as a flat `list[SleepScheduleEvent]`

### `OolerBLEState`

Dataclass with fields: `power`, `mode`, `set_temperature`, `actual_temperature`, `water_level`, `clean`, `temperature_unit`.

### Sleep Schedule Types

- `OolerSleepSchedule` -- weekly schedule containing a list of `SleepScheduleNight` and a sequence counter
- `SleepScheduleNight` -- one night's program: day (0=Mon), temperature steps, off time, optional warm wake
- `SleepScheduleEvent` -- a single event in the flat wire format (minute of week + temperature). Minute of week is minutes elapsed since Monday 00:00 (e.g. Tuesday 6:00am = 1800).
- `WarmWake` -- warm wake configuration: target temperature and duration in minutes
- `build_sleep_schedule(bedtime, wake_time, temp_f, ...)` -- convenience builder for uniform schedules (same program across selected days, with optional warm wake and extra temperature steps)

### Connection Events

- `ConnectionEvent` -- a connectivity event with `type`, `timestamp`, and optional `detail`
- `ConnectionEventType` -- enum: `CONNECTED`, `DISCONNECTED`, `SUBSCRIPTION_MISMATCH`, `SUBSCRIPTION_RECOVERED`, `FORCED_RECONNECT`, `SETPOINT_OVERRIDE_FIXED`, `SETPOINT_OVERRIDE_UNFIXABLE`
  - `SETPOINT_OVERRIDE_FIXED` -- detail `{"overrode": int, "overrode_with": int, "restored": int | None, "attempt": int}`. Worth logging; needs no attention. The fix briefly runs the pump and moves the setpoint to 75.
  - `SETPOINT_OVERRIDE_UNFIXABLE` -- detail `{"attempts": int}`. Every duration was tried and none held; the user's temperature is being discarded and nothing will correct it.

### Other Types

- `OolerMode` -- `Literal["Silent", "Regular", "Boost"]`
- `TemperatureUnit` -- `Literal["C", "F"]`
- `OolerConnectionError` -- raised when all retry attempts are exhausted (inherits from `BleakError`)
- `DeviceOffError` -- raised by `set_temperature`, `set_mode` and `set_temperature_unit` when the device is off. It drops writes while powered down, so these refuse rather than record a value it does not hold. Turn the device on first; `set_clean` is the exception and powers it on, since a clean physically requires it

## Concurrency & Reconnection

### Connection serialization

All connection attempts are serialized through an internal `asyncio.Lock`. If `connect()` is called while another connection is already in progress, the second caller waits for the first to complete and then returns immediately if the connection succeeded. This prevents duplicate connections and race conditions.

### Two-level retry

GATT write operations use a two-level retry strategy:

1. **Immediate retry** -- if a write fails with a transient BLE error (e.g., ESP32 proxy hiccup), the operation is retried immediately without reconnecting.
2. **Reconnect + retry** -- if the immediate retry also fails, the library forces a full disconnect/reconnect cycle (with a 0.5s backoff) and retries the operation once more.

If all three attempts fail, an `OolerConnectionError` is raised.

`async_poll()` uses a similar pattern: if the poll fails, it reconnects and retries once.

### Handled exception types

The library catches `BleakError`, `EOFError`, `BrokenPipeError`, and `asyncio.TimeoutError` during GATT operations. These cover the common failure modes seen with ESP32 BLE proxies.

### Disconnect handling

When the BLE connection drops unexpectedly, the internal client reference is cleared immediately so `is_connected` returns `False`. Registered callbacks are fired to notify consumers of the state change. The library does not automatically reconnect -- the consumer (e.g., a Home Assistant integration) is responsible for triggering reconnection on the next advertisement or poll cycle.

## ESP32 BLE Proxy Considerations

### Notification slots

ESP32 BLE proxies (ESPHome) have a global limit of 12 notification registrations across all connected devices. This library subscribes to 4 notification characteristics per device:

- Power, Mode, Set Temperature, Actual Temperature

Water level and clean status are **polled** (via `async_poll()`) rather than subscribed to notifications. This means two Ooler devices use 8 of 12 available slots, leaving headroom for other BLE devices.

### Connection slots

ESP32 proxies support 3 simultaneous BLE connections by default. Each Ooler device holds one connection slot for as long as it's connected.

## Temperature Behavior

The Ooler has a quirk in how it handles temperature units:

- **Set temperature** (`SETTEMP_CHAR`) is always stored and reported in **Fahrenheit** by the device, regardless of the display unit setting.
- **Actual temperature** (`ACTUALTEMP_CHAR`) is reported in whatever unit the device display is set to.

The library handles this automatically:
- `state.set_temperature` is converted to the current display unit on read.
- `set_temperature(value)` accepts a value in the current display unit and converts to Fahrenheit before writing to the device.
- `state.actual_temperature` is passed through as-is from the device.

The display unit is read once on connect and cached. It can be changed via `set_temperature_unit()`.

## Setpoint Override After a Deep Clean

On firmware 15.20, **a deep clean run to completion makes the device override the setpoint every time it is powered off**, replacing it with the value stored when the clean ran. Anything set afterwards is silently discarded. Users see this as the Ooler resetting their temperature.

**Starting a clean and cancelling it before it completes clears the condition.** A clean left to finish never can, because finishing ends by powering the device off and so never sends `CLEAN=0` — which is why running more deep cleans cannot fix what a deep clean caused.

Reproduced in both directions on two devices, triggered from Home Assistant, the vendor's app and this library, so it is device firmware rather than any client's doing.

The client handles it without any work from the consumer:

1. Every power-off starts a watch (`SETPOINT_OVERRIDE_WATCH_SECONDS`).
2. A setpoint change while the device stays off can only be the device itself — it drops writes while off, its buttons cannot change the setpoint while off, and it accepts one connection at a time.
3. `fix_setpoint_override()` runs, and `SETPOINT_OVERRIDE_FIXED` is emitted.

**The power-off that ends a clean is excluded from step 1.** A clean forces the setpoint to 75 and the device reverts that whenever the clean ends, so a revert straight after a clean is expected behaviour, not evidence — and it looks identical whether the clean ran to completion (which arms the override) or was aborted by powering the unit off (which does not). Watching it would run a fix clean on devices that were never armed. Nothing is lost: an armed device overrides on *every* power-off, so the next ordinary one catches it.

Cancelling makes the device restore its own stored setpoint. The client overwrites that only if a person actually asked for something — on the first override after a clean, the reported value is the clean's forced 75 while the device's stored value is the pre-clean setpoint, which is the better answer.

Attempts use successively longer clean durations (`FIX_CLEAN_SECONDS`, 0s/3s/30s) rather than repeating one that just failed. Running out emits `SETPOINT_OVERRIDE_UNFIXABLE` and stops, rather than running the pump after every power-off forever.

The override lands 3s to 60s after power-off with no pattern we can find, so any manual check needs minutes, not seconds.

**Known gap:** a clean completing while nothing is connected is not noticed, so the device stays overridden until something connects and the symptom shows on a later power-off. Every clean is started by some client — the vendor's app, Home Assistant, this library — but none of them has to stay connected for it to run to completion, so the power-off that ends it can land with nothing listening. Reachable with nobody present: the app starts a clean and walks away, or our connection drops partway through. The consequence is a delayed fix, not a missed one.

## License

Apache-2.0
