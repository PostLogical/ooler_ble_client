from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any, TypeVar

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.service import BleakGATTServiceCollection
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakDBusError
from bleak import BleakClient
from bleak_retry_connector import establish_connection

from .models import OolerBLEState
from .const import *

WrapFuncType = TypeVar("WrapFuncType", bound=Callable[..., Any])

_LOGGER = logging.getLogger(__name__)

# power_characteristic = "7a2623ff-bd92-4c13-be9f-7023aa4ecb85"
# mode_characteristic = "cafe2421-d04c-458f-b1c0-253c6c97e8e8"
# settemp_characteristic = "6aa46711-a29d-4f8a-88e2-044ca1fd03ff"
# actualtemp_characteristic = "e8ebded3-9dca-45c2-a2d8-ceffb901474d"


class OolerBLEDevice:
    _operation_lock = asyncio.Lock()
    _state: OolerBLEState = OolerBLEState()
    _connect_lock: asyncio.Lock = asyncio.Lock()
    # _power_char: BleakGATTCharacteristic | None = None
    # _mode_char: BleakGATTCharacteristic | None = None
    # _settemp_char: BleakGATTCharacteristic | None = None
    # _actualtemp_char: BleakGATTCharacteristic | None = None
    _disconnect_timer: asyncio.TimerHandle | None = None
    _client: BleakClient | None = None
    _callbacks: list[Callable[[OolerBLEState], None]] = []



    def __init__(self, model: str) -> None:
        """Initialize the OolerBLEDevice."""
        self._model_id = model

        self._loop = asyncio.get_running_loop()

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Set the BLE Device and advertisement data."""
        self._ble_device = ble_device

    @property
    def is_connected(self) -> bool:
        """Return whether the device is connected."""
        if self._client is None:
            return False
        elif self._client.is_connected:
            if not self._state.connected:
                self._set_state_and_fire_callbacks(OolerBLEState(self._state.power, self._state.mode, self._state.set_temperature, self._state.actual_temperature, True))
            return True
        else:
            return False
    
    @property
    def address(self) -> str:
        """Return the address."""
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

    def register_callback(self, callback: Callable[[OolerBLEState], None]) -> Callable[[], None]:
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
                self._model_id #consider if need to fix model_id refs
            )
        if self.is_connected:
            self._reset_disconnect_timer()
            return
        async with self._connect_lock:
            #Check again while holding the lock
            if self.is_connected:
                self._reset_disconnect_timer()
                return
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
            # resolved = self._resolve_characteristics(client.services)
            # if not resolved:
            #     # Try to handle services failing to load
            #     resolved = self._resolve_characteristics(await client.get_services())
            
            self._client = client
            self._reset_disconnect_timer()
            _LOGGER.debug("%s: Attempt to retrieve intial state.", self._model_id)
            await self.async_poll(client)
            _LOGGER.debug(
                "%s: Subscribe to notifications", self._model_id
            )
            await client.start_notify(POWER_CHARACTERISTIC, self._notification_handler)
            await client.start_notify(MODE_CHARACTERISTIC, self._notification_handler)
            await client.start_notify(SETTEMP_CHARACTERISTIC, self._notification_handler)
            await client.start_notify(ACTUALTEMP_CHARACTERISTIC, self._notification_handler)

    def _notification_handler(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
        """Handle notification responses."""
        uuid = _sender.uuid
        _LOGGER.debug("%s: Notification received: %s from %s", self._model_id, data.hex(), uuid)

        power = self._state.power
        mode = self._state.mode
        settemp_int = self._state.set_temperature
        actualtemp_int = self._state.actual_temperature

        if uuid == POWER_CHARACTERISTIC:
            power = bool(int.from_bytes(data, "little"))
        elif uuid == MODE_CHARACTERISTIC:
            mode_int = int.from_bytes(data, "little")
            mode = MODE_INT_TO_MODE_STATE[mode_int]
        elif uuid == SETTEMP_CHARACTERISTIC:
            settemp_int = int.from_bytes(data, "little")
        elif uuid == ACTUALTEMP_CHARACTERISTIC:
            actualtemp_int = int.from_bytes(data, "little")

        self._set_state_and_fire_callbacks(OolerBLEState(power, mode, settemp_int, actualtemp_int, True))

    async def async_poll(self, client: BleakClient) -> None:
        """Retrieve state from device."""
        power_byte = await client.read_gatt_char(POWER_CHARACTERISTIC)
        mode_byte = await client.read_gatt_char(MODE_CHARACTERISTIC)
        settemp_byte = await client.read_gatt_char(SETTEMP_CHARACTERISTIC)
        actualtemp_byte = await client.read_gatt_char(ACTUALTEMP_CHARACTERISTIC)
        power = bool(int.from_bytes(power_byte, "little"))
        mode_int = int.from_bytes(mode_byte, "little")
        mode = MODE_INT_TO_MODE_STATE[mode_int]
        settemp_int = int.from_bytes(settemp_byte, "little")
        actualtemp_int = int.from_bytes(actualtemp_byte, "little")

        self._set_state_and_fire_callbacks(OolerBLEState(power, mode, settemp_int, actualtemp_int, True))
        _LOGGER.debug("%s: State retrieved.", self._model_id)

    async def set_power(self, power: bool) -> None:
        client = self._client
        if client is not None:
            power_byte = int(power).to_bytes(1, "little")
            await client.write_gatt_char(POWER_CHARACTERISTIC, power_byte)
        else:
            _LOGGER.error("Tried to set power, but BleakClient is None.")
    
    async def set_mode(self, mode: str) -> None:
        client = self._client
        if client is not None:
            mode_int = MODE_INT_TO_MODE_STATE.index(mode)
            mode_byte = mode_int.to_bytes(1, "little")
            await client.write_gatt_char(MODE_CHARACTERISTIC, mode_byte)
        else:
            _LOGGER.error("Tried to set mode, but BleakClient is None.")

    async def set_temperature(self, settemp_int: int) -> None:
        client = self._client
        if client is not None:
            settemp_byte = settemp_int.to_bytes(1, "little")
            await client.write_gatt_char(SETTEMP_CHARACTERISTIC, settemp_byte, True)
        else:
            _LOGGER.error("Tried to set temperature, but BleakClient is None.")

    def _reset_disconnect_timer(self) -> None:
        """Reset disconnect timer."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._disconnect_timer = self._loop.call_later(
            DISCONNECT_DELAY, self._disconnect
        )
    
    def _disconnected_callback(self, client: BleakClient) -> None:
        """Disconnected callback."""
        _LOGGER.debug(
            "%s: Disconnected from device", self._model_id
        )
        self._set_state_and_fire_callbacks(OolerBLEState(self._state.power, self._state.mode, self._state.set_temperature, self._state.actual_temperature, False))
    
    def _disconnect(self) -> None:
        """Disconnect from device."""
        self._disconnect_timer = None
        asyncio.create_task(self._execute_timed_disconnect())

    async def _execute_timed_disconnect(self) -> None:
        """Execute timed disconnection."""
        _LOGGER.debug(
            "%s: Disconnecting after timeout of %s",
            self._model_id,
            DISCONNECT_DELAY,
        )
        await self._execute_disconnect()

    async def _execute_disconnect(self) -> None:
        """Execute disconnection."""
        async with self._connect_lock:
            client = self._client
            # actualtemp_char = self._actualtemp_char
            self._expected_disconnect = True
            self._client = None
            # self._power_char = None
            # self._mode_char = None
            # self._settemp_char = None
            # self._actualtemp_char = None
            if client and client.is_connected:
                await client.stop_notify(POWER_CHARACTERISTIC)
                await client.stop_notify(MODE_CHARACTERISTIC)
                await client.stop_notify(SETTEMP_CHARACTERISTIC)
                await client.stop_notify(ACTUALTEMP_CHARACTERISTIC)
                await client.disconnect()

    # def _resolve_characteristics(self, services: BleakGATTServiceCollection) -> bool:
    #     """Resolve characteristics."""
    #     if char := services.get_characteristic(power_characteristic):
    #         self._power_char = char
    #     if char := services.get_characteristic(mode_characteristic):
    #         self._mode_char = char
    #     if char := services.get_characteristic(settemp_characteristic):
    #         self._settemp_char = char
    #     if char := services.get_characteristic(actualtemp_characteristic):
    #         self._actualtemp_char = char

    #     return bool((self._power_char and self._mode_char and self._settemp_char and self._actualtemp_char))

