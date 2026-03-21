from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.service import BleakGATTServiceCollection
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakDBusError
from bleak import BleakClient
from bleak_retry_connector import establish_connection

from .models import OolerBLEState
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
)

WrapFuncType = TypeVar("WrapFuncType", bound=Callable[..., Any])


def _f_to_c(f: int) -> int:
    """Convert Fahrenheit to Celsius (rounded)."""
    return round((f - 32) * 5 / 9)


def _c_to_f(c: int) -> int:
    """Convert Celsius to Fahrenheit (rounded)."""
    return round(c * 9 / 5 + 32)




class OolerBLEDevice:

    def __init__(self, model: str) -> None:
        """Initialize the OolerBLEDevice."""
        self._model_id = model
        self._state = OolerBLEState()
        self._connect_lock = asyncio.Lock()
        self._client: BleakClient | None = None
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
            if self._ble_device is None:
                raise RuntimeError("BLE device not set — call set_ble_device() first")
            _LOGGER.debug("%s: Connecting", self._model_id)
            client = await establish_connection(
                BleakClient,
                self._ble_device,
                self._model_id,
                self._disconnected_callback,
                use_services_cache=True,
                ble_device_callback=lambda: self._ble_device,
            )
            _LOGGER.debug("%s: Connected", self._model_id)
            self._client = client
            _LOGGER.debug("%s: Attempt to retrieve initial state.", self._model_id)
            await self.async_poll()
            _LOGGER.debug("%s: Subscribe to notifications", self._model_id)
            await client.start_notify(POWER_CHAR, self._notification_handler)
            await client.start_notify(MODE_CHAR, self._notification_handler)
            await client.start_notify(SETTEMP_CHAR, self._notification_handler)
            await client.start_notify(ACTUALTEMP_CHAR, self._notification_handler)
            await client.start_notify(WATER_LEVEL_CHAR, self._notification_handler)
            await client.start_notify(CLEAN_CHAR, self._notification_handler)

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle notification responses."""
        uuid = _sender.uuid
        _LOGGER.debug(
            "%s: Notification received: %s from %s", self._model_id, data.hex(), uuid
        )
        if uuid == POWER_CHAR:
            power = bool(int.from_bytes(data, "little"))
            self._state.power = power
            # OFF ends clean. Similarly, when clean mode ends, the device only sends OFF.
            if not power:
                self._state.clean = False
        elif uuid == MODE_CHAR:
            mode_int = int.from_bytes(data, "little")
            mode = MODE_INT_TO_MODE_STATE[mode_int]
            self._state.mode = mode
        elif uuid == SETTEMP_CHAR:
            # SETTEMP_CHAR always reports in Fahrenheit
            settemp_f = int.from_bytes(data, "little")
            self._state.set_temperature = (
                _f_to_c(settemp_f) if self._state.temperature_unit == "C" else settemp_f
            )
        elif uuid == ACTUALTEMP_CHAR:
            actualtemp_int = int.from_bytes(data, "little")
            self._state.actual_temperature = actualtemp_int
        elif uuid == WATER_LEVEL_CHAR:
            waterlevel_int = int.from_bytes(data, "little")
            self._state.water_level = waterlevel_int
        elif uuid == CLEAN_CHAR:
            clean = bool(int.from_bytes(data, "little"))
            self._state.clean = clean
        self._fire_callbacks()

    async def async_poll(self) -> None:
        """Retrieve state from device."""
        client = self._client
        if client is None:
            return await self.connect()

        power_byte = await client.read_gatt_char(POWER_CHAR)
        mode_byte = await client.read_gatt_char(MODE_CHAR)
        settemp_byte = await client.read_gatt_char(SETTEMP_CHAR)
        actualtemp_byte = await client.read_gatt_char(ACTUALTEMP_CHAR)
        waterlevel_byte = await client.read_gatt_char(WATER_LEVEL_CHAR)
        clean_byte = await client.read_gatt_char(CLEAN_CHAR)
        temp_unit_byte = await client.read_gatt_char(DISPLAY_TEMPERATURE_UNIT_CHAR)

        power = bool(int.from_bytes(power_byte, "little"))
        mode_int = int.from_bytes(mode_byte, "little")
        mode = MODE_INT_TO_MODE_STATE[mode_int]
        # SETTEMP_CHAR is always in Fahrenheit regardless of display unit.
        # ACTUALTEMP_CHAR is in whatever the display unit is set to.
        settemp_f = int.from_bytes(settemp_byte, "little")
        actualtemp_int = int.from_bytes(actualtemp_byte, "little")
        waterlevel_int = int.from_bytes(waterlevel_byte, "little")
        clean = bool(int.from_bytes(clean_byte, "little"))
        temperature_unit = "C" if int.from_bytes(temp_unit_byte, "little") == 1 else "F"

        # Convert set_temperature from F to display unit for consistent state
        set_temperature = _f_to_c(settemp_f) if temperature_unit == "C" else settemp_f

        self._set_state_and_fire_callbacks(
            OolerBLEState(
                power=power,
                mode=mode,
                set_temperature=set_temperature,
                actual_temperature=actualtemp_int,
                water_level=waterlevel_int,
                clean=clean,
                temperature_unit=temperature_unit,
            )
        )
        _LOGGER.debug("%s: State retrieved.", self._model_id)

    async def set_power(self, power: bool) -> None:
        if self._client is None:
            await self.connect()
        client = self._client
        if client is None:
            raise RuntimeError("Failed to connect to device")
        power_byte = int(power).to_bytes(1, "little")
        await client.write_gatt_char(POWER_CHAR, power_byte, True)
        _LOGGER.debug("Set power to %s.", power)
        self._state.power = power

        # Re-send other values that may have been changed while ooler is not running,
        # they are not updated unless on.
        if power:
            await self.set_mode(self._state.mode)
            await self.set_temperature(self._state.set_temperature)

    async def set_mode(self, mode: str) -> None:
        if self._client is None:
            await self.connect()
        client = self._client
        if client is None:
            raise RuntimeError("Failed to connect to device")
        mode_int = MODE_INT_TO_MODE_STATE.index(mode)
        mode_byte = mode_int.to_bytes(1, "little")
        await client.write_gatt_char(MODE_CHAR, mode_byte, True)
        _LOGGER.debug("Set mode to %s.", mode)
        self._state.mode = mode

    async def set_temperature(self, settemp_int: int) -> None:
        """Set target temperature. Value should be in the current display unit."""
        if self._client is None:
            await self.connect()
        client = self._client
        if client is None:
            raise RuntimeError("Failed to connect to device")
        # SETTEMP_CHAR always expects Fahrenheit — convert if display unit is C
        settemp_f = _c_to_f(settemp_int) if self._state.temperature_unit == "C" else settemp_int
        settemp_byte = settemp_f.to_bytes(1, "little")
        await client.write_gatt_char(SETTEMP_CHAR, settemp_byte, True)
        _LOGGER.debug("Set temperature to %s (wrote %s°F to device).", settemp_int, settemp_f)
        self._state.set_temperature = settemp_int

    async def set_clean(self, clean: bool) -> None:
        if self._client is None:
            await self.connect()
        client = self._client
        if client is None:
            raise RuntimeError("Failed to connect to device")
        # Turn on first else clean will not be active.
        await self.set_power(True)

        clean_byte = int(clean).to_bytes(1, "little")
        await client.write_gatt_char(CLEAN_CHAR, clean_byte, True)
        _LOGGER.debug("Set clean to %s.", clean)
        self._state.clean = clean

    async def set_temperature_unit(self, unit: str) -> None:
        if self._client is None:
            await self.connect()
        client = self._client
        if client is None:
            raise RuntimeError("Failed to connect to device")
        unit_byte = (1 if unit == "C" else 0).to_bytes(1, "little")
        await client.write_gatt_char(DISPLAY_TEMPERATURE_UNIT_CHAR, unit_byte, True)
        _LOGGER.debug("Set temperature unit to %s.", unit)
        self._state.temperature_unit = unit

    def _disconnected_callback(self, client: BleakClient) -> None:
        """Disconnected callback."""
        # Clear client immediately so is_connected returns False,
        # allowing the integration's BLE callback to trigger reconnection.
        self._client = None
        if self._expected_disconnect:
            _LOGGER.debug("%s: Expected disconnect from device", self._model_id)
        else:
            _LOGGER.warning("%s: Unexpectedly disconnected from device", self._model_id)
        self._expected_disconnect = False
        self._fire_callbacks()

    async def _execute_disconnect(self) -> None:
        """Execute disconnection."""
        async with self._connect_lock:
            client = self._client
            self._expected_disconnect = True
            self._client = None
            if client and client.is_connected:
                await client.stop_notify(POWER_CHAR)
                await client.stop_notify(MODE_CHAR)
                await client.stop_notify(SETTEMP_CHAR)
                await client.stop_notify(ACTUALTEMP_CHAR)
                await client.stop_notify(WATER_LEVEL_CHAR)
                await client.stop_notify(CLEAN_CHAR)
                await client.disconnect()
