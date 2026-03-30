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
- `set_mode(OolerMode)` -- set pump mode: `"Silent"`, `"Regular"`, or `"Boost"`
- `set_temperature(int)` -- set target temperature in the current display unit
- `set_clean(bool)` -- start/stop clean cycle (automatically powers on)
- `set_temperature_unit(TemperatureUnit)` -- set device display unit: `"C"` or `"F"`

### `OolerBLEState`

Dataclass with fields: `power`, `mode`, `set_temperature`, `actual_temperature`, `water_level`, `clean`, `temperature_unit`.

### Types

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

## License

Apache-2.0
