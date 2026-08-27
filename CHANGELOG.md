# Changelog

## 1.1.0b1

Beta. The setpoint-restore repair has not yet been exercised against real
hardware through the automatic path -- see Unverified below.

### Added
- **Stuck-setpoint bug detection and repair.** A deep clean run to completion
  commits the device's current setpoint to non-volatile storage and makes the
  device restore it on every subsequent power-off, silently discarding anything
  set afterwards. Reproduced in both directions on two devices (firmware 15.20),
  armed via Home Assistant on one and the official app on the other, so it is
  firmware behaviour and not client-specific.
  - `clear_stuck_setpoint_bug(setpoint=None)` -- starts a clean and cancels it,
    which is the only thing that clears the state. A clean left to finish never
    can: it ends by powering the device off and so never sends `CLEAN=0`.
    Optionally leaves the device at a given temperature, and restores the power
    state it found.
  - Every power-off starts a watch. A setpoint change while the device stays off
    can only be the device's own doing, since it drops writes while off and its
    single-connection limit means nothing else can be writing.
  - `ConnectionEventType.STUCK_SETPOINT_REPAIRED` -- detail carries `wanted` and
    `stuck_at`. Emitted so the brief pump run and setpoint blip are explained.
  - Repairs back off rather than repeating a duration that just failed:
    `CLEAN_TOGGLE_SECONDS` is a schedule, `(3.0, 10.0, 30.0)`, and each attempt
    takes the next entry. Only 20s has been proven by hand, so the cheap value
    is tried first and the proven range is the fallback.
  - `ConnectionEventType.STUCK_SETPOINT_UNFIXABLE` -- detail carries
    `consecutive`. Emitted once every duration has been tried and none held, at
    which point the client stops running the pump to no effect.
  - `OolerBLEDevice(model, auto_clear_stuck_setpoint_bug=False)` disables the
    repair while leaving detection and events intact.
- `diagnostics/capture.py` -- labelled snapshot of every Ooler in range, diffed
  against the previous capture with sensor noise separated from real changes.

### Fixed
- `set_clean(False)` no longer powers the device on. The power-on branch ran for
  stopping as well as starting, so telling an idle device to stop cleaning woke
  it and resent mode and temperature. The device drops writes while off, so it
  now skips and warns, matching `set_temperature_unit`.

### Changed
- `SUB_FIRMWARE_CHAR` renamed to `UNKNOWN_9A5F_CHAR`. It is not a firmware
  version: it moves with use, and fell from `"1024.80"` to `"1.x"` across a
  factory reset and a long power loss. A runtime-proportional model was tried and
  falsified -- `RUNTIME` and `LIFETIME` both *decrease* across an abrupt power
  cut, so they are not monotonic.
- `UNKNOWN_9234` documentation corrected. Its previous annotation linked it to a
  temperature revert bug; that was coincidence. It is read/notify only and
  fluctuates between reads on an untouched device.
- `DEVICE_LOGS` record format documented in `const.py`: 6-byte
  `(code, param, ts)` records where `ts` is an age in seconds, with opcodes
  confirmed by controlled writes. Reading the log drains it, so the client does
  not read it; this is for debugging only.

### Verified on hardware
- The full chain: a library-initiated deep clean run to completion armed a
  device (it substituted the setpoint 20s after power-off), a 3s
  `clear_stuck_setpoint_bug()` repaired it, and the setpoint then held for 10
  minutes. The value the device substitutes is the setpoint that was live during
  the clean.
- A full deep clean takes ~45 minutes, so escalating retries span days of
  ordinary use rather than minutes.

### Unverified
- `_watch_for_stuck_setpoint` -- the automatic trigger -- has not run against
  hardware; the repair it calls has. Detection is unit-tested only.
- Repeated deep cleans with no ordinary use between them can trip the give-up
  counter, since each legitimately re-arms the device. It self-heals on the next
  normal power-off.

## 0.11.1

### Changed
- **Replaced the notification-staleness watchdog with a poll/state consistency detector.** The 0.11.0 watchdog watched for absence of notifications and force-reconnected after a 15-minute silence. Overnight soak (2026-04-12/13) revealed that during "coast" periods (at setpoint, pump off, ACTUALTEMP genuinely stable) all four subscribed characteristics legitimately go silent for 15+ minutes, producing 30 spurious forced reconnects in a 15.5h window that cascaded at exact 15-minute intervals. The new detector instead compares every successful `async_poll()` against cached state on the four notify-backed fields (power, mode, set_temperature, actual_temperature). A disagreement is positive evidence that a notification was missed, and the recovery ladder re-subscribes in place (Tier 1, `stop_notify` + `start_notify` on the existing client) and only escalates to a full forced reconnect if the next poll still shows a mismatch (Tier 2). The detector runs inside `async_poll` with no background task, no tunable threshold, and zero false positives during coast.
- `ConnectionEventType`: removed `NOTIFY_STALL`. Added `SUBSCRIPTION_MISMATCH` (detail includes sorted `fields` list) and `SUBSCRIPTION_RECOVERED`. `FORCED_RECONNECT` gains a new `trigger` value, `"subscription_mismatch"`, emitted on Tier 2 escalation.

### Removed
- `_NOTIFY_STALL_TIMEOUT_SECONDS`, `_WATCHDOG_TICK_SECONDS`, `_WATCHDOG_RECONNECT_COOLDOWN_SECONDS` constants
- `_notify_watchdog_loop`, `_watchdog_tick`, `_cancel_watchdog` methods
- `_last_notification_monotonic`, `_watchdog_task`, `_force_reconnect_cooldown_until` instance state
- `_watchdog_enabled_default` class attribute and the `_disable_notify_watchdog` autouse test fixture

## 0.11.0

### Added
- **Notification-staleness watchdog** -- background task that forces a reconnect when the notification stream has been silent for longer than 15 minutes while the device is powered. Addresses silent 37-249 minute notify stalls observed on ESPHome BLE proxies where reads kept succeeding but the subscription state had been lost during a proxy-internal reconnect. (Superseded in 0.11.1 by the poll/state consistency detector.)
- **Connection-event channel** -- new `register_connection_event_callback()` API delivering `ConnectionEvent` instances on connect, unexpected disconnect, notify stall, and forced reconnect. Independent of the existing state callback.
  - `ConnectionEventType` -- enum: `CONNECTED`, `DISCONNECTED`, `NOTIFY_STALL`, `FORCED_RECONNECT` (the `NOTIFY_STALL` variant was removed in 0.11.1)
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
