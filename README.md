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

### `OolerBLEDevice(model: str, auto_clear_stuck_setpoint_bug: bool = True)`

Main client class. `auto_clear_stuck_setpoint_bug` controls whether the client repairs a device it catches substituting its own setpoint after a power-off -- see [Stuck Setpoint Bug](#stuck-setpoint-bug). Pass `False` to be told about it via connection events and handle it yourself.

- `set_ble_device(device)` -- set the BLE device to connect to
- `connect()` -- establish BLE connection, read initial state, subscribe to notifications
- `stop()` -- unsubscribe from notifications and disconnect
- `is_connected` -- whether the device is currently connected
- `state` -- current `OolerBLEState`
- `register_callback(fn)` -- register a state change callback, returns an unsubscribe function
- `async_poll()` -- read all characteristics from the device
- `set_power(bool)` -- turn device on/off (re-sends mode and temperature on power-on)
- `set_mode(OolerMode)` -- set pump mode: `"Silent"`, `"Regular"`, or `"Boost"`
- `set_temperature(int)` -- set target temperature in the current display unit
- `set_clean(bool)` -- start/stop clean cycle. Starting one powers the device on; stopping one does not, since the device drops writes while off
- `clear_stuck_setpoint_bug(setpoint=None, seconds=None)` -- work around the firmware bug described in [Stuck Setpoint Bug](#stuck-setpoint-bug). Optionally leaves the device at a given temperature
- `set_temperature_unit(TemperatureUnit)` -- set device display unit: `"C"` or `"F"`
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
- `ConnectionEventType` -- enum: `CONNECTED`, `DISCONNECTED`, `SUBSCRIPTION_MISMATCH`, `SUBSCRIPTION_RECOVERED`, `FORCED_RECONNECT`, `STUCK_SETPOINT_DETECTED`, `STUCK_SETPOINT_UNFIXABLE`, `STUCK_SETPOINT_RECOVERED`
  - `STUCK_SETPOINT_DETECTED` -- detail `{"trigger": str, "wanted": int, "stuck_at": int | None, "repaired": bool}`. `trigger` is `"clean_completed"` (a clean finished, which arms the bug; repaired pre-emptively, `stuck_at` is `None`) or `"observed"` (the device was caught substituting). `repaired` says whether the client acted. When true the repair briefly ran the pump and moved the setpoint, so surfacing this keeps that from looking like a glitch.
  - `STUCK_SETPOINT_UNFIXABLE` -- detail `{"consecutive": int}`. Every clean duration was tried and none held; the setpoint really is being discarded and nothing will correct it. Re-fires on each subsequent stuck power-off, so raising the same issue repeatedly is idempotent.
  - `STUCK_SETPOINT_RECOVERED` -- detail `{"after": int}`. A setpoint survived a full window off after an earlier repair. Fires on the transition only, so anything raised on `STUCK_SETPOINT_UNFIXABLE` has an edge to clear on. Note it also follows an ordinary successful repair about a watch window later, so the healthy path is `DETECTED{repaired: True}` then `RECOVERED{after: 1}` -- treat clearing as idempotent. It stays quiet when the observation could not have shown a failure, i.e. when the setpoint already equals the value the device substitutes.

### Other Types

- `OolerMode` -- `Literal["Silent", "Regular", "Boost"]`
- `TemperatureUnit` -- `Literal["C", "F"]`
- `OolerConnectionError` -- raised when all retry attempts are exhausted (inherits from `BleakError`)

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

## Stuck Setpoint Bug

On firmware 15.20, **a deep clean run to completion commits the device's current setpoint to non-volatile storage and makes the device restore it on every subsequent power-off.** Any temperature set afterwards is silently discarded a few seconds after the unit is turned off. Users see this as the Ooler resetting their temperature.

**Starting a clean and cancelling it before it completes clears the state.** A clean left to finish never can, because completion ends by powering the device off and so never sends `CLEAN=0` -- which is why running more deep cleans cannot fix what a deep clean caused.

This was reproduced in both directions on two devices, armed once through Home Assistant and once through the official app, so it is device firmware rather than client behaviour.

By default the client handles it without any work from the consumer:

1. A completed deep clean is repaired at once, since it is known to arm the bug.
2. Otherwise, every power-off starts a watch.
3. A setpoint change while the device stays off can only be the device's own doing -- it drops writes while off, and its single-connection limit means nothing else can be writing.
4. The repair is applied, the setpoint the user asked for is restored, and `STUCK_SETPOINT_DETECTED` is emitted with `repaired: True`.

Repairs back off rather than repeating a duration that just failed: `CLEAN_TOGGLE_SECONDS` is `(3.0, 10.0, 30.0)` and each attempt takes the next entry. Running out emits `STUCK_SETPOINT_UNFIXABLE` and stops, rather than running the pump after every power-off forever.

Note the revert delay is highly variable -- 3s to 60s observed -- so any manual check needs minutes, not seconds.

## License

Apache-2.0
