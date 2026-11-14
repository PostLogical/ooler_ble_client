from __future__ import annotations

import asyncio
import dataclasses
import logging
import struct
import time
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any, TypeVar

_T = TypeVar("_T")

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
    DeviceOffError,
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
    CLEAN_TEMP_F,
    UV_CLEAN_MAX_SECONDS,
    LIFETIME_CHAR,
    RUNTIME_CHAR,
    UV_RUNTIME_CHAR,
    FIX_CLEAN_SECONDS,
    SETPOINT_OVERRIDE_WATCH_SECONDS,
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
    WATER_LEVEL_RANGE,
    ACTUAL_TEMP_RANGE_F,
    ACTUAL_TEMP_RANGE_C,
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


def _hex(data: bytes | bytearray) -> str:
    """Render a GATT response for logging, distinguishing an empty one.

    Accepts bytearray as well as bytes, matching what ``read_gatt_char``
    actually returns.
    """
    return data.hex() or "<empty>"


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
        # Setpoint override after a deep clean; see
        # _watch_for_setpoint_override and CLEAN_TEMP_F in const.py.
        #
        # What a person last asked for, which is not the same as what the
        # device currently reports: a clean forces the setpoint to CLEAN_TEMP_F
        # for its duration, and the override replaces it outright. Exists so a
        # fix can put back the right value.
        self._last_user_setpoint: int | None = None
        # Power as the device last reported it, which is not state.power: the
        # setters set that optimistically, so it cannot show a transition.
        self._last_notified_power: bool | None = None
        self._setpoint_override_watch: asyncio.Task[None] | None = None
        self._override_fixes_tried: int = 0
        # A clean this client started for a fix and has not yet stopped.
        self._fix_started_clean: bool = False
        # When the CLEAN characteristic was first seen asserted in the current
        # run of assertions, for the UV_CLEAN_MAX_SECONDS backstop. None while
        # the bit reads clear.
        self._clean_asserted_since: float | None = None
        # The current run of CLEAN-asserted-while-off has been warned about.
        # It can latch for hours, and a warning on every poll says nothing new.
        self._warned_clean_while_off: bool = False
        # A deep clean this client asked for, from the write until it stops.
        # Provenance is deliberately NOT part of what deep_clean means -- a
        # clean started in the vendor's app has to be recognised too, and that
        # is what the CLEAN_TEMP_F discriminator is for. This exists only to
        # win a race the discriminator cannot: the device's forced-setpoint
        # notification arrives ~2s after our write, and a poll may not have
        # happened yet, so without this the handler would record the clean's
        # own temperature as the user's choice.
        self._deep_clean_pending: bool = False

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
                if self._fix_started_clean:
                    if self._state.power:
                        # A fix lost the link before stopping its clean. Letting
                        # it finish would cause the override the fix was
                        # clearing.
                        _LOGGER.warning(
                            "%s: Stopping a clean left running by an interrupted"
                            " fix.",
                            self._model_id,
                        )
                        await self.set_deep_clean(False)
                    else:
                        # A clean that ends by powering the device off is over.
                        # CLEAN=0 cannot land and is not needed -- and raising
                        # DeviceOffError here would fail the whole connect.
                        _LOGGER.warning(
                            "%s: A fix's clean ended with the device off.",
                            self._model_id,
                        )
                    self._fix_started_clean = False
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
                # Read the transition from what the device has told us, not from
                # state.power: the setters update that optimistically, so by the
                # time a commanded power-off is notified the cached value
                # already says off and the transition would be invisible.
                was_on = self._last_notified_power
                is_on = bool(int.from_bytes(data, "little"))
                self._last_notified_power = is_on
                if self._state.power != is_on:
                    self._state.power = is_on
                    changed = True
                # OFF ends a deep clean. When one ends of its own accord the
                # device only sends OFF, never CLEAN=0, and CLEAN is polled
                # rather than notified -- so without this the flag would stand
                # until the next poll.
                ended_a_clean = not is_on and self._state.deep_clean
                if ended_a_clean:
                    self._state.deep_clean = False
                    self._deep_clean_pending = False
                    changed = True
                # The UV cycle runs only while the unit does. Same reasoning:
                # polled, not notified, so it is cleared here rather than up to
                # a poll interval later.
                if not is_on and self._state.uv_clean:
                    self._state.uv_clean = False
                    changed = True
                # Watch only a real on -> off transition. A notification
                # repeating a state we already hold is not the device being
                # turned off -- it is the device re-announcing itself, or a
                # reconnection after another client had it -- and watching
                # those risks reading a stale cached setpoint as an override.
                #
                # A power-off that ends a deep clean is excluded too. It forces
                # the setpoint to CLEAN_TEMP_F and the device reverts that when
                # the clean ends, so a revert here is expected behaviour rather
                # than evidence -- and it looks the same whether the clean ran to
                # completion, which arms the override, or was aborted by powering
                # the unit off, which does not. Watching it would run a fix clean
                # on devices that were never armed. An armed device overrides on
                # every power-off, so the next ordinary one still catches it.
                if was_on and not is_on and not ended_a_clean and (
                    self._setpoint_override_watch is None
                    or self._setpoint_override_watch.done()
                ):
                    self._setpoint_override_watch = asyncio.create_task(
                        self._watch_for_setpoint_override()
                    )
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
                    forced_by_clean = (
                        settemp_f == CLEAN_TEMP_F
                        and (self._state.deep_clean or self._deep_clean_pending)
                    )
                    if self._state.power and not forced_by_clean:
                        # A person asked for this. The device changes the
                        # setpoint by itself only while deep cleaning or while
                        # off.
                        #
                        # Keyed on the VALUE, not on the clean flag alone. The
                        # device asserts CLEAN for its own hourly UV cycle
                        # without touching the setpoint, and suppressing
                        # capture through those 240 seconds every hour would
                        # silently lose a temperature the user did ask for.
                        self._last_user_setpoint = set_temperature
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

    async def _read_and_apply_all(self) -> set[str]:
        """Read every polled characteristic, applying each to state as it lands.

        Returns the notify-backed fields whose reading disagreed with cached
        state, which is empty unless the consistency check is armed.

        Each reading is compared and applied before the next read is awaited.
        The notification handler runs at every await, so reading everything
        first and applying it at the end compared readings up to a second old
        against a cache that notifications had since moved on, reported a
        missed notification that was never missed, and then wrote the older
        value over the newer one. Field by field, a notification that lands
        before a read is already in the cache that read is compared against,
        and one that lands after it overwrites it -- both correct.

        Not airtight: a notification arriving together with a read's response
        can be handled before the read's caller resumes. That window is
        milliseconds rather than the length of a poll, and costs at worst a
        needless re-subscribe.

        Callbacks fire if anything changed, even when a read fails partway:
        what was applied before the failure is real, and should not wait for
        the next callback to reach consumers.
        """
        client = self._client
        if client is None:
            raise BleakError("Not connected")

        before = dataclasses.replace(self._state)
        missed: set[str] = set()
        try:
            # Use cached temperature_unit (read once on connect)
            temperature_unit = self._state.temperature_unit or "F"
            self._state.temperature_unit = temperature_unit

            power_byte = await client.read_gatt_char(POWER_CHAR)
            power = bool(int.from_bytes(power_byte, "little"))
            if self._polled_disagrees(self._state.power, power):
                missed.add("power")
            self._state.power = power
            # A poll is device truth as much as a notification is, so it keeps
            # the transition baseline current. Without this the first power-off
            # after every connection would go unwatched, since connect()
            # establishes power by polling rather than by notification.
            self._last_notified_power = power

            mode_byte = await client.read_gatt_char(MODE_CHAR)
            mode_int = int.from_bytes(mode_byte, "little")
            if 0 <= mode_int < len(MODE_INT_TO_MODE_STATE):
                mode = MODE_INT_TO_MODE_STATE[mode_int]
                if self._polled_disagrees(self._state.mode, mode):
                    missed.add("mode")
                self._state.mode = mode
            else:
                _LOGGER.warning(
                    "%s: Unknown mode value during poll: %s",
                    self._model_id, mode_int,
                )
                if self._state.mode is None:
                    self._state.mode = "Regular"

            # SETTEMP_CHAR is always in Fahrenheit regardless of display unit.
            settemp_byte = await client.read_gatt_char(SETTEMP_CHAR)
            settemp_f = int.from_bytes(settemp_byte, "little")
            set_temperature = (
                _f_to_c(settemp_f) if temperature_unit == "C" else settemp_f
            )
            if self._polled_disagrees(self._state.set_temperature, set_temperature):
                missed.add("set_temperature")
            self._state.set_temperature = set_temperature

            # ACTUALTEMP_CHAR is in whatever the display unit is set to.
            #
            # The two sensor-derived characteristics sometimes answer a read
            # taken just after a connect with a placeholder instead of a value.
            # Keeping the last one is not a compromise: water level changes
            # over days, and a temperature is replaced by its notification
            # within a second, so the cached value is the right one either way.
            actualtemp_byte = await client.read_gatt_char(ACTUALTEMP_CHAR)
            actualtemp_int = int.from_bytes(actualtemp_byte, "little")
            actual_low, actual_high = (
                ACTUAL_TEMP_RANGE_C if temperature_unit == "C"
                else ACTUAL_TEMP_RANGE_F
            )
            if actual_low <= actualtemp_int <= actual_high:
                if self._polled_disagrees(
                    self._state.actual_temperature, actualtemp_int
                ):
                    missed.add("actual_temperature")
                self._state.actual_temperature = actualtemp_int
            else:
                _LOGGER.warning(
                    "%s: Ignoring actual temperature %s, outside %s-%s%s",
                    self._model_id, actualtemp_int, actual_low, actual_high,
                    temperature_unit,
                )

            waterlevel_byte = await client.read_gatt_char(WATER_LEVEL_CHAR)
            waterlevel_int = int.from_bytes(waterlevel_byte, "little")
            if WATER_LEVEL_RANGE[0] <= waterlevel_int <= WATER_LEVEL_RANGE[1]:
                self._state.water_level = waterlevel_int
            else:
                _LOGGER.warning(
                    "%s: Ignoring water level %s, outside %s-%s",
                    self._model_id, waterlevel_int, *WATER_LEVEL_RANGE,
                )

            clean_byte = await client.read_gatt_char(CLEAN_CHAR)
            clean_asserted = bool(int.from_bytes(clean_byte, "little"))
            # Power from state rather than from this poll's own read: a
            # power-off notified since that read has already cleared both
            # flags, and the older read would bring a finished clean back.
            self._state.deep_clean, self._state.uv_clean = (
                self._derive_clean_states(
                    clean_asserted, self._state.power is True, settemp_f
                )
            )

            # The parsed state alone cannot distinguish a placeholder the
            # device sent from a response that carried no data, since both
            # parse to 0. Only the raw responses show which happened, so log
            # them.
            _LOGGER.debug(
                "%s: Raw reads: power=%s mode=%s settemp=%s actualtemp=%s"
                " water=%s clean=%s%s",
                self._model_id,
                _hex(power_byte),
                _hex(mode_byte),
                _hex(settemp_byte),
                _hex(actualtemp_byte),
                _hex(waterlevel_byte),
                _hex(clean_byte),
                await self._counter_diagnostics(client),
            )
        finally:
            if self._state != before:
                self._fire_callbacks()
        return missed

    def _polled_disagrees(self, cached: object, polled: object) -> bool:
        """Whether a polled reading is evidence of a missed notification.

        Only once the check is armed: the first poll of a connection is the
        baseline, not a comparison. A ``None`` cache has nothing to compare
        against either (e.g. before any connect).
        """
        return (
            self._consistency_check_armed
            and cached is not None
            and cached != polled
        )

    async def _counter_diagnostics(self, client: BleakClientWithServiceCache) -> str:
        """Read the runtime counters, for the Raw reads line only.

        The device's UV cycle runs on accumulated runtime, not wall clock. Over
        17 cycles on two units at 60s polling, consecutive starts are 3602
        RUNTIME ticks apart -- not 3600. Robooler's residuals were exactly 0.0
        across 9 cycles; Tawaret averaged 3601.94. Cycle starts are dated from
        the counters alone, as rt - (uv_runtime - uv_runtime_previous_poll),
        which is exact to 1s because UV_RUNTIME is frozen between cycles.

        So a cycle's timing is predictable from RUNTIME, and an assertion
        outside the expected window could only be a deep clean -- a
        discriminator needing no setpoint, temperature or effort. Note the
        phase must be taken mod 3602: against 3600 it drifts ~48s per day.
        A 3602 period is indistinguishable from 3600 plus two ticks of
        per-cycle overhead, and the predictions are identical either way.

        Each cycle burns exactly 240 lamp seconds (16 of 16 complete cycles),
        and UV_RUNTIME does not move at all between cycles.

        Still open: whether a deep clean disturbs the schedule. It runs the
        lamp for ~45 minutes and may restart the timer, so a consumer re-learns
        the phase after any clean. RUNTIME can also step backwards (below).

        Answering it needs RUNTIME recorded beside the clean bit across many
        cycles and both units, which this collects from ordinary use. Holding
        the connection to measure it directly costs the integration its device
        and does not scale past a single session.

        LIFETIME is logged alongside as the cross-check on RUNTIME. RUNTIME is
        known to step BACKWARDS: by 6,353 and 9,182 across an abrupt power loss
        (see UNKNOWN_9A5F_CHAR), and once by exactly 131,072 -- two whole units
        of byte 2 -- on a unit that was powered and connected throughout, with
        the low 16 bits still counting normally through the step and the UV
        schedule undisturbed either side of it. Whatever clocks the cycle is
        therefore not this counter. If LIFETIME steps at the same moment the
        fault is a shared storage or readout path; if it does not, LIFETIME is
        a reference the step can be measured against.

        Read only when DEBUG is enabled, so nobody else pays three extra GATT
        reads per poll for diagnostics they are not collecting. Failures are
        swallowed: a diagnostic must never be able to fail a poll.
        """
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return ""
        try:
            runtime = await client.read_gatt_char(RUNTIME_CHAR)
            uv_runtime = await client.read_gatt_char(UV_RUNTIME_CHAR)
            lifetime = await client.read_gatt_char(LIFETIME_CHAR)
        except Exception:  # noqa: BLE001 - never break a poll for a log line
            return " runtime=? uv_runtime=? lifetime=?"
        return (
            f" runtime={_hex(runtime)} uv_runtime={_hex(uv_runtime)}"
            f" lifetime={_hex(lifetime)}"
        )

    def _derive_clean_states(
        self, asserted: bool, power: bool, settemp_f: int
    ) -> tuple[bool, bool]:
        """Split an asserted CLEAN bit into the two states that share it.

        The characteristic carries both the deep clean a person starts and the
        device's own hourly UV cycle. Only a deep clean forces SET_TEMP to
        :data:`CLEAN_TEMP_F`, so that is the discriminator -- and it is device
        state, not provenance, so a clean started in the vendor's app is
        recognised on the first poll after we reconnect. See CLEAN_TEMP_F in
        const.py for the measurements behind this.
        """
        if not asserted:
            self._clean_asserted_since = None
            self._warned_clean_while_off = False
            return False, False

        now = self._monotonic()
        if self._clean_asserted_since is None:
            self._clean_asserted_since = now

        if not power:
            # Nothing runs on a powered-off unit, so this is neither state.
            # Seen once, latched for ~9 hours, cause unknown. Reported rather
            # than modelled: claiming a cycle is running here would be a
            # statement we know to be false.
            #
            # Warned once per run, since it can latch for hours. The event
            # still fires every poll: consumers count it to see how long the
            # run lasted.
            log = _LOGGER.debug if self._warned_clean_while_off else _LOGGER.warning
            self._warned_clean_while_off = True
            log(
                "%s: CLEAN asserted while the device is off; reporting neither "
                "a deep clean nor a UV clean.",
                self._model_id,
            )
            self._fire_connection_event(
                ConnectionEventType.CLEAN_ASSERTED_WHILE_OFF,
                {"set_temperature": settemp_f},
            )
            return False, False
        self._warned_clean_while_off = False

        deep_clean = (
            # The device forced the setpoint, which only a deep clean does.
            settemp_f == CLEAN_TEMP_F
            # Or we asked for one and the forced setpoint has not landed yet.
            or self._deep_clean_pending
            # Or it has run too long to be the UV cycle. This is the only
            # signal for a user whose setpoint is ALREADY CLEAN_TEMP_F, where
            # a starting deep clean changes nothing observable.
            or now - self._clean_asserted_since >= UV_CLEAN_MAX_SECONDS
        )
        return deep_clean, not deep_clean

    async def async_poll(self) -> None:
        """Retrieve state from device.

        Each reading is compared against cached state on the four
        notify-backed fields as it lands (see :meth:`_read_and_apply_all`).
        A disagreement is positive evidence that a notification was missed,
        so the recovery ladder in :meth:`_handle_subscription_mismatch` is
        invoked — Tier 1 re-subscribes in place, Tier 2 forces a full
        reconnect if a prior re-subscribe did not clear the mismatch.
        """
        if self._client is None:
            return await self.connect()

        try:
            missed_fields = await self._read_and_apply_all()
        except BLEAK_RETRY_EXCEPTIONS:
            if self._connect_lock.locked():
                raise  # Called from _ensure_connected, let its handler deal with it
            _LOGGER.warning(
                "%s: Poll failed, attempting reconnect", self._model_id
            )
            await self._execute_forced_reconnect(trigger="poll_failure")
            try:
                missed_fields = await self._read_and_apply_all()
            except BLEAK_RETRY_EXCEPTIONS as err:
                raise OolerConnectionError(
                    f"{self._model_id}: Poll failed after reconnect: {err}"
                ) from err

        if self._consistency_check_armed:
            if missed_fields:
                refreshed = await self._handle_subscription_mismatch(missed_fields)
                if refreshed:
                    # Tier 2 ran: _execute_forced_reconnect → _ensure_connected
                    # → an inner async_poll already re-read state and re-armed.
                    return
            else:
                # Clean poll — any pending Tier 1 proved itself out.
                self._tier1_pending = False

        self._consistency_check_armed = True
        _LOGGER.debug("%s: State retrieved.", self._model_id)

    async def _handle_subscription_mismatch(
        self, missed_fields: set[str]
    ) -> bool:
        """Recover from a poll/state mismatch indicating missed notifications.

        Returns ``True`` if the recovery path re-read state itself (Tier 2 —
        a forced reconnect whose inner poll re-read everything and re-armed
        the check). In that case the caller has nothing left to do.

        Tier 1 (re-subscribe in place) returns ``False``. The poll has
        already applied the values it read, which become the baseline for
        the next poll's consistency check.
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

    async def _retry_on_stale(self, operation: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
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
        ``"subscription_mismatch"``, ``"poll_failure"``, ``"write_failure"``.

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

        # When turning on, re-send mode and temperature. Setters refuse while
        # the device is off, so these are the values it already reported —
        # writing them back is a no-op in the ordinary case. It is kept because
        # the device is not trusted to have retained them across a power cycle,
        # which is cheap to guarantee and expensive to be wrong about.
        # Written directly rather than through the setters, both to keep this
        # atomic and because they would refuse: state.power is not True until
        # the device confirms.
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

        Raises :class:`DeviceOffError` if the device is off — it drops writes
        while powered down. Invalid modes raise :class:`ValueError` first,
        since that is a caller error whatever the device is doing.
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
        if not self._state.power:
            raise DeviceOffError(
                f"{self._model_id}: cannot set mode while the device is off"
            )
        await self._write_gatt(MODE_CHAR, mode_int.to_bytes(1, "little"))
        _LOGGER.debug("Set mode to %s.", mode)
        self._state.mode = mode

    async def set_temperature(self, settemp_int: int) -> None:
        """Set target temperature. Value should be in the current display unit.

        Raises :class:`DeviceOffError` if the device is off — it drops writes
        while powered down, so there is nothing this can do but say so.
        Out-of-range values raise :class:`ValueError` first, since that is a
        caller error whatever the device is doing.
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
        if not self._state.power:
            raise DeviceOffError(
                f"{self._model_id}: cannot set temperature while the device is off"
            )
        await self._write_gatt(SETTEMP_CHAR, settemp_f.to_bytes(1, "little"))
        _LOGGER.debug(
            "Set temperature to %s (wrote %s°F to device).", settemp_int, settemp_f
        )
        # Only after a write that landed. Recording an intent we did not send
        # would put a value in state the device does not hold, and in
        # _last_user_setpoint a value the device never accepted.
        self._state.set_temperature = settemp_int
        self._last_user_setpoint = settemp_int

    async def set_deep_clean(self, deep_clean: bool) -> None:
        """Start or stop a deep clean.

        Starting one automatically powers on the device: a clean cannot run on
        a unit that is off, so powering it up is part of the operation asked
        for rather than a value cached for later.

        Raises :class:`DeviceOffError` when asked to stop a clean on a device
        that is off. The write cannot land -- the device drops writes while
        powered down -- and there is nothing running to stop.

        The device's own UV cycle is not startable or stoppable and has no
        setter; see :attr:`OolerBLEState.uv_clean`.
        """
        if self._client is None:
            await self.connect()
        if self._client is None:
            raise RuntimeError("Failed to connect to device")
        if not self._state.power:
            if deep_clean:
                await self.set_power(True)
            else:
                raise DeviceOffError(
                    f"{self._model_id}: cannot stop a deep clean while the "
                    "device is off"
                )
        # Set before the write: the device's forced-setpoint notification
        # arrives during the await, and a handler seeing this still false
        # records the clean's own temperature as the user's choice.
        self._deep_clean_pending = deep_clean
        self._state.deep_clean = deep_clean
        if deep_clean:
            # Mutually exclusive by construction; a poll would say the same.
            self._state.uv_clean = False
        await self._write_gatt(CLEAN_CHAR, int(deep_clean).to_bytes(1, "little"))
        _LOGGER.debug("Set deep clean to %s.", deep_clean)

    async def fix_setpoint_override(
        self, setpoint: int | None = None, clean_seconds: float | None = None
    ) -> None:
        """Stop the device overriding the setpoint when it is powered off.

        A deep clean run to completion makes the device replace the setpoint
        with a stored value every time it is powered off afterwards. Starting a
        clean and cancelling it clears that; a clean left to finish never can,
        because finishing ends by powering the device off and so never sends
        ``CLEAN=0``. See CLEAN_TEMP_F in const.py.

        Cancelling makes the device restore its stored setpoint by itself.
        ``setpoint`` overwrites that afterwards; pass ``None`` to keep whatever
        the device restored, which is the better answer when nothing else is
        known. Leaves the device powered as it was found. Harmless on a device
        that is not affected.
        """
        was_off = not self._state.power
        self._fix_started_clean = True
        try:
            await self.set_deep_clean(True)
            await asyncio.sleep(
                FIX_CLEAN_SECONDS[-1] if clean_seconds is None else clean_seconds
            )
        finally:
            # Stop the clean even if the wait was interrupted. A clean left
            # running finishes on its own, and finishing is what causes the
            # override — the fix would become the cause.
            #
            # Written directly rather than through set_deep_clean, which
            # refuses when it believes the device is off. That guard is right
            # for external callers but wrong here: this clean is ours, the
            # device was powered on to start it, and an exception there would
            # leave the clean running. _write_gatt raises if it cannot land, so
            # the flag survives for connect() to act on.
            await self._write_gatt(CLEAN_CHAR, b"\x00")
            self._state.deep_clean = False
            self._deep_clean_pending = False
            self._fix_started_clean = False
        try:
            if setpoint is not None:
                await self.set_temperature(setpoint)
        finally:
            # Restore the power state even if the setpoint write fails, or an
            # error between the two would leave the device running unasked.
            if was_off:
                await self.set_power(False)
            # None of this was asked for, and the setters do not fire callbacks.
            self._fire_callbacks()

    async def _watch_for_setpoint_override(self) -> None:
        """After a power-off, fix the device if it overrides the setpoint.

        A setpoint change while the device stays off can only be the device
        itself: it drops writes while off, its own buttons cannot change the
        setpoint while off, and it accepts one connection at a time so nothing
        else can be writing. Powering back on or losing the link mid-watch
        proves nothing either way, so neither counts as evidence.

        The power-off that ends a clean never starts one of these, because the
        device's revert of the setpoint the clean forced is expected there and
        proves nothing about whether the override was armed. See
        _handle_notification and CLEAN_TEMP_F in const.py.
        """
        reported = self._state.set_temperature
        deadline = self._monotonic() + SETPOINT_OVERRIDE_WATCH_SECONDS
        try:
            while self._state.set_temperature == reported:
                # Back in use, or no longer holding the connection this whole
                # premise rests on. Either way nothing was observed.
                if self._state.power or not self.is_connected:
                    return
                if self._monotonic() > deadline:
                    # Survived a full window off, so any earlier fix worked.
                    self._override_fixes_tried = 0
                    return
                await asyncio.sleep(1)
            if self._state.power:
                # Powered back on as the device overrode the setpoint. Fixing
                # now would run a clean on a device someone is using; the next
                # power-off will catch it.
                return
            overrode_with = self._state.set_temperature
            attempt = self._override_fixes_tried
            override = f"{reported} -> {overrode_with}"
            if attempt >= len(FIX_CLEAN_SECONDS):
                _LOGGER.warning(
                    "%s: Setpoint still overridden (%s) after trying %s; giving "
                    "up until it settles.",
                    self._model_id, override, FIX_CLEAN_SECONDS,
                )
                self._fire_connection_event(
                    ConnectionEventType.SETPOINT_OVERRIDE_UNFIXABLE,
                    {"attempts": attempt},
                )
                return
            self._override_fixes_tried = attempt + 1
            _LOGGER.info(
                "%s: Device overrode setpoint while off (%s); fixing with a "
                "%gs clean.",
                self._model_id, override, FIX_CLEAN_SECONDS[attempt],
            )
            # Only overwrite what the device restores if a person asked for
            # something. On the first override after a clean the reported value
            # is the clean's forced CLEAN_TEMP_F, while the device's own stored
            # value is the pre-clean setpoint — which is the better answer.
            await self.fix_setpoint_override(
                setpoint=self._last_user_setpoint,
                clean_seconds=FIX_CLEAN_SECONDS[attempt],
            )
            self._fire_connection_event(
                ConnectionEventType.SETPOINT_OVERRIDE_FIXED,
                {
                    "overrode": reported,
                    "overrode_with": overrode_with,
                    "restored": self._last_user_setpoint,
                    "attempt": attempt + 1,
                },
            )
        except Exception as err:  # noqa: BLE001 - background task, nothing to raise to
            _LOGGER.warning(
                "%s: Could not fix the setpoint override: %s", self._model_id, err
            )

    async def set_temperature_unit(self, unit: TemperatureUnit) -> None:
        """Set device display unit: 'C' or 'F'.

        Raises :class:`DeviceOffError` if the device is off — it drops writes
        while powered down. Invalid units raise :class:`ValueError` first,
        since that is a caller error whatever the device is doing.
        """
        if unit not in ("C", "F"):
            raise ValueError(f"Invalid temperature unit '{unit}'. Must be 'C' or 'F'")
        if self._client is None:
            await self.connect()
        if self._client is None:
            raise RuntimeError("Failed to connect to device")
        if not self._state.power:
            raise DeviceOffError(
                f"{self._model_id}: cannot set display unit while the device is off"
            )
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
        if self._setpoint_override_watch is not None:
            # Outlives the connection otherwise, and fixing needs the link.
            self._setpoint_override_watch.cancel()
            self._setpoint_override_watch = None
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
