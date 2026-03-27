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

- `set_ble_device(device)` — set the BLE device to connect to
- `connect()` — establish BLE connection, read initial state, subscribe to notifications
- `stop()` — unsubscribe from notifications and disconnect
- `is_connected` — whether the device is currently connected
- `state` — current `OolerBLEState`
- `register_callback(fn)` — register a state change callback, returns an unsubscribe function
- `async_poll()` — read all characteristics from the device
- `set_power(bool)` — turn device on/off (re-sends mode and temperature on power-on)
- `set_mode(OolerMode)` — set pump mode: `"Silent"`, `"Regular"`, or `"Boost"`
- `set_temperature(int)` — set target temperature in the current display unit
- `set_clean(bool)` — start/stop clean cycle (automatically powers on)
- `set_temperature_unit(TemperatureUnit)` — set device display unit: `"C"` or `"F"`

### `OolerBLEState`

Dataclass with fields: `power`, `mode`, `set_temperature`, `actual_temperature`, `water_level`, `clean`, `temperature_unit`.

### Types

- `OolerMode` — `Literal["Silent", "Regular", "Boost"]`
- `TemperatureUnit` — `Literal["C", "F"]`
- `OolerConnectionError` — raised when all retry attempts are exhausted (inherits from `BleakError`)

## License

Apache-2.0
