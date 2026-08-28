# Changelog

## 1.1.0b7

Supersedes all earlier 1.1.0 betas. Use this one.

### Fixed
- **The clean's forced setpoint was still recorded as the user's choice.**
  `set_clean` updated `state.clean` *after* awaiting the write, and the device
  answers during that await — trial 2 logged its forced-setpoint notification in
  the same millisecond as the write, ahead of the next statement. So the handler
  ran with the flag still false and recorded 75 as a person's choice; the fix
  then wrote 75 back and discarded the user's 85. Same harm as the original
  finding C. The flag is now set before the write.

  b5 removed the previous guard on the reasoning that `state.clean` could not be
  stale because the device forces its temperature "a couple of seconds later."
  That figure came from a poll observation, not notification timing. There is no
  margin — the reply lands inside the await.

### Verified on hardware (trial 2)
- **The 0s first attempt is sufficient.** ~222ms between the `CLEAN=1` and
  `CLEAN=0` writes cleared the override, confirmed independently by a power-off
  that held 203s afterwards. Staying first in `FIX_CLEAN_SECONDS`.
- The transition gating works: the clean's own power-off armed the watch.
- Override delay 40.1s, against 26.0s in trial 1 — both inside the documented
  3–60s range.

### Still unverified
- The 10s and 30s attempts, and `SETPOINT_OVERRIDE_UNFIXABLE`. Now unreachable
  in ordinary testing, since 0s succeeds first; exercising them needs deliberate
  fault injection.

## 1.1.0b6

### Fixed
- **Only a genuine on-to-off transition starts the override watch.** Any
  notification reporting power off did, including one repeating a state already
  held -- the device re-announcing itself, or a reconnection after another
  client had the device. Those are not the device being turned off, and
  watching them risks reading a stale cached setpoint as an override and
  powering the unit on to "fix" it.

  The transition is read from what the device has reported, not from
  `state.power`. The setters update that optimistically, so by the time a
  commanded power-off is notified the cached value already says off -- reading
  it there would have missed every power-off made through a consumer, which is
  the normal case. A poll keeps the same baseline current, since `connect()`
  learns the power state by polling rather than by notification.

## 1.1.0b5

### Fixed
- **Removed the `CLEAN_TEMP_F` exclusion added in b4.** It guarded a state that
  cannot occur. A clean can only be started by a connected client: either this
  one, and `set_clean` sets `state.clean` before the device forces its own
  temperature a couple of seconds later, or another client, which holds the
  device's single connection so nothing reaches us. `connect()` polls before
  subscribing, so the flag is fresh before any notification arrives.

  The exclusion had a real cost in exchange: a person choosing exactly 75F was
  never recorded as having chosen anything, and in Celsius that is 24C — an
  ordinary setting. So b4 traded a bug that could not happen for one that could.

## 1.1.0b4

### Fixed
- Excluded `CLEAN_TEMP_F` from being recorded as a user setpoint, on the belief
  that a clean started elsewhere could set it while `state.clean` was stale.
  **Withdrawn in b5** — that state is unreachable, and the exclusion discarded
  genuine 75F/24C choices.
- **The fix could clear its own recovery flag without stopping the clean.** Its
  cancel went through `set_clean`, which skips the write when it believes the
  device is off — right for external callers, wrong here, since the clean is
  ours and the device was powered on to start it. A skipped write cleared the
  flag while the clean was still running, and a clean that finishes causes the
  very override the fix was clearing. The cancel is now written directly, so a
  failure raises and the flag survives for `connect()` to act on.

## 1.1.0b3

Supersedes 1.1.0b1 and 1.1.0b2, which write the wrong temperature on the most
common path. Use this instead.

### Added
- **Setpoint override detection and fix.** A deep clean run to completion makes
  the device override the setpoint every time it is powered off, silently
  discarding anything set afterwards. Reproduced in both directions on two
  devices, triggered from Home Assistant, the vendor's app and this library, so
  it is device firmware rather than any client's doing.
  - `fix_setpoint_override(setpoint=None, clean_seconds=None)` -- starts a clean
    and cancels it, the only thing that clears the condition. Cancelling makes
    the device restore its own stored setpoint; `setpoint` overwrites that, and
    `None` keeps it.
  - Every power-off starts a watch. A setpoint change while the device stays off
    can only be the device itself: it drops writes while off, its buttons cannot
    change the setpoint while off, and it accepts one connection at a time.
  - `ConnectionEventType.SETPOINT_OVERRIDE_FIXED` -- detail carries `overrode`,
    `overrode_with`, `restored` and `attempt`. For logging; needs no attention.
  - `ConnectionEventType.SETPOINT_OVERRIDE_UNFIXABLE` -- detail carries
    `attempts`. Needs a person: the temperature is being discarded and nothing
    will correct it.
  - Attempts use successively longer clean durations (`FIX_CLEAN_SECONDS`,
    0s/3s/30s) rather than repeating one that just failed. 3s is confirmed on
    hardware; trying nothing first means the usual case costs one round trip.
- `diagnostics/capture.py` -- labelled snapshot of every Ooler in range, diffed
  against the previous capture with sensor noise separated from real changes.

### Fixed
- `set_clean(False)` no longer powers the device on. That branch ran for stopping
  as well as starting, so telling an idle device to stop cleaning woke it and
  resent mode and temperature.
- A fix never leaves a clean running. A clean that finishes causes the very
  override the fix was clearing, plus a 45-minute cycle; if the cancel cannot
  land, the next connection stops it.
- The fix restores what a person asked for, not the clean's forced 75. On the
  first override after a clean the reported setpoint is the clean's artifact, so
  earlier betas wrote that back and destroyed the user's setting.
- The fix fires state callbacks when it finishes. It moves power and setpoint
  unasked, and the setters update cached state without notifying.

### Changed
- `SUB_FIRMWARE_CHAR` renamed to `UNKNOWN_9A5F_CHAR`. It is not a firmware
  version: it moves with use, and a runtime-proportional model was falsified --
  `RUNTIME` and `LIFETIME` both *decrease* across an abrupt power cut, so they
  are not monotonic.
- `UNKNOWN_9234` documentation corrected; its previous annotation linked it to a
  temperature revert bug, which was coincidence.
- `DEVICE_LOGS` record format documented in `const.py`. Reading it drains it, so
  the client does not; this is for debugging only.

### Known gaps
- A clean completing while nothing is connected is not noticed, so the device
  stays overridden until something connects and the symptom shows on a later
  power-off. A delayed fix, not a missed one.
- Only the 3s clean duration is confirmed on hardware; 0s and 30s are untried.
- The watch itself has not run against hardware; the fix it calls has.

## 1.1.0b2

Field-trial fixes from the first real detect-repair-recover cycle on hardware.

### Changed
- **A completed deep clean is now repaired at once, rather than waiting for the
  bug to bite.** A clean run to completion is known to arm it, and after the
  restore-the-user's-setpoint fix below the first revert is invisible -- the
  firmware puts back the pre-clean setpoint, which is what the user wanted -- so
  the bug does not surface until their next temperature change. Waiting
  therefore costs a setting. A 3s pump cycle immediately after a 45-minute clean
  is also about the least surprising moment for one.

  The observed-symptom watch is unchanged and remains the guarantee: `CLEAN` is
  polled rather than notified, so a clean can be missed, and nothing breaks when
  it is.
- `STUCK_SETPOINT_DETECTED` detail gains `trigger`, either `"clean_completed"`
  (repaired pre-emptively; `stuck_at` is `None`) or `"observed"` (the device was
  caught substituting; `stuck_at` is the value it used).

### Fixed
- **The repair restored the clean's forced temperature instead of the user's.**
  A deep clean holds the setpoint at `CLEAN_TEMP_F` (75) for its duration, and
  the power-off that ends the clean is both what arms the bug and what triggers
  the first detection -- so the last *reported* setpoint at that moment is the
  clean's artifact, not anything the user chose. Field trial: user set 85, the
  clean forced 75, the firmware reverted to 85, and the repair wrote 75 and
  reported `repaired: True`. The user's setting was destroyed by the repair on
  the path a real user hits first.

  The client now tracks the setpoint that was actually asked for -- by
  `set_temperature()`, or observed while the device is running and not cleaning
  -- and restores that. Change detection still uses the reported value, since
  those are two different jobs.
- **Library-initiated changes were invisible to consumers.** The setters update
  cached state optimistically, so the device's own notification finds nothing
  changed and fires no callback. Consumers asking for a change refresh anyway,
  but the repair changes power and setpoint without being asked; a field trial
  saw an entity stay stale for 36s until something else polled. The repair now
  fires state callbacks when it finishes.

- **`STUCK_SETPOINT_RECOVERED` could claim an unverified fix.** After the
  post-clean repair the setpoint equals the value the device substitutes (they
  are the same number by construction on that path), so a repair that did not
  take would have left exactly that value and the confirmation window could not
  tell success from failure. It now stays quiet when the observation was
  incapable of showing a failure; the next power-off at a different setpoint
  settles it.

### Notes
- The field trial also confirmed what the firmware commits: the **pre-clean**
  setpoint (85), not the clean's forced 75 and not a sentinel. And the 3s tier
  cleared it on the first attempt (3.14s toggle).

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
  - Repairs back off rather than repeating a duration that just failed:
    `CLEAN_TOGGLE_SECONDS` is a schedule, `(3.0, 10.0, 30.0)`, and each attempt
    takes the next entry. 3s is confirmed on hardware; the longer entries are
    headroom in case some device needs more.
  - `ConnectionEventType.STUCK_SETPOINT_DETECTED` -- detail carries `wanted`,
    `stuck_at` and `repaired`. One event for the occurrence; the flag says
    whether the client acted, so the opt-out path reports what it saw without
    claiming to have fixed anything.
  - `ConnectionEventType.STUCK_SETPOINT_RECOVERED` -- detail carries `after`.
    Fires when a setpoint survives a full watch window following an earlier
    repair, on the transition only. Gives consumers an edge to clear anything
    they raised on `STUCK_SETPOINT_UNFIXABLE`.
  - `ConnectionEventType.STUCK_SETPOINT_UNFIXABLE` -- detail carries
    `consecutive`. Emitted once every duration has been tried and none held, at
    which point the client stops running the pump to no effect.
  - `OolerBLEDevice(model, auto_clear_stuck_setpoint_bug=False)` disables the
    repair while leaving detection and events intact.
- `diagnostics/capture.py` -- labelled snapshot of every Ooler in range, diffed
  against the previous capture with sensor noise separated from real changes.

### Fixed
- With `auto_clear_stuck_setpoint_bug=False`, the give-up counter no longer
  climbs and `STUCK_SETPOINT_UNFIXABLE` can no longer fire. It previously
  reported every clean duration as tried when none had been attempted, and
  claimed a repair that never happened.
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
