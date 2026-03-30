# Changelog

## 0.9.0

First stable release. Complete rewrite of connection management and error handling.

### Added
- `set_temperature_unit()` -- read and write the device's display temperature unit (Celsius/Fahrenheit)
- `OolerConnectionError` -- raised when all retry attempts are exhausted (inherits from `BleakError`)
- `OolerMode` and `TemperatureUnit` Literal types for type safety
- `py.typed` marker for PEP 561 compliance
- Two-level GATT retry: immediate retry for transient errors, full reconnect for stale connections
- Broader exception handling: catches `BleakError`, `EOFError`, `BrokenPipeError`, `asyncio.TimeoutError`
- Notification change detection: callbacks only fire when state actually changes
- Input validation on `set_mode()`, `set_temperature()`, `set_temperature_unit()`
- Temperature range validation (55-115 F)
- 238 tests with 100% code coverage

### Changed
- Switched to `BleakClientWithServiceCache` for automatic GATT cache clearing on errors
- Reduced notification subscriptions from 6 to 4 per device (water level and clean are polled instead)
- Temperature unit is read once on connect instead of every poll
- `set_power(True)` now re-sends mode and temperature as a single atomic operation (no recursive setter calls)
- `_disconnected_callback` clears `_client` immediately so `is_connected` returns `False` right away
- `max_attempts=5` for `establish_connection` (improved ESP32 proxy resilience)
- 0.5s backoff in forced reconnect to let BLE stack clean up
- `async_poll()` uses keyword arguments for `OolerBLEState` construction
- Modernized `pyproject.toml` to PEP 621 `[project]` format
- Minimum Python version raised to `>=3.11`

### Fixed
- **Shared state across instances** -- class-level mutable attributes (`_state`, `_connect_lock`, `_callbacks`, `_client`) moved to `__init__` as instance variables
- **Infinite recursion in setters** -- `set_power`, `set_mode`, `set_temperature`, `set_clean` now raise `RuntimeError` if connection fails instead of calling themselves forever
- **`is_connected` side effect** -- no longer mutates `state.connected`, now a pure property
- **Partial notification subscription** -- if `start_notify` fails mid-setup, the connection is torn down cleanly instead of left half-initialized
- **`_ble_device` not initialized** -- prevents `AttributeError` if accessed before `set_ble_device()`
- **Notification handler exceptions** -- caught and logged instead of being silently swallowed by bleak
- **`_execute_disconnect` partial cleanup** -- each `stop_notify` call is individually guarded so one failure doesn't skip the rest

### Removed
- `state.connected` field -- use `client.is_connected` instead
- `test_connection()` function -- replaced by `connect()` + `async_poll()`
- `advertisement.py` -- Ooler doesn't include manufacturer data in advertisements
- `check_connection.py`, `pair.py`, `setup.py` -- dead code
- `DISCONNECT_DELAY` / disconnect timer -- was always 0 (dead code)
- Wildcard imports, unused `TypeVar`

## 0.7.1

Previous release (before rewrite).
