from __future__ import annotations

import asyncio
import struct
import time
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any, ClassVar

from bleak.backends.device import BLEDevice
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BLEAK_RETRY_EXCEPTIONS,
    establish_connection,
)

from .models import (
    ConnectionEvent,
    ConnectionEventType,
    OolerBLEState,
    OolerConnectionError,
    OolerMode,
    TemperatureUnit,
)
from .const import (
    _LOGGER,
    MODE_INT_TO_MODE_STATE,
    POWER_CHAR,
    MODE_CHAR,
    SETTEMP_CHAR,
    ACTUALTEMP_CHAR,
    WATER_LEVEL_CHAR,
    CLEAN_CHAR,
    DISPLAY_TEMPERATURE_UNIT_CHAR,
    SCHEDULE_HEADER_CHAR,
    SCHEDULE_TIMES_CHAR,
    SCHEDULE_TEMPS_CHAR,
    CURRENT_TIME_CHAR,
    LOCAL_TIME_INFO_CHAR,
    TEMP_LO_F,
    TEMP_MIN_F,
    TEMP_MAX_F,
    TEMP_HI_F,
    _SHUTDOWN_ERROR_BACKOFF_SECONDS,
    _SHUTDOWN_ERROR_MAX_ATTEMPTS,
)
from .sleep_schedule import (
    OolerSleepSchedule,
    SleepScheduleEvent,
    SleepScheduleNight,
    decode_sleep_schedule_events,
    encode_sleep_schedule_events,
    events_to_sleep_schedule,
    sleep_schedule_to_events,
)

_RECONNECT_BACKOFF_SECONDS = 0.5


def _byteswap_uint16s(data: bytes) -> bytes:
    """Swap each pair of bytes in a buffer.

    The Ooler firmware byte-swaps uint16 values on GATT writes to the
    schedule service.  To compensate, we pre-swap so the device stores
    the intended little-endian values.
    """
    buf = bytearray(data)
    for i in range(0, len(buf) - 1, 2):
        buf[i], buf[i + 1] = buf[i + 1], buf[i]
    return bytes(buf)


def _f_to_c(f: int) -> int:
    """Convert Fahrenheit to Celsius (rounded)."""
    return round((f - 32) * 5 / 9)


def _c_to_f(c: int) -> int:
    """Convert Celsius to Fahrenheit (rounded)."""
    return round(c * 9 / 5 + 32)


def _is_valid_temp_f(temp_f: int) -> bool:
    """Check if a Fahrenheit temperature is valid for the Ooler.

    Valid values: TEMP_LO_F (45), 54-116, TEMP_HI_F (120).
    The device clamps 46-54 to LO (45) and 116-119 to HI (120), so
    54 and 116 are accepted as the integration's way to request LO/HI.
    Values below 54 (except 45) or above 116 (except 120) are rejected.
    """
    return temp_f in (TEMP_LO_F, TEMP_HI_F) or 54 <= temp_f <= 116


class OolerBLEDevice:

    def __init__(self, model: str) -> None:
        """Initialize the OolerBLEDevice."""
        self._model_id = model
        self._state = OolerBLEState()
        self._connect_lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._callbacks: list[Callable[[OolerBLEState], None]] = []
        self._connection_event_callbacks: list[
            Callable[[ConnectionEvent], None]
        ] = []
        self._ble_device: BLEDevice | None = None
        self._expected_disconnect = False
        self._sleep_schedule: OolerSleepSchedule | None = None
        self._sleep_schedule_events: list[SleepScheduleEvent] = []
        self._sleep_schedule_seq: int = 0
        self._force_reconnecting: bool = False
        # Poll/state consistency detector: skip the check on the first
        # poll of any new connection (no meaningful cached baseline),
        # then arm it for subsequent polls.
        self._consistency_check_armed: bool = False
        # Tier-1 re-subscribe was attempted and we are waiting for the
        # next poll to confirm whether the mismatch cleared.
        self._tier1_pending: bool = False
        # Indirection so tests can inject a fake monotonic clock without
        # globally patching time.monotonic.
        self._monotonic: Callable[[], float] = time.monotonic

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Set the BLE Device."""
        self._ble_device = ble_device

    @property
    def is_connected(self) -> bool:
        """Return whether the device is connected.

        During a forced reconnect (library-internal), this continues to
        return True so downstream consumers (e.g. the HA coordinator)
        don't flap entities unavailable while the library heals the
        connection. If the forced reconnect fails, the flag is cleared
        and the normal unexpected-disconnect path fires a
        ``DISCONNECTED`` connection event.
        """
        if self._force_reconnecting:
            return True
        return self._client is not None and self._client.is_connected

    @property
    def address(self) -> str:
        """Return the address."""
        if self._ble_device is None:
            raise RuntimeError("BLE device not set — call set_ble_device() first")
        return self._ble_device.address

    @property
    def state(self) -> OolerBLEState:
        """Return the state."""
        return self._state

    @property
    def sleep_schedule(self) -> OolerSleepSchedule | None:
        """Return the cached sleep schedule, or None if not yet read."""
        return self._sleep_schedule

    @property
    def sleep_schedule_events(self) -> list[SleepScheduleEvent]:
        """Return the cached sleep schedule as a flat event list."""
        return self._sleep_schedule_events

    async def connect(self) -> None:
        await self._ensure_connected()

    async def stop(self) -> None:
        """Stop the client."""
        _LOGGER.debug("%s: Stop", self._model_id)
        await self._execute_disconnect()

    def _set_state_and_fire_callbacks(self, state: OolerBLEState) -> None:
        if self._state != state:
            self._state = state
            self._fire_callbacks()

    def _fire_callbacks(self) -> None:
        """Fire the callbacks."""
        for callback in self._callbacks:
            callback(self._state)

    def register_callback(
        self, callback: Callable[[OolerBLEState], None]
    ) -> Callable[[], None]:
        """Register a callback to be called when the state changes."""

        def unregister_callback() -> None:
            self._callbacks.remove(callback)

        self._callbacks.append(callback)
        return unregister_callback

    def register_connection_event_callback(
        self, callback: Callable[[ConnectionEvent], None]
    ) -> Callable[[], None]:
        """Register a callback invoked on connectivity events.

        Delivers :class:`ConnectionEvent` instances when the device
        connects, disconnects unexpectedly, a poll reveals a missed
        notification (subscription mismatch), a re-subscribe recovers
        the subscription, or a forced reconnect is initiated. The event
        channel is independent of the state callback registered via
        :meth:`register_callback`.

        Returns a zero-arg function that unregisters the callback.
        Multiple callbacks are supported and are invoked with per-
        callback exception isolation.
        """

        def unregister() -> None:
            try:
                self._connection_event_callbacks.remove(callback)
            except ValueError:
                pass

        self._connection_event_callbacks.append(callback)
        return unregister

    def _fire_connection_event(
        self,
        event_type: ConnectionEventType,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Build and dispatch a :class:`ConnectionEvent` to subscribers."""
        if not self._connection_event_callbacks:
            return
        event = ConnectionEvent(
            type=event_type,
            timestamp=self._monotonic(),
            detail=detail,
        )
        for callback in list(self._connection_event_callbacks):
            try:
                callback(event)
            except Exception:
                _LOGGER.warning(
                    "%s: Connection-event callback raised, ignoring",
                    self._model_id,
                    exc_info=True,
                )

    async def _ensure_connected(self) -> None:
        """Ensure connection to device is established."""
        # Don't treat ``self._force_reconnecting`` as connected: the
        # forced-reconnect path itself calls _ensure_connected and must
        # bypass the is_connected shortcut.
        if self._connect_lock.locked():
            _LOGGER.debug(
                "%s: Connection already in progress, waiting for it to complete",
                self._model_id,
            )
        if self._client is not None and self._client.is_connected:
            return
        async with self._connect_lock:
            # Check again while holding the lock
            if self._client is not None and self._client.is_connected:
                return
            ble_device = self._ble_device
            if ble_device is None:
                raise RuntimeError("BLE device not set — call set_ble_device() first")
            _LOGGER.debug("%s: Connecting", self._model_id)
            client = await self._establish_with_shutdown_backoff(ble_device)
            _LOGGER.debug("%s: Connected", self._model_id)
            self._client = client
            # The internal poll below establishes the post-(re)connect
            # baseline. Any pre-disconnect cached state is not a valid
            # comparison target, so disarm the consistency detector
            # until that baseline is in place.
            self._consistency_check_armed = False
            self._tier1_pending = False
            try:
                # Read temperature unit once on connect (rarely changes)
                temp_unit_byte = await client.read_gatt_char(DISPLAY_TEMPERATURE_UNIT_CHAR)
                self._state.temperature_unit = (
                    "C" if int.from_bytes(temp_unit_byte, "little") == 1 else "F"
                )
                _LOGGER.debug("%s: Attempt to retrieve initial state.", self._model_id)
                await self.async_poll()
                _LOGGER.debug("%s: Subscribe to notifications", self._model_id)
                # Only subscribe to 4 notifications to stay within ESP32 proxy limits
                # (12 global notification slots). Water level and clean are polled instead.
                await client.start_notify(POWER_CHAR, self._notification_handler)
                await client.start_notify(MODE_CHAR, self._notification_handler)
                await client.start_notify(SETTEMP_CHAR, self._notification_handler)
                await client.start_notify(ACTUALTEMP_CHAR, self._notification_handler)
            except Exception:
                _LOGGER.warning(
                    "%s: Failed during post-connect setup, disconnecting",
                    self._model_id,
                    exc_info=True,
                )
                self._client = None
                await client.disconnect()
                raise
        self._fire_connection_event(ConnectionEventType.CONNECTED)

    async def _establish_with_shutdown_backoff(
        self, ble_device: BLEDevice
    ) -> BleakClientWithServiceCache:
        """Wrap establish_connection with outer retries for "already shutdown".

        ``bleak_retry_connector.establish_connection`` burns its 5
        inner attempts in ~2 seconds when the BlueZ/proxy adapter is
        transiently unavailable (``BleakError("Bluetooth is already
        shutdown")``). The proxy blip typically lasts ~15s, so sleep
        ~20s between outer attempts to span the blip.
        """
        last_err: BleakError | None = None
        for attempt in range(_SHUTDOWN_ERROR_MAX_ATTEMPTS):
            try:
                return await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self._model_id,
                    self._disconnected_callback,
                    max_attempts=5,
                    use_services_cache=True,
                    ble_device_callback=lambda: self._ble_device or ble_device,
                )
            except BleakError as err:
                if "Bluetooth is already shutdown" not in str(err):
                    raise
                last_err = err
                if attempt == _SHUTDOWN_ERROR_MAX_ATTEMPTS - 1:
                    break
                _LOGGER.warning(
                    "%s: establish_connection hit 'Bluetooth is already shutdown'"
                    " (attempt %d/%d), backing off %ds",
                    self._model_id,
                    attempt + 1,
                    _SHUTDOWN_ERROR_MAX_ATTEMPTS,
                    _SHUTDOWN_ERROR_BACKOFF_SECONDS,
                )
                await asyncio.sleep(_SHUTDOWN_ERROR_BACKOFF_SECONDS)
        assert last_err is not None
        raise last_err

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle notification responses."""
        try:
            uuid = _sender.uuid
            _LOGGER.debug(
                "%s: Notification received: %s from %s",
                self._model_id,
                data.hex(),
                uuid,
            )
            changed = False
            if uuid == POWER_CHAR:
                power = bool(int.from_bytes(data, "little"))
                if self._state.power != power:
                    self._state.power = power
                    changed = True
                # OFF ends clean. Similarly, when clean mode ends, the device only sends OFF.
                if not power and self._state.clean:
                    self._state.clean = False
                    changed = True
            elif uuid == MODE_CHAR:
                mode_int = int.from_bytes(data, "little")
                if 0 <= mode_int < len(MODE_INT_TO_MODE_STATE):
                    mode = MODE_INT_TO_MODE_STATE[mode_int]
                    if self._state.mode != mode:
                        self._state.mode = mode
                        changed = True
                else:
                    _LOGGER.warning(
                        "%s: Unknown mode value: %s", self._model_id, mode_int
                    )
                    return
            elif uuid == SETTEMP_CHAR:
                # SETTEMP_CHAR always reports in Fahrenheit
                settemp_f = int.from_bytes(data, "little")
                set_temperature = (
                    _f_to_c(settemp_f)
                    if self._state.temperature_unit == "C"
                    else settemp_f
                )
                if self._state.set_temperature != set_temperature:
                    self._state.set_temperature = set_temperature
                    changed = True
            elif uuid == ACTUALTEMP_CHAR:
                actualtemp_int = int.from_bytes(data, "little")
                if self._state.actual_temperature != actualtemp_int:
                    self._state.actual_temperature = actualtemp_int
                    changed = True
            if changed:
                self._fire_callbacks()
        except Exception:
            _LOGGER.warning(
                "%s: Error handling notification from %s",
                self._model_id,
                _sender.uuid,
                exc_info=True,
            )

    async def _read_all_characteristics(self) -> OolerBLEState:
        """Read all GATT characteristics and return a new state."""
        client = self._client
        if client is None:
            raise BleakError("Not connected")

        power_byte = await client.read_gatt_char(POWER_CHAR)
        mode_byte = await client.read_gatt_char(MODE_CHAR)
        settemp_byte = await client.read_gatt_char(SETTEMP_CHAR)
        actualtemp_byte = await client.read_gatt_char(ACTUALTEMP_CHAR)
        waterlevel_byte = await client.read_gatt_char(WATER_LEVEL_CHAR)
        clean_byte = await client.read_gatt_char(CLEAN_CHAR)

        power = bool(int.from_bytes(power_byte, "little"))
        mode_int = int.from_bytes(mode_byte, "little")
        if 0 <= mode_int < len(MODE_INT_TO_MODE_STATE):
            mode = MODE_INT_TO_MODE_STATE[mode_int]
        else:
            _LOGGER.warning(
                "%s: Unknown mode value during poll: %s", self._model_id, mode_int
            )
            mode = self._state.mode or "Regular"
        # SETTEMP_CHAR is always in Fahrenheit regardless of display unit.
        # ACTUALTEMP_CHAR is in whatever the display unit is set to.
        settemp_f = int.from_bytes(settemp_byte, "little")
        actualtemp_int = int.from_bytes(actualtemp_byte, "little")
        waterlevel_int = int.from_bytes(waterlevel_byte, "little")
        clean = bool(int.from_bytes(clean_byte, "little"))

        # Use cached temperature_unit (read once on connect)
        temperature_unit = self._state.temperature_unit or "F"
        set_temperature = _f_to_c(settemp_f) if temperature_unit == "C" else settemp_f

        return OolerBLEState(
            power=power,
            mode=mode,
            set_temperature=set_temperature,
            actual_temperature=actualtemp_int,
            water_level=waterlevel_int,
            clean=clean,
            temperature_unit=temperature_unit,
        )

    async def async_poll(self) -> None:
        """Retrieve state from device.

        After a successful GATT read, compares the fresh values against
        cached state on the four notify-backed fields. A disagreement
        is positive evidence that a notification was missed, so the
        recovery ladder in :meth:`_handle_subscription_mismatch` is
        invoked — Tier 1 re-subscribes in place, Tier 2 forces a full
        reconnect if a prior re-subscribe did not clear the mismatch.
        """
        if self._client is None:
            return await self.connect()

        try:
            state = await self._read_all_characteristics()
        except BLEAK_RETRY_EXCEPTIONS:
            if self._connect_lock.locked():
                raise  # Called from _ensure_connected, let its handler deal with it
            _LOGGER.warning(
                "%s: Poll failed, attempting reconnect", self._model_id
            )
            await self._execute_forced_reconnect(trigger="poll_failure")
            try:
                state = await self._read_all_characteristics()
            except BLEAK_RETRY_EXCEPTIONS as err:
                raise OolerConnectionError(
                    f"{self._model_id}: Poll failed after reconnect: {err}"
                ) from err

        if self._consistency_check_armed:
            missed_fields = self._check_notify_consistency(state)
            if missed_fields:
                refreshed = await self._handle_subscription_mismatch(missed_fields)
                if refreshed:
                    # Tier 2 ran: _execute_forced_reconnect → _ensure_connected
                    # → an inner async_poll already applied fresh state.
                    # Our pre-reconnect `state` value is now stale.
                    return
            else:
                # Clean poll — any pending Tier 1 proved itself out.
                self._tier1_pending = False

        self._set_state_and_fire_callbacks(state)
        self._consistency_check_armed = True
        _LOGGER.debug("%s: State retrieved.", self._model_id)

    _NOTIFY_BACKED_FIELDS: ClassVar[tuple[str, ...]] = (
        "power",
        "mode",
        "set_temperature",
        "actual_temperature",
    )

    def _check_notify_consistency(self, fresh: OolerBLEState) -> set[str]:
        """Return field names where fresh poll values disagree with cached state.

        Fields that are ``None`` in the cache are skipped — there is no
        meaningful baseline to compare against (e.g., first poll after
        instantiation before any connect).
        """
        missed: set[str] = set()
        cached = self._state
        for field in self._NOTIFY_BACKED_FIELDS:
            cached_val = getattr(cached, field)
            if cached_val is None:
                continue
            if cached_val != getattr(fresh, field):
                missed.add(field)
        return missed

    async def _handle_subscription_mismatch(
        self, missed_fields: set[str]
    ) -> bool:
        """Recover from a poll/state mismatch indicating missed notifications.

        Returns ``True`` if the recovery path refreshed cached state
        itself (Tier 2 — a forced reconnect whose inner poll already
        applied fresh values). In that case the caller should NOT apply
        its own pre-reconnect poll result.

        Tier 1 (re-subscribe in place) returns ``False`` — the caller
        still applies the fresh poll values it just read, which become
        the new cached baseline for the next poll's consistency check.
        """
        sorted_fields = sorted(missed_fields)
        self._fire_connection_event(
            ConnectionEventType.SUBSCRIPTION_MISMATCH,
            detail={"fields": sorted_fields},
        )
        _LOGGER.warning(
            "%s: Poll revealed missed notification(s) on %s; subscription suspect",
            self._model_id,
            sorted_fields,
        )

        if self._tier1_pending:
            _LOGGER.warning(
                "%s: Re-subscribe did not resolve mismatch; forcing reconnect",
                self._model_id,
            )
            try:
                await self._execute_forced_reconnect(
                    trigger="subscription_mismatch"
                )
            except Exception:
                _LOGGER.warning(
                    "%s: Forced reconnect from subscription mismatch failed",
                    self._model_id,
                    exc_info=True,
                )
                return False
            return True

        client = self._client
        if client is None or not client.is_connected:
            # Nothing to re-subscribe on. Leave _tier1_pending unset so
            # the next poll still treats this as a fresh mismatch.
            return False

        try:
            for char in (POWER_CHAR, MODE_CHAR, SETTEMP_CHAR, ACTUALTEMP_CHAR):
                try:
                    await client.stop_notify(char)
                except Exception:
                    _LOGGER.debug(
                        "%s: stop_notify(%s) raised during re-subscribe, ignoring",
                        self._model_id,
                        char,
                    )
                await client.start_notify(char, self._notification_handler)
        except Exception:
            _LOGGER.warning(
                "%s: Re-subscribe failed, escalating to forced reconnect",
                self._model_id,
                exc_info=True,
            )
            try:
                await self._execute_forced_reconnect(
                    trigger="subscription_mismatch"
                )
            except Exception:
                _LOGGER.warning(
                    "%s: Forced reconnect from failed re-subscribe also failed",
                    self._model_id,
                    exc_info=True,
                )
                return False
            return True

        self._tier1_pending = True
        self._fire_connection_event(ConnectionEventType.SUBSCRIPTION_RECOVERED)
        _LOGGER.info(
            "%s: Re-subscribed to notification characteristics; "
            "next poll will confirm",
            self._model_id,
        )
        return False

    async def _retry_on_stale(self, operation: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        """Execute a GATT operation with two levels of retry.

        First retry: immediate (handles transient proxy hiccups).
        Second retry: full reconnect (handles stale connections).
        """
        try:
            return await operation()
        except BLEAK_RETRY_EXCEPTIONS:
            _LOGGER.debug(
                "%s: GATT operation failed, retrying immediately", self._model_id
            )
        # First retry: immediate, no reconnect
        try:
            return await operation()
        except BLEAK_RETRY_EXCEPTIONS as err:
            _LOGGER.warning(
                "%s: GATT operation failed twice (%s), reconnecting",
                self._model_id,
                err,
            )
        # Second retry: full reconnect
        await self._execute_forced_reconnect(trigger="write_failure")
        try:
            return await operation()
        except BLEAK_RETRY_EXCEPTIONS as err:
            raise OolerConnectionError(
                f"{self._model_id}: Operation failed after reconnect: {err}"
            ) from err

    async def _execute_forced_reconnect(self, trigger: str = "unknown") -> None:
        """Force disconnect and reconnect.

        ``trigger`` is propagated to subscribers via a
        :class:`ConnectionEventType.FORCED_RECONNECT` event and is one of
        ``"notify_stall"``, ``"poll_failure"``, ``"write_failure"``.

        While a forced reconnect is in flight, :pyattr:`is_connected`
        continues to report ``True`` so downstream consumers do not
        flap entities unavailable. If the reconnect itself fails, the
        flag is cleared before re-raising so the normal unexpected-
        disconnect path takes over.
        """
        _LOGGER.debug("%s: Forcing reconnect (trigger=%s)", self._model_id, trigger)
        self._force_reconnecting = True
        self._expected_disconnect = True
        # Disarm the consistency detector so the post-reconnect internal
        # poll in _ensure_connected doesn't flag pre-reconnect cache
        # against post-reconnect device state.
        self._consistency_check_armed = False
        self._tier1_pending = False
        self._fire_connection_event(
            ConnectionEventType.FORCED_RECONNECT,
            detail={"trigger": trigger},
        )
        client = self._client
        self._client = None
        if client:
            try:
                await client.disconnect()
            except Exception:
                _LOGGER.debug(
                    "%s: Disconnect during forced reconnect failed, ignoring",
                    self._model_id,
                )
        # Brief delay to let the BLE stack clean up before reconnecting
        await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
        try:
            await self._ensure_connected()
        except Exception:
            # Clear flap suppression so the normal unexpected-disconnect
            # path (and any downstream unavailability logic) takes over.
            self._force_reconnecting = False
            raise
        self._force_reconnecting = False

    async def _write_gatt(self, char: str, data: bytes) -> None:
        """Write to a GATT characteristic with retry-on-stale logic."""

        async def _write() -> None:
            client = self._client
            if client is None:
                raise BleakError("Not connected")
            await client.write_gatt_char(char, data, True)

        await self._retry_on_stale(_write)

    async def set_power(self, power: bool) -> None:
        """Turn the device on or off. Re-sends mode and temperature on power-on."""
        if self._client is None:
            await self.connect()
        if self._client is None:
            raise RuntimeError("Failed to connect to device")
        power_byte = int(power).to_bytes(1, "little")
        await self._write_gatt(POWER_CHAR, power_byte)
        _LOGGER.debug("Set power to %s.", power)
        self._state.power = power

        # When turning on, re-send mode and temperature to the device.
        # These may have been changed in HA while the Ooler was off, and the
        # device won't pick them up unless they're written after power-on.
        # Write directly to GATT here instead of calling set_mode/set_temperature
        # to keep this as a single atomic operation.
        if power and self._state.mode is not None:
            mode_int = MODE_INT_TO_MODE_STATE.index(self._state.mode)
            await self._write_gatt(MODE_CHAR, mode_int.to_bytes(1, "little"))
        if power and self._state.set_temperature is not None:
            settemp_f = (
                _c_to_f(self._state.set_temperature)
                if self._state.temperature_unit == "C"
                else self._state.set_temperature
            )
            await self._write_gatt(SETTEMP_CHAR, settemp_f.to_bytes(1, "little"))

    async def set_mode(self, mode: OolerMode) -> None:
        """Set pump mode: 'Silent', 'Regular', or 'Boost'.

        If the device is off, the value is cached in state and will be
        sent to the device on the next set_power(True) call.
        """
        if mode not in MODE_INT_TO_MODE_STATE:
            raise ValueError(
                f"Invalid mode '{mode}'. Must be one of: {MODE_INT_TO_MODE_STATE}"
            )
        if self._client is None:
            await self.connect()
        if self._client is None:
            raise RuntimeError("Failed to connect to device")
        mode_int = MODE_INT_TO_MODE_STATE.index(mode)
        if self._state.power:
            await self._write_gatt(MODE_CHAR, mode_int.to_bytes(1, "little"))
        else:
            _LOGGER.debug(
                "Device is off; mode cached and will be sent on power-on."
            )
        _LOGGER.debug("Set mode to %s.", mode)
        self._state.mode = mode

    async def set_temperature(self, settemp_int: int) -> None:
        """Set target temperature. Value should be in the current display unit.

        If the device is off, the value is cached in state and will be
        sent to the device on the next set_power(True) call.
        """
        if self._client is None:
            await self.connect()
        if self._client is None:
            raise RuntimeError("Failed to connect to device")
        # SETTEMP_CHAR always expects Fahrenheit — convert if display unit is C
        settemp_f = (
            _c_to_f(settemp_int)
            if self._state.temperature_unit == "C"
            else settemp_int
        )
        if not _is_valid_temp_f(settemp_f):
            raise ValueError(
                f"Temperature {settemp_int} (={settemp_f}°F) out of range. "
                f"Valid: {TEMP_LO_F} (LO), 54-116, or {TEMP_HI_F} (HI)"
            )
        if self._state.power:
            await self._write_gatt(SETTEMP_CHAR, settemp_f.to_bytes(1, "little"))
        else:
            _LOGGER.debug(
                "Device is off; temperature cached and will be sent on power-on."
            )
        _LOGGER.debug(
            "Set temperature to %s (wrote %s°F to device).", settemp_int, settemp_f
        )
        self._state.set_temperature = settemp_int

    async def set_clean(self, clean: bool) -> None:
        """Start or stop a clean cycle. Automatically powers on the device."""
        if self._client is None:
            await self.connect()
        if self._client is None:
            raise RuntimeError("Failed to connect to device")
        # Power on first — clean requires the device to be running.
        if not self._state.power:
            await self.set_power(True)
        await self._write_gatt(CLEAN_CHAR, int(clean).to_bytes(1, "little"))
        _LOGGER.debug("Set clean to %s.", clean)
        self._state.clean = clean

    async def set_temperature_unit(self, unit: TemperatureUnit) -> None:
        """Set device display unit: 'C' or 'F'.

        Unlike mode and temperature, there is no resend-on-power-on for this
        setting, so it is only written when the device is on. If the device
        is off, a warning is logged and the write is skipped.
        """
        if unit not in ("C", "F"):
            raise ValueError(f"Invalid temperature unit '{unit}'. Must be 'C' or 'F'")
        if self._client is None:
            await self.connect()
        if self._client is None:
            raise RuntimeError("Failed to connect to device")
        if not self._state.power:
            _LOGGER.warning(
                "Device is off; display unit write skipped (device drops writes when off)."
            )
            return
        unit_byte = (1 if unit == "C" else 0).to_bytes(1, "little")
        await self._write_gatt(DISPLAY_TEMPERATURE_UNIT_CHAR, unit_byte)
        _LOGGER.debug("Set temperature unit to %s.", unit)
        self._state.temperature_unit = unit

    async def _read_sleep_schedule_chars(
        self, client: BleakClientWithServiceCache
    ) -> None:
        """Read schedule characteristics and cache the parsed result."""
        header_bytes = await client.read_gatt_char(SCHEDULE_HEADER_CHAR)
        times_bytes = await client.read_gatt_char(SCHEDULE_TIMES_CHAR)
        temps_bytes = await client.read_gatt_char(SCHEDULE_TEMPS_CHAR)

        self._sleep_schedule_seq = int.from_bytes(header_bytes, "little")
        self._sleep_schedule_events = decode_sleep_schedule_events(
            times_bytes, temps_bytes
        )
        self._sleep_schedule = events_to_sleep_schedule(
            self._sleep_schedule_events, self._sleep_schedule_seq
        )
        _LOGGER.debug(
            "%s: Sleep schedule read (%d events, seq=%d)",
            self._model_id,
            len(self._sleep_schedule_events),
            self._sleep_schedule_seq,
        )

    async def read_sleep_schedule(self) -> OolerSleepSchedule:
        """Read the sleep schedule from the device.

        This re-reads the schedule characteristics and updates the cache.
        """
        if self._client is None:
            await self.connect()
        client = self._client
        if client is None:
            raise RuntimeError("Failed to connect to device")

        async def _read() -> None:
            await self._read_sleep_schedule_chars(client)

        await self._retry_on_stale(_read)
        assert self._sleep_schedule is not None
        return self._sleep_schedule

    async def set_sleep_schedule(
        self, nights: list[SleepScheduleNight]
    ) -> None:
        """Write a structured sleep schedule to the device.

        Encodes the nights into wire format and writes to the device.
        Schedules with per-night variation (different warm wake settings,
        different temperature zones per night) work correctly with the
        device but may not display or edit correctly in the Ooler app.
        """
        schedule = OolerSleepSchedule(nights=nights, seq=self._sleep_schedule_seq)
        events = sleep_schedule_to_events(schedule)
        await self.set_sleep_schedule_events(events)

    async def set_sleep_schedule_events(
        self, events: list[SleepScheduleEvent]
    ) -> None:
        """Write a flat event list to the device as the sleep schedule."""
        if self._client is None:
            await self.connect()
        if self._client is None:
            raise RuntimeError("Failed to connect to device")

        times_bytes, temps_bytes = encode_sleep_schedule_events(events)
        new_seq = self._sleep_schedule_seq + 1
        # The device byte-swaps uint16 values on write, so we pre-swap
        # times (array of uint16) and the header (single uint16).
        # Temps are single bytes and don't need swapping.
        header_wire = _byteswap_uint16s(new_seq.to_bytes(2, "little"))
        times_wire = _byteswap_uint16s(times_bytes)

        await self._write_gatt(SCHEDULE_TIMES_CHAR, times_wire)
        await self._write_gatt(SCHEDULE_TEMPS_CHAR, temps_bytes)
        await self._write_gatt(SCHEDULE_HEADER_CHAR, header_wire)

        self._sleep_schedule_seq = new_seq
        self._sleep_schedule_events = events
        self._sleep_schedule = events_to_sleep_schedule(events, new_seq)
        _LOGGER.debug(
            "%s: Sleep schedule written (%d events, seq=%d)",
            self._model_id,
            len(events),
            new_seq,
        )

    async def clear_sleep_schedule(self) -> None:
        """Clear the sleep schedule on the device."""
        await self.set_sleep_schedule_events([])

    async def sync_clock(self, now: datetime | None = None) -> None:
        """Sync the device clock to the current time.

        The Ooler has an internal clock used for schedule execution.  The
        official app sets this clock on every connection.  This method
        writes the standard BLE Current Time and Local Time Info
        characteristics so the device stays in sync.

        For correct DST handling, pass a datetime with a ``zoneinfo``
        timezone (e.g. ``datetime.now(ZoneInfo("America/New_York"))``).
        In Home Assistant, use ``ZoneInfo(hass.config.time_zone)``.

        If *now* is omitted, the method uses the system's IANA timezone
        via ``zoneinfo`` when available, falling back to a fixed-offset
        timezone with DST marked as unknown.
        """
        if self._client is None:
            await self.connect()
        if self._client is None:
            raise RuntimeError("Failed to connect to device")

        if now is None:
            now = _local_now()
        if now.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")

        # BLE Current Time characteristic (org.bluetooth.characteristic.current_time)
        # 10 bytes: year(u16 LE), month, day, hour, min, sec, dow(1=Mon), frac256, adjust
        dow = now.isoweekday()  # 1=Mon, 7=Sun (matches BLE spec)
        current_time = struct.pack(
            "<HBBBBBBB",
            now.year,
            now.month,
            now.day,
            now.hour,
            now.minute,
            now.second,
            dow,
            0,  # fractions (1/256s)
        ) + b"\x01"  # adjust reason: manual time update

        # BLE Local Time Info (org.bluetooth.characteristic.local_time_information)
        # 2 bytes: tz_offset (int8, units of 15 min), dst_offset (uint8)
        utc_offset = now.utcoffset()
        if utc_offset is None:
            raise ValueError("datetime must have a UTC offset")
        total_minutes = int(utc_offset.total_seconds() / 60)

        # DST: the BLE spec wants the base timezone and DST component
        # separately.  A proper IANA timezone (via zoneinfo) reports DST
        # correctly through .dst().  Fixed-offset timezones return None,
        # in which case we mark DST as unknown (0xFF per BLE spec).
        dst_offset = now.dst()
        if dst_offset is not None:
            dst_minutes = int(dst_offset.total_seconds() / 60)
            tz_minutes = total_minutes - dst_minutes
            # BLE DST encoding: 0=standard, 4=+1h, 8=+2h, 12=+0.5h, 255=unknown
            _DST_MAP = {0: 0, 30: 12, 60: 4, 120: 8}
            dst_byte = _DST_MAP.get(dst_minutes, 255)
        else:
            # Can't determine DST — use total offset and mark unknown
            tz_minutes = total_minutes
            dst_byte = 255

        tz_offset_15 = tz_minutes // 15  # signed int8
        local_time_info = struct.pack("bB", tz_offset_15, dst_byte)

        await self._write_gatt(CURRENT_TIME_CHAR, current_time)
        await self._write_gatt(LOCAL_TIME_INFO_CHAR, local_time_info)

        _LOGGER.debug(
            "%s: Clock synced to %s (UTC%+.1f, DST=%d)",
            self._model_id,
            now.strftime("%Y-%m-%d %H:%M:%S"),
            total_minutes / 60,
            dst_byte,
        )


    def _disconnected_callback(self, client: BleakClientWithServiceCache | None) -> None:
        """Disconnected callback."""
        # Clear client immediately so is_connected returns False,
        # allowing the integration's BLE callback to trigger reconnection.
        self._client = None
        expected = self._expected_disconnect
        force_reconnecting = self._force_reconnecting
        self._expected_disconnect = False
        if expected:
            _LOGGER.debug("%s: Expected disconnect from device", self._model_id)
        else:
            _LOGGER.warning(
                "%s: Unexpectedly disconnected from device", self._model_id
            )
        if force_reconnecting:
            # The forced-reconnect path owns the connection-event channel
            # for its window. Suppress the state-callback fire too so the
            # coordinator does not see a transient is_connected=False.
            return
        self._fire_callbacks()
        if not expected:
            self._fire_connection_event(ConnectionEventType.DISCONNECTED)

    async def _execute_disconnect(self) -> None:
        """Execute disconnection."""
        async with self._connect_lock:
            client = self._client
            self._expected_disconnect = True
            self._client = None
            if client and client.is_connected:
                for char in (POWER_CHAR, MODE_CHAR, SETTEMP_CHAR, ACTUALTEMP_CHAR):
                    try:
                        await client.stop_notify(char)
                    except Exception:
                        _LOGGER.debug(
                            "%s: Failed to unsubscribe from %s, ignoring",
                            self._model_id,
                            char,
                        )
                await client.disconnect()

def _local_now() -> datetime:
    """Get the current local time with the best available timezone info.

    Prefers ``zoneinfo`` for proper DST detection.  Falls back to a
    fixed-offset timezone (DST will be marked unknown).
    """
    try:
        import os

        from zoneinfo import ZoneInfo

        # Try TZ environment variable first, then /etc/localtime symlink
        tz_name = os.environ.get("TZ")
        if not tz_name:
            try:
                link = os.readlink("/etc/localtime")
                if "zoneinfo/" in link:
                    tz_name = link.split("zoneinfo/", 1)[1]
            except OSError:
                pass
        if tz_name:
            return datetime.now(ZoneInfo(tz_name))
    except Exception:
        pass
    # Fallback: fixed-offset timezone (DST unknown)
    return datetime.now(timezone.utc).astimezone()
