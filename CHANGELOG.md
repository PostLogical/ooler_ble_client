# Changelog

## 0.11.0

### Added
- **Notification-staleness watchdog** -- background task that forces a reconnect when the notification stream has been silent for longer than 15 minutes while the device is powered. Addresses silent 37-249 minute notify stalls observed on ESPHome BLE proxies where reads kept succeeding but the subscription state had been lost during a proxy-internal reconnect.
- **Connection-event channel** -- new `register_connection_event_callback()` API delivering `ConnectionEvent` instances on connect, unexpected disconnect, notify stall, and forced reconnect. Independent of the existing state callback.
  - `ConnectionEventType` -- enum: `CONNECTED`, `DISCONNECTED`, `NOTIFY_STALL`, `FORCED_RECONNECT`
  - `ConnectionEvent` -- frozen dataclass with `type`, `timestamp` (monotonic), and `detail` payload
  - `NOTIFY_STALL` detail includes `stall_duration_seconds`
  - `FORCED_RECONNECT` detail includes `trigger` (`"notify_stall"`, `"poll_failure"`, or `"write_failure"`)
- **Flap suppression** -- `is_connected` now returns `True` throughout a forced-reconnect window so consumers (e.g. the Home Assistant coordinator) do not race the library's reconnect with their own. If the forced reconnect fails, the flag clears and the normal unexpected-disconnect path takes over.
- **"Bluetooth is already shutdown" backoff** -- `establish_connection` is wrapped with an outer retry loop (3 attempts, 20s backoff) that recognises the specific `BleakError` substring and spans the ~15s proxy blip instead of burning 5 inner attempts in ~2 seconds.
- 26 new tests covering watchdog behavior, event channel, flap suppression, and shutdown backoff (373 total)

### Changed
- `decode_sleep_schedule_events()` signature widened to `bytes | bytearray` to match what `BleakClient.read_gatt_char` actually returns (mypy `--strict` now clean)

## 0.10.0

### Added
- **Sleep schedule support** -- read, write, and clear the device's weekly sleep schedule
  - `SleepScheduleEvent` -- low-level wire-format event (minute-of-week + temperature)
  - `SleepScheduleNight` -- structured night with temperature zones and per-night warm wake
  - `OolerSleepSchedule` -- full weekly schedule as a list of nights
  - `WarmWake` -- warm wake configuration (target temp + duration)
  - `build_sleep_schedule()` -- convenience builder for uniform app-compatible schedules
- `read_sleep_schedule()` -- read schedule from device (lazy, not on every connect)
- `set_sleep_schedule()` -- write a structured schedule
- `set_sleep_schedule_events()` -- write raw events for full control
- `clear_sleep_schedule()` -- clear the device schedule
- `sync_clock()` -- sync the device's internal clock with proper DST handling via `zoneinfo`
- Schedule format fully decoded and documented in `sleep_schedule.py` and `const.py`
- 347 tests with 100% code coverage

### Fixed
- Schedule service GATT write quirk: device byte-swaps uint16 values on write; client pre-swaps to compensate

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
