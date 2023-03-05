# from __future__ import annotations

# import asyncio
# from collections.abc import Callable
# from typing import Any, TypeVar

# from bleak.backends.device import BLEDevice
# from bleak.backends.scanner import AdvertisementData
# from bleak.backends.service import BleakGATTServiceCollection
# from bleak.backends.characteristic import BleakGATTCharacteristic
# from bleak.exc import BleakDBusError
# from bleak import BleakClient
# from bleak_retry_connector import establish_connection

# from .const import *
# from .const import _LOGGER

# WrapFuncType = TypeVar("WrapFuncType", bound=Callable[..., Any])

# class OolerConnection:
#     _operation_lock = asyncio.Lock()
#     # _state: OolerBLEState = OolerBLEState()
#     _connect_lock: asyncio.Lock = asyncio.Lock()
#     # _disconnect_timer: asyncio.TimerHandle | None = None
#     _client: BleakClient | None = None
#     # _callbacks: list[Callable[[OolerBLEState], None]] = []

#     def __init__(self, model: str) -> None:
#         """Initialize the OolerBLEDevice."""
#         self._model_id = model
#         # self._loop = asyncio.get_running_loop()

#     def set_ble_device(self, ble_device: BLEDevice) -> None:
#         """Set the BLE Device and advertisement data."""
#         self._ble_device = ble_device

#     @property
#     def is_connected(self) -> bool:
#         """Return whether the device is connected."""
#         if self._client is None:
#             return False
#         elif self._client.is_connected:
#             return True
#         else:
#             return False
    
#     @property
#     def address(self) -> str:
#         """Return the address."""
#         return self._ble_device.address

#     # @property
#     # def state(self) -> OolerBLEState:
#     #     """Return the state."""
#     #     return self._state

#     @property
#     def powerstate(self) -> bool:
#         """Return the power state of device."""
#         return self._powerstate

#     async def connect(self) -> None:
#         await self._ensure_connected()

#     async def stop(self) -> None:
#         """Stop the client."""
#         _LOGGER.debug("%s: Stop", self._model_id)
#         await self._execute_disconnect()

#     # def _set_state_and_fire_callbacks(self, state: OolerBLEState) -> None:
#     #     if self._state != state:
#     #         self._state = state
#     #         self._fire_callbacks()

#     # def _fire_callbacks(self) -> None:
#     #     """Fire the callbacks."""
#     #     for callback in self._callbacks:
#     #         callback(self._state)

#     # def register_callback(self, callback: Callable[[OolerBLEState], None]) -> Callable[[], None]:
#     #     """Register a callback to be called when the state changes."""
#     #     def unregister_callback() -> None:
#     #         self._callbacks.remove(callback)

#     #     self._callbacks.append(callback)
#     #     return unregister_callback

#     async def _ensure_connected(self) -> None:
#         """Ensure connection to device is established."""
#         if self._connect_lock.locked():
#             _LOGGER.debug(
#                 "%s: Connection already in progress, waiting for it to complete",
#                 self._model_id
#             )
#         if self.is_connected:
#             # self._reset_disconnect_timer()
#             return
#         async with self._connect_lock:
#             #Check again while holding the lock
#             if self.is_connected:
#                 self._reset_disconnect_timer()
#                 return
#             _LOGGER.debug("%s: Connecting", self._model_id)
#             client = await establish_connection(
#                 BleakClient,
#                 self._ble_device,
#                 self._model_id,
#                 self._disconnected_callback,
#                 use_services_cache=True,
#                 ble_device_callback=lambda: self._ble_device,
#             )
#             _LOGGER.debug("%s: Connected", self._model_id)
#             self._client = client
#             # self._reset_disconnect_timer()
#             # _LOGGER.debug("%s: Attempt to retrieve intial state.", self._model_id)
#             # await self.async_poll()
#             # _LOGGER.debug(
#             #     "%s: Subscribe to notifications", self._model_id
#             # )
#             # await client.start_notify(POWER_CHAR, self._notification_handler)
#             # await client.start_notify(MODE_CHAR, self._notification_handler)
#             # await client.start_notify(SETTEMP_CHAR, self._notification_handler)
#             # await client.start_notify(ACTUALTEMP_CHAR, self._notification_handler)
#             # await client.start_notify(WATER_LEVEL_CHAR, self._notification_handler)
#             # await client.start_notify(PUMP_WATTS_CHAR, self._notification_handler)
#             # await client.start_notify(CLEAN_CHAR, self._notification_handler)

#     # def _notification_handler(self, _sender: BleakGATTCharacteristic, data: bytearray) -> None:
#     #     """Handle notification responses."""
#     #     uuid = _sender.uuid
#     #     _LOGGER.debug("%s: Notification received: %s from %s", self._model_id, data.hex(), uuid)
#     #     if uuid == POWER_CHAR:
#     #         power = bool(int.from_bytes(data, "little"))
#     #         self._state.power = power
#     #     elif uuid == MODE_CHAR:
#     #         mode_int = int.from_bytes(data, "little")
#     #         mode = MODE_INT_TO_MODE_STATE[mode_int]
#     #         self._state.mode = mode
#     #     elif uuid == SETTEMP_CHAR:
#     #         settemp_int = int.from_bytes(data, "little")
#     #         self._state.set_temperature = settemp_int
#     #     elif uuid == ACTUALTEMP_CHAR:
#     #         actualtemp_int = int.from_bytes(data, "little")
#     #         self._state.actual_temperature = actualtemp_int
#     #     elif uuid == WATER_LEVEL_CHAR:
#     #         waterlevel_int = int.from_bytes(data, "little")
#     #         self._state.water_level = waterlevel_int
#     #     elif uuid == PUMP_WATTS_CHAR:
#     #         pumpwatts_int = int.from_bytes(data, "little")
#     #         self._state.pump_watts = pumpwatts_int
#     #     elif uuid == CLEAN_CHAR:
#     #         clean = bool(int.from_bytes(data, "little"))
#     #         self._state.clean = clean
#     #     self._fire_callbacks()

#     # async def async_poll(self) -> None:
#     #     """Retrieve state from device."""
#     #     client = self._client
#     #     if client is None:
#     #         return await self.connect()

#     #     power_byte = await client.read_gatt_char(POWER_CHAR)
#     #     mode_byte = await client.read_gatt_char(MODE_CHAR)
#     #     settemp_byte = await client.read_gatt_char(SETTEMP_CHAR)
#     #     actualtemp_byte = await client.read_gatt_char(ACTUALTEMP_CHAR)
#     #     waterlevel_byte = await client.read_gatt_char(WATER_LEVEL_CHAR)
#     #     pumpwatts_byte = await client.read_gatt_char(PUMP_WATTS_CHAR)
#     #     clean_byte = await client.read_gatt_char(CLEAN_CHAR)

#     #     power = bool(int.from_bytes(power_byte, "little"))
#     #     mode_int = int.from_bytes(mode_byte, "little")
#     #     mode = MODE_INT_TO_MODE_STATE[mode_int]
#     #     settemp_int = int.from_bytes(settemp_byte, "little")
#     #     actualtemp_int = int.from_bytes(actualtemp_byte, "little")
#     #     waterlevel_int = int.from_bytes(waterlevel_byte, "little")
#     #     pumpwatts_int = int.from_bytes(pumpwatts_byte, "little")
#     #     clean = bool(int.from_bytes(clean_byte, "little"))

#     #     self._set_state_and_fire_callbacks(OolerBLEState(power, mode, settemp_int, actualtemp_int, waterlevel_int, pumpwatts_int, clean, True))
#     #     _LOGGER.debug("%s: State retrieved.", self._model_id)

#     async def check_connection_status(self) -> bool:
#         await self.connect()
#         client = self._client
#         orig_power_byte = await client.read_gatt_char(POWER_CHAR)
#         orig_power = bool(int.from_bytes(orig_power_byte, "little"))
#         write_power_byte = int(not orig_power).to_bytes(1, "little")
#         await client.write_gatt_char(POWER_CHAR, write_power_byte)
#         await asyncio.sleep(2)
#         read_power_byte = await client.read_gatt_char(POWER_CHAR)
#         if write_power_byte == read_power_byte:
#             await client.write_gatt_char(POWER_CHAR, orig_power_byte)
#             return True
#         else:
#             return False




    
#     # def _disconnected_callback(self, client: BleakClient) -> None:
#     #     """Disconnected callback."""
#     #     _LOGGER.debug(
#     #         "%s: Disconnected from device", self._model_id
#     #     )
#     #     self._state.connected = False
#     #     self._fire_callbacks()

#     def _disconnect(self) -> None:
#         """Disconnect from device."""
#         self._disconnect_timer = None
#         asyncio.create_task(self._execute_timed_disconnect())

#     async def _execute_timed_disconnect(self) -> None:
#         """Execute timed disconnection."""
#         _LOGGER.debug(
#             "%s: Disconnecting after timeout of %s",
#             self._model_id,
#             DISCONNECT_DELAY,
#         )
#         await self._execute_disconnect()

#     async def _execute_disconnect(self) -> None:
#         """Execute disconnection."""
#         async with self._connect_lock:
#             client = self._client
#             self._expected_disconnect = True
#             self._client = None
#             if client and client.is_connected:
#                 # await client.stop_notify(POWER_CHAR)
#                 # await client.stop_notify(MODE_CHAR)
#                 # await client.stop_notify(SETTEMP_CHAR)
#                 # await client.stop_notify(ACTUALTEMP_CHAR)
#                 # await client.stop_notify(WATER_LEVEL_CHAR)
#                 # await client.stop_notify(PUMP_WATTS_CHAR)
#                 # await client.stop_notify(CLEAN_CHAR)
#                 await client.disconnect()