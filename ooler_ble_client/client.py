from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BLEAK_RETRY_EXCEPTIONS,
    establish_connection,
)

from .models import OolerBLEState, OolerConnectionError, OolerMode, TemperatureUnit
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
    TEMP_LO_F,
    TEMP_MIN_F,
    TEMP_MAX_F,
    TEMP_HI_F,
)

_RECONNECT_BACKOFF_SECONDS = 0.5


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
        self._ble_device: BLEDevice | None = None
        self._expected_disconnect = False

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Set the BLE Device."""
        self._ble_device = ble_device

    @property
    def is_connected(self) -> bool:
        """Return whether the device is connected."""
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

    async def _ensure_connected(self) -> None:
        """Ensure connection to device is established."""
        if self._connect_lock.locked():
            _LOGGER.debug(
                "%s: Connection already in progress, waiting for it to complete",
                self._model_id,
            )
        if self.is_connected:
            return
        async with self._connect_lock:
            # Check again while holding the lock
            if self.is_connected:
                return
            ble_device = self._ble_device
            if ble_device is None:
                raise RuntimeError("BLE device not set — call set_ble_device() first")
            _LOGGER.debug("%s: Connecting", self._model_id)
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                self._model_id,
                self._disconnected_callback,
                max_attempts=5,
                use_services_cache=True,
                ble_device_callback=lambda: self._ble_device or ble_device,
            )
            _LOGGER.debug("%s: Connected", self._model_id)
            self._client = client
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
        """Retrieve state from device."""
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
            await self._execute_forced_reconnect()
            try:
                state = await self._read_all_characteristics()
            except BLEAK_RETRY_EXCEPTIONS as err:
                raise OolerConnectionError(
                    f"{self._model_id}: Poll failed after reconnect: {err}"
                ) from err

        self._set_state_and_fire_callbacks(state)
        _LOGGER.debug("%s: State retrieved.", self._model_id)

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
        await self._execute_forced_reconnect()
        try:
            return await operation()
        except BLEAK_RETRY_EXCEPTIONS as err:
            raise OolerConnectionError(
                f"{self._model_id}: Operation failed after reconnect: {err}"
            ) from err

    async def _execute_forced_reconnect(self) -> None:
        """Force disconnect and reconnect."""
        _LOGGER.debug("%s: Forcing reconnect", self._model_id)
        self._expected_disconnect = True
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
        await self._ensure_connected()

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

    def _disconnected_callback(self, client: BleakClientWithServiceCache | None) -> None:
        """Disconnected callback."""
        # Clear client immediately so is_connected returns False,
        # allowing the integration's BLE callback to trigger reconnection.
        self._client = None
        if self._expected_disconnect:
            _LOGGER.debug("%s: Expected disconnect from device", self._model_id)
        else:
            _LOGGER.warning(
                "%s: Unexpectedly disconnected from device", self._model_id
            )
        self._expected_disconnect = False
        self._fire_callbacks()

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
