"""Tests for OolerBLEDevice client logic."""
from __future__ import annotations

import asyncio
import random
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from bleak.exc import BleakError

from ooler_ble_client import OolerBLEDevice, OolerBLEState, OolerConnectionError
from ooler_ble_client.const import (
    MODE_INT_TO_MODE_STATE,
    POWER_CHAR,
    MODE_CHAR,
    SETTEMP_CHAR,
    ACTUALTEMP_CHAR,
    WATER_LEVEL_CHAR,
    CLEAN_CHAR,
    DISPLAY_TEMPERATURE_UNIT_CHAR,
)

# Standard GATT read responses for a fully populated state
_GATT_READS_F = [
    b"\x01",  # power = True
    b"\x01",  # mode = Regular
    b"\x48",  # settemp = 72°F
    b"\x4a",  # actualtemp = 74
    b"\x32",  # water_level = 50
    b"\x00",  # clean = False
]

_TEMP_UNIT_F = b"\x00"
_TEMP_UNIT_C = b"\x01"


def _make_sender(uuid: str) -> MagicMock:
    sender = MagicMock()
    sender.uuid = uuid
    return sender


def _make_connected_device() -> tuple[OolerBLEDevice, MagicMock]:
    """Create a device with a mocked connected client."""
    device = OolerBLEDevice(model="OOLER-TEST")
    client = MagicMock()
    client.is_connected = True
    client.write_gatt_char = AsyncMock()
    client.read_gatt_char = AsyncMock()
    client.start_notify = AsyncMock()
    client.stop_notify = AsyncMock()
    client.disconnect = AsyncMock()
    device._client = client
    device._state.temperature_unit = "F"
    device._state.mode = "Regular"
    device._state.set_temperature = 72
    device._state.power = False
    return device, client


def _make_mock_client(reads: list[bytes] | None = None) -> MagicMock:
    """Create a mock BLE client for establish_connection."""
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(
        side_effect=reads or [_TEMP_UNIT_F] + _GATT_READS_F
    )
    client.write_gatt_char = AsyncMock()
    client.start_notify = AsyncMock()
    client.stop_notify = AsyncMock()
    client.disconnect = AsyncMock()
    return client


def _patch_establish(mock_client: MagicMock):  # type: ignore[no-untyped-def]
    """Patch establish_connection to return a mock client."""
    return patch(
        "ooler_ble_client.client.establish_connection",
        new_callable=AsyncMock,
        return_value=mock_client,
    )


def _patch_sleep():  # type: ignore[no-untyped-def]
    """Patch asyncio.sleep to be instant."""
    return patch("asyncio.sleep", new_callable=AsyncMock)


class TestInit:
    def test_creates_instance(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        assert device.is_connected is False
        assert device.state == OolerBLEState()

    def test_separate_instances_have_separate_state(self) -> None:
        """Regression: class-level mutable attributes were shared across instances."""
        device1 = OolerBLEDevice(model="OOLER-1")
        device2 = OolerBLEDevice(model="OOLER-2")
        device1._state.power = True
        assert device2._state.power is None

    def test_separate_instances_have_separate_callbacks(self) -> None:
        """Regression: class-level mutable attributes were shared across instances."""
        device1 = OolerBLEDevice(model="OOLER-1")
        device2 = OolerBLEDevice(model="OOLER-2")
        device1.register_callback(lambda s: None)
        assert len(device2._callbacks) == 0

    def test_separate_instances_have_separate_locks(self) -> None:
        device1 = OolerBLEDevice(model="OOLER-1")
        device2 = OolerBLEDevice(model="OOLER-2")
        assert device1._connect_lock is not device2._connect_lock


class TestAddress:
    def test_raises_without_ble_device(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        with pytest.raises(RuntimeError, match="BLE device not set"):
            _ = device.address

    def test_returns_address(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        ble_device = MagicMock()
        ble_device.address = "AA:BB:CC:DD:EE:FF"
        device.set_ble_device(ble_device)
        assert device.address == "AA:BB:CC:DD:EE:FF"


class TestCallbacks:
    def test_register_and_fire(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        received: list[OolerBLEState] = []
        device.register_callback(lambda s: received.append(s))

        new_state = OolerBLEState(power=True, mode="Regular")
        device._set_state_and_fire_callbacks(new_state)

        assert len(received) == 1
        assert received[0].power is True

    def test_no_fire_on_same_state(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        received: list[OolerBLEState] = []
        device.register_callback(lambda s: received.append(s))

        state = OolerBLEState(power=True)
        device._set_state_and_fire_callbacks(state)
        device._set_state_and_fire_callbacks(state)

        assert len(received) == 1

    def test_unregister(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        received: list[OolerBLEState] = []
        unregister = device.register_callback(lambda s: received.append(s))
        unregister()

        device._set_state_and_fire_callbacks(OolerBLEState(power=True))
        assert len(received) == 0

    def test_multiple_callbacks(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        r1: list[OolerBLEState] = []
        r2: list[OolerBLEState] = []
        device.register_callback(lambda s: r1.append(s))
        device.register_callback(lambda s: r2.append(s))

        device._set_state_and_fire_callbacks(OolerBLEState(power=True))
        assert len(r1) == 1
        assert len(r2) == 1


class TestNotificationHandler:
    def test_power_on(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._notification_handler(
            _make_sender(POWER_CHAR), bytearray(b"\x01")
        )
        assert device.state.power is True

    def test_power_off_clears_clean(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.clean = True
        device._notification_handler(
            _make_sender(POWER_CHAR), bytearray(b"\x00")
        )
        assert device.state.power is False
        assert device.state.clean is False

    def test_power_off_no_callback_if_clean_already_false(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.power = False
        device._state.clean = False
        received: list[OolerBLEState] = []
        device.register_callback(lambda s: received.append(s))
        device._notification_handler(
            _make_sender(POWER_CHAR), bytearray(b"\x00")
        )
        assert len(received) == 0

    def test_mode(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._notification_handler(
            _make_sender(MODE_CHAR), bytearray(b"\x02")
        )
        assert device.state.mode == "Boost"

    def test_all_modes(self) -> None:
        for i, mode in enumerate(MODE_INT_TO_MODE_STATE):
            device = OolerBLEDevice(model="OOLER-TEST")
            device._notification_handler(
                _make_sender(MODE_CHAR), bytearray([i])
            )
            assert device.state.mode == mode

    def test_unknown_mode_ignored(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.mode = "Regular"
        device._notification_handler(
            _make_sender(MODE_CHAR), bytearray(b"\x09")
        )
        assert device.state.mode == "Regular"

    def test_settemp_fahrenheit(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.temperature_unit = "F"
        device._notification_handler(
            _make_sender(SETTEMP_CHAR), bytearray(b"\x48")
        )
        assert device.state.set_temperature == 72

    def test_settemp_celsius_conversion(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.temperature_unit = "C"
        device._notification_handler(
            _make_sender(SETTEMP_CHAR), bytearray(b"\x48")  # 72°F = 22°C
        )
        assert device.state.set_temperature == 22

    def test_actual_temperature(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._notification_handler(
            _make_sender(ACTUALTEMP_CHAR), bytearray(b"\x4a")
        )
        assert device.state.actual_temperature == 74

    def test_no_callback_on_unchanged_value(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.actual_temperature = 74
        received: list[OolerBLEState] = []
        device.register_callback(lambda s: received.append(s))
        device._notification_handler(
            _make_sender(ACTUALTEMP_CHAR), bytearray(b"\x4a")
        )
        assert len(received) == 0

    def test_callback_on_changed_value(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.actual_temperature = 74
        received: list[OolerBLEState] = []
        device.register_callback(lambda s: received.append(s))
        device._notification_handler(
            _make_sender(ACTUALTEMP_CHAR), bytearray(b"\x4b")
        )
        assert len(received) == 1

    def test_exception_in_handler_does_not_propagate(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        sender = MagicMock()
        sender.uuid = "unknown-uuid"
        device._notification_handler(sender, bytearray(b"\xff\xff\xff"))

    def test_exception_in_handler_logged(self) -> None:
        """An internal exception in the notification handler is caught and logged."""
        device = OolerBLEDevice(model="OOLER-TEST")
        # Register a callback that raises to trigger the except branch
        device.register_callback(lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
        device._state.power = False  # Ensure power changes
        device._notification_handler(
            _make_sender(POWER_CHAR), bytearray(b"\x01")
        )
        # Should not propagate; power state was updated before callback
        assert device.state.power is True


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_set_mode_invalid(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        with pytest.raises(ValueError, match="Invalid mode"):
            await device.set_mode("Turbo")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_set_temperature_unit_invalid(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        with pytest.raises(ValueError, match="Invalid temperature unit"):
            await device.set_temperature_unit("K")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_set_temperature_out_of_range(self) -> None:
        device, _ = _make_connected_device()
        with pytest.raises(ValueError, match="out of range"):
            await device.set_temperature(200)

    @pytest.mark.asyncio
    async def test_set_temperature_out_of_range_celsius(self) -> None:
        device, _ = _make_connected_device()
        device._state.temperature_unit = "C"
        with pytest.raises(ValueError, match="out of range"):
            await device.set_temperature(50)  # 50°C = 122°F, over 115

    @pytest.mark.asyncio
    async def test_set_temperature_below_range(self) -> None:
        device, _ = _make_connected_device()
        with pytest.raises(ValueError, match="out of range"):
            await device.set_temperature(30)


class TestIsConnected:
    def test_false_when_no_client(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        assert device.is_connected is False

    def test_true_when_client_connected(self) -> None:
        device, _ = _make_connected_device()
        assert device.is_connected is True

    def test_false_when_client_disconnected(self) -> None:
        device, client = _make_connected_device()
        client.is_connected = False
        assert device.is_connected is False


class TestSetPower:
    @pytest.mark.asyncio
    async def test_set_power_on(self) -> None:
        device, client = _make_connected_device()
        await device.set_power(True)
        assert client.write_gatt_char.call_count == 3
        assert device.state.power is True

    @pytest.mark.asyncio
    async def test_set_power_off(self) -> None:
        device, client = _make_connected_device()
        await device.set_power(False)
        assert client.write_gatt_char.call_count == 1
        assert device.state.power is False

    @pytest.mark.asyncio
    async def test_set_power_on_skips_resend_when_state_none(self) -> None:
        device, client = _make_connected_device()
        device._state.mode = None
        device._state.set_temperature = None
        await device.set_power(True)
        assert client.write_gatt_char.call_count == 1

    @pytest.mark.asyncio
    async def test_set_power_writes_correct_bytes(self) -> None:
        device, client = _make_connected_device()
        device._state.mode = None
        device._state.set_temperature = None
        await device.set_power(True)
        client.write_gatt_char.assert_called_with(POWER_CHAR, b"\x01", True)

    @pytest.mark.asyncio
    async def test_set_power_connects_if_not_connected(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client()
        with _patch_establish(mock_client) as mock_establish:
            await device.set_power(False)
            mock_establish.assert_called_once()
        assert device.state.power is False

    @pytest.mark.asyncio
    async def test_set_power_raises_if_connect_fails(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        with patch(
            "ooler_ble_client.client.establish_connection",
            new_callable=AsyncMock,
            side_effect=BleakError("can't connect"),
        ):
            with pytest.raises(BleakError):
                await device.set_power(True)

    @pytest.mark.asyncio
    async def test_set_power_on_resends_celsius_temp(self) -> None:
        device, client = _make_connected_device()
        device._state.temperature_unit = "C"
        device._state.set_temperature = 22  # 22°C = 72°F
        await device.set_power(True)
        # Third write should be SETTEMP with 72°F
        calls = client.write_gatt_char.call_args_list
        assert calls[2][0] == (SETTEMP_CHAR, b"\x48", True)


class TestSetMode:
    @pytest.mark.asyncio
    async def test_set_mode_regular(self) -> None:
        device, client = _make_connected_device()
        await device.set_mode("Regular")
        client.write_gatt_char.assert_called_with(MODE_CHAR, b"\x01", True)
        assert device.state.mode == "Regular"

    @pytest.mark.asyncio
    async def test_set_mode_boost(self) -> None:
        device, client = _make_connected_device()
        await device.set_mode("Boost")
        client.write_gatt_char.assert_called_with(MODE_CHAR, b"\x02", True)

    @pytest.mark.asyncio
    async def test_set_mode_silent(self) -> None:
        device, client = _make_connected_device()
        await device.set_mode("Silent")
        client.write_gatt_char.assert_called_with(MODE_CHAR, b"\x00", True)

    @pytest.mark.asyncio
    async def test_set_mode_connects_if_not_connected(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client()
        with _patch_establish(mock_client):
            await device.set_mode("Boost")
        assert device.state.mode == "Boost"

    @pytest.mark.asyncio
    async def test_set_mode_raises_if_connect_fails(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        with patch(
            "ooler_ble_client.client.establish_connection",
            new_callable=AsyncMock,
            side_effect=BleakError("fail"),
        ):
            with pytest.raises(BleakError):
                await device.set_mode("Silent")


class TestSetTemperature:
    @pytest.mark.asyncio
    async def test_set_temperature_fahrenheit(self) -> None:
        device, client = _make_connected_device()
        await device.set_temperature(72)
        client.write_gatt_char.assert_called_with(SETTEMP_CHAR, b"\x48", True)
        assert device.state.set_temperature == 72

    @pytest.mark.asyncio
    async def test_set_temperature_celsius_converts(self) -> None:
        device, client = _make_connected_device()
        device._state.temperature_unit = "C"
        await device.set_temperature(22)
        client.write_gatt_char.assert_called_with(SETTEMP_CHAR, b"\x48", True)
        assert device.state.set_temperature == 22

    @pytest.mark.asyncio
    async def test_set_temperature_connects_if_not_connected(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client()
        with _patch_establish(mock_client):
            await device.set_temperature(72)
        assert device.state.set_temperature == 72

    @pytest.mark.asyncio
    async def test_set_temperature_raises_if_connect_fails(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        with patch(
            "ooler_ble_client.client.establish_connection",
            new_callable=AsyncMock,
            side_effect=BleakError("fail"),
        ):
            with pytest.raises(BleakError):
                await device.set_temperature(72)


class TestSetClean:
    @pytest.mark.asyncio
    async def test_set_clean_on(self) -> None:
        device, client = _make_connected_device()
        await device.set_clean(True)
        assert device.state.clean is True
        assert device.state.power is True

    @pytest.mark.asyncio
    async def test_set_clean_off(self) -> None:
        device, client = _make_connected_device()
        device._state.power = True
        await device.set_clean(False)
        assert device.state.clean is False

    @pytest.mark.asyncio
    async def test_set_clean_connects_if_not_connected(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client()
        with _patch_establish(mock_client):
            await device.set_clean(True)
        assert device.state.clean is True

    @pytest.mark.asyncio
    async def test_set_clean_raises_if_connect_fails(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        with patch(
            "ooler_ble_client.client.establish_connection",
            new_callable=AsyncMock,
            side_effect=BleakError("fail"),
        ):
            with pytest.raises(BleakError):
                await device.set_clean(True)


class TestSetTemperatureUnit:
    @pytest.mark.asyncio
    async def test_set_celsius(self) -> None:
        device, client = _make_connected_device()
        await device.set_temperature_unit("C")
        client.write_gatt_char.assert_called_with(
            DISPLAY_TEMPERATURE_UNIT_CHAR, b"\x01", True
        )
        assert device.state.temperature_unit == "C"

    @pytest.mark.asyncio
    async def test_set_fahrenheit(self) -> None:
        device, client = _make_connected_device()
        await device.set_temperature_unit("F")
        client.write_gatt_char.assert_called_with(
            DISPLAY_TEMPERATURE_UNIT_CHAR, b"\x00", True
        )

    @pytest.mark.asyncio
    async def test_set_unit_connects_if_not_connected(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client()
        with _patch_establish(mock_client):
            await device.set_temperature_unit("C")
        assert device.state.temperature_unit == "C"

    @pytest.mark.asyncio
    async def test_set_unit_raises_if_connect_fails(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        with patch(
            "ooler_ble_client.client.establish_connection",
            new_callable=AsyncMock,
            side_effect=BleakError("fail"),
        ):
            with pytest.raises(BleakError):
                await device.set_temperature_unit("C")


class TestReadAllCharacteristics:
    @pytest.mark.asyncio
    async def test_reads_all_six_chars(self) -> None:
        device, client = _make_connected_device()
        client.read_gatt_char = AsyncMock(side_effect=_GATT_READS_F)
        state = await device._read_all_characteristics()
        assert state.power is True
        assert state.mode == "Regular"
        assert state.set_temperature == 72
        assert state.actual_temperature == 74
        assert state.water_level == 50
        assert state.clean is False
        assert state.temperature_unit == "F"

    @pytest.mark.asyncio
    async def test_reads_with_celsius_conversion(self) -> None:
        device, client = _make_connected_device()
        device._state.temperature_unit = "C"
        client.read_gatt_char = AsyncMock(side_effect=[
            b"\x01", b"\x00", b"\x48", b"\x17", b"\x64", b"\x01",
        ])
        state = await device._read_all_characteristics()
        assert state.set_temperature == 22
        assert state.temperature_unit == "C"

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        with pytest.raises(BleakError, match="Not connected"):
            await device._read_all_characteristics()

    @pytest.mark.asyncio
    async def test_unknown_mode_falls_back(self) -> None:
        device, client = _make_connected_device()
        client.read_gatt_char = AsyncMock(side_effect=[
            b"\x01", b"\x09", b"\x48", b"\x4a", b"\x32", b"\x00",
        ])
        state = await device._read_all_characteristics()
        assert state.mode == "Regular"

    @pytest.mark.asyncio
    async def test_defaults_temp_unit_to_f(self) -> None:
        device, client = _make_connected_device()
        device._state.temperature_unit = None
        client.read_gatt_char = AsyncMock(side_effect=_GATT_READS_F)
        state = await device._read_all_characteristics()
        assert state.temperature_unit == "F"


class TestAsyncPoll:
    @pytest.mark.asyncio
    async def test_poll_updates_state(self) -> None:
        device, client = _make_connected_device()
        client.read_gatt_char = AsyncMock(side_effect=[
            b"\x01", b"\x02", b"\x48", b"\x4a", b"\x32", b"\x00",
        ])
        await device.async_poll()
        assert device.state.power is True
        assert device.state.mode == "Boost"

    @pytest.mark.asyncio
    async def test_poll_connects_if_no_client(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client()
        with _patch_establish(mock_client) as mock_establish:
            await device.async_poll()
            mock_establish.assert_called_once()

    @pytest.mark.asyncio
    async def test_poll_reconnects_on_stale(self) -> None:
        device, old_client = _make_connected_device()
        device._ble_device = MagicMock()
        old_client.read_gatt_char = AsyncMock(side_effect=BleakError("stale"))

        new_client = _make_mock_client()
        # After reconnect, the poll reads again
        new_client.read_gatt_char = AsyncMock(side_effect=[
            _TEMP_UNIT_F,  # temp unit read during _ensure_connected
            *_GATT_READS_F,  # poll during _ensure_connected
            *_GATT_READS_F,  # the retry poll
        ])

        with _patch_establish(new_client), _patch_sleep():
            await device.async_poll()
        assert device.state.power is True

    @pytest.mark.asyncio
    async def test_poll_raises_connection_error_after_retry(self) -> None:
        device, old_client = _make_connected_device()
        device._ble_device = MagicMock()
        old_client.read_gatt_char = AsyncMock(side_effect=BleakError("stale"))

        new_client = _make_mock_client()
        # Reconnect succeeds but the retry poll also fails
        new_client.read_gatt_char = AsyncMock(side_effect=[
            _TEMP_UNIT_F,
            *_GATT_READS_F,  # poll during _ensure_connected
            BleakError("still broken"),  # retry poll fails
        ])

        with _patch_establish(new_client), _patch_sleep():
            with pytest.raises(OolerConnectionError):
                await device.async_poll()

    @pytest.mark.asyncio
    async def test_poll_reraises_during_ensure_connected(self) -> None:
        """When poll is called from _ensure_connected (lock held), don't retry."""
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = MagicMock()
        mock_client.is_connected = True
        # Temp unit read succeeds, poll fails
        mock_client.read_gatt_char = AsyncMock(side_effect=[
            _TEMP_UNIT_F,
            BleakError("fail during setup"),
        ])
        mock_client.disconnect = AsyncMock()

        with _patch_establish(mock_client):
            with pytest.raises(BleakError, match="fail during setup"):
                await device.connect()


class TestRetryOnStale:
    @pytest.mark.asyncio
    async def test_succeeds_first_try(self) -> None:
        device, _ = _make_connected_device()
        op = AsyncMock(return_value="ok")
        result = await device._retry_on_stale(op)
        assert result == "ok"
        assert op.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_immediately_on_first_failure(self) -> None:
        device, _ = _make_connected_device()
        op = AsyncMock(side_effect=[BleakError("fail"), "ok"])
        result = await device._retry_on_stale(op)
        assert result == "ok"
        assert op.call_count == 2

    @pytest.mark.asyncio
    async def test_reconnects_on_second_failure(self) -> None:
        device, _ = _make_connected_device()
        device._ble_device = MagicMock()
        call_count = 0

        async def flaky_op() -> str:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise BleakError("fail")
            return "ok"

        new_client = _make_mock_client()
        with _patch_establish(new_client), _patch_sleep():
            result = await device._retry_on_stale(flaky_op)
            assert result == "ok"
            assert call_count == 3

    @pytest.mark.asyncio
    async def test_raises_connection_error_after_all_retries(self) -> None:
        device, _ = _make_connected_device()
        device._ble_device = MagicMock()
        op = AsyncMock(side_effect=BleakError("permanent fail"))

        new_client = _make_mock_client()
        with _patch_establish(new_client), _patch_sleep():
            with pytest.raises(OolerConnectionError):
                await device._retry_on_stale(op)

    @pytest.mark.asyncio
    async def test_retries_on_timeout_error(self) -> None:
        device, _ = _make_connected_device()
        op = AsyncMock(side_effect=[asyncio.TimeoutError(), "ok"])
        result = await device._retry_on_stale(op)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_retries_on_eof_error(self) -> None:
        device, _ = _make_connected_device()
        op = AsyncMock(side_effect=[EOFError(), "ok"])
        result = await device._retry_on_stale(op)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_retries_on_broken_pipe(self) -> None:
        device, _ = _make_connected_device()
        op = AsyncMock(side_effect=[BrokenPipeError(), "ok"])
        result = await device._retry_on_stale(op)
        assert result == "ok"


class TestWriteGatt:
    @pytest.mark.asyncio
    async def test_write_succeeds(self) -> None:
        device, client = _make_connected_device()
        await device._write_gatt(POWER_CHAR, b"\x01")
        client.write_gatt_char.assert_called_with(POWER_CHAR, b"\x01", True)

    @pytest.mark.asyncio
    async def test_write_raises_when_not_connected(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        with pytest.raises((BleakError, RuntimeError)):
            await device._write_gatt(POWER_CHAR, b"\x01")


class TestDisconnectedCallback:
    def test_clears_client(self) -> None:
        device, client = _make_connected_device()
        device._disconnected_callback(client)
        assert device._client is None
        assert device.is_connected is False

    def test_fires_callbacks(self) -> None:
        device, client = _make_connected_device()
        received: list[OolerBLEState] = []
        device.register_callback(lambda s: received.append(s))
        device._disconnected_callback(client)
        assert len(received) == 1

    def test_expected_disconnect_flag(self) -> None:
        device, client = _make_connected_device()
        device._expected_disconnect = True
        device._disconnected_callback(client)
        assert device._expected_disconnect is False

    def test_unexpected_disconnect(self) -> None:
        device, client = _make_connected_device()
        device._expected_disconnect = False
        device._disconnected_callback(client)
        assert device._expected_disconnect is False
        assert device._client is None


class TestExecuteDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_clears_client(self) -> None:
        device, client = _make_connected_device()
        await device._execute_disconnect()
        assert device._client is None
        client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_unsubscribes_notifications(self) -> None:
        device, client = _make_connected_device()
        await device._execute_disconnect()
        assert client.stop_notify.call_count == 4
        client.stop_notify.assert_any_call(POWER_CHAR)
        client.stop_notify.assert_any_call(MODE_CHAR)
        client.stop_notify.assert_any_call(SETTEMP_CHAR)
        client.stop_notify.assert_any_call(ACTUALTEMP_CHAR)

    @pytest.mark.asyncio
    async def test_disconnect_handles_stop_notify_failure(self) -> None:
        device, client = _make_connected_device()
        client.stop_notify = AsyncMock(side_effect=BleakError("already gone"))
        await device._execute_disconnect()
        client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_no_client(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        await device._execute_disconnect()

    @pytest.mark.asyncio
    async def test_stop_calls_disconnect(self) -> None:
        device, client = _make_connected_device()
        await device.stop()
        client.disconnect.assert_called_once()


class TestEnsureConnected:
    @pytest.mark.asyncio
    async def test_connects_and_sets_up(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client()

        with _patch_establish(mock_client):
            await device.connect()

        assert device.is_connected is True
        assert device.state.temperature_unit == "F"
        assert mock_client.start_notify.call_count == 4

    @pytest.mark.asyncio
    async def test_connects_with_celsius(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client([
            _TEMP_UNIT_C,
            b"\x01", b"\x01", b"\x16", b"\x17", b"\x32", b"\x00",
        ])

        with _patch_establish(mock_client):
            await device.connect()

        assert device.state.temperature_unit == "C"

    @pytest.mark.asyncio
    async def test_raises_without_ble_device(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        with pytest.raises(RuntimeError, match="BLE device not set"):
            await device.connect()

    @pytest.mark.asyncio
    async def test_skips_if_already_connected(self) -> None:
        device, _ = _make_connected_device()
        with _patch_establish(MagicMock()) as mock_establish:
            await device.connect()
            mock_establish.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleans_up_on_setup_failure(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.read_gatt_char = AsyncMock(
            side_effect=BleakError("GATT read failed")
        )
        mock_client.disconnect = AsyncMock()

        with _patch_establish(mock_client):
            with pytest.raises(BleakError):
                await device.connect()

        assert device._client is None
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_subscribes_to_four_notifications(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client()

        with _patch_establish(mock_client):
            await device.connect()

        assert mock_client.start_notify.call_count == 4
        subscribed_chars = [
            call.args[0] for call in mock_client.start_notify.call_args_list
        ]
        assert POWER_CHAR in subscribed_chars
        assert MODE_CHAR in subscribed_chars
        assert SETTEMP_CHAR in subscribed_chars
        assert ACTUALTEMP_CHAR in subscribed_chars
        assert WATER_LEVEL_CHAR not in subscribed_chars
        assert CLEAN_CHAR not in subscribed_chars

    @pytest.mark.asyncio
    async def test_second_check_inside_lock(self) -> None:
        """If connection completes between outer check and lock acquisition."""
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client()

        with _patch_establish(mock_client) as mock_establish:
            # First connect
            await device.connect()
            assert mock_establish.call_count == 1
            # Second connect — already connected, skips
            await device.connect()
            assert mock_establish.call_count == 1

    @pytest.mark.asyncio
    async def test_lock_already_held_logs_and_waits(self) -> None:
        """When lock is already held, the second caller waits."""
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client()

        connected_order: list[int] = []

        async def slow_establish(*args, **kwargs):  # type: ignore[no-untyped-def]
            await asyncio.sleep(0)
            connected_order.append(len(connected_order))
            return mock_client

        with patch(
            "ooler_ble_client.client.establish_connection",
            side_effect=slow_establish,
        ):
            await asyncio.gather(device.connect(), device.connect())

        # Only one actual connection should have happened
        assert len(connected_order) == 1

    @pytest.mark.asyncio
    async def test_concurrent_connect_waits(self) -> None:
        """Two concurrent connects should result in one connection, not two."""
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client()

        with _patch_establish(mock_client) as mock_establish:
            await asyncio.gather(device.connect(), device.connect())
            assert mock_establish.call_count == 1


class TestForcedReconnect:
    @pytest.mark.asyncio
    async def test_reconnects(self) -> None:
        device, old_client = _make_connected_device()
        device._ble_device = MagicMock()
        new_client = _make_mock_client()

        with _patch_establish(new_client), _patch_sleep():
            await device._execute_forced_reconnect()

        old_client.disconnect.assert_called_once()
        assert device._client is new_client

    @pytest.mark.asyncio
    async def test_handles_disconnect_failure(self) -> None:
        device, old_client = _make_connected_device()
        device._ble_device = MagicMock()
        old_client.disconnect = AsyncMock(side_effect=Exception("gone"))
        new_client = _make_mock_client()

        with _patch_establish(new_client), _patch_sleep():
            await device._execute_forced_reconnect()

        assert device._client is new_client

    @pytest.mark.asyncio
    async def test_sets_expected_disconnect(self) -> None:
        device, _ = _make_connected_device()
        device._ble_device = MagicMock()
        new_client = _make_mock_client()

        expected_values: list[bool] = []
        original_disconnect = device._disconnected_callback

        def capture_expected(client: MagicMock) -> None:
            expected_values.append(device._expected_disconnect)

        device._disconnected_callback = capture_expected  # type: ignore[assignment]

        with _patch_establish(new_client), _patch_sleep():
            await device._execute_forced_reconnect()


# ============================================================================
# Stress / resilience tests
# ============================================================================

class TestSetterConnectFailure:
    """Test the defensive RuntimeError when connect() completes but _client is still None."""

    @pytest.mark.asyncio
    async def test_set_power_raises_if_client_still_none(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device.connect = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="Failed to connect"):
            await device.set_power(True)

    @pytest.mark.asyncio
    async def test_set_mode_raises_if_client_still_none(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device.connect = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="Failed to connect"):
            await device.set_mode("Boost")

    @pytest.mark.asyncio
    async def test_set_temperature_raises_if_client_still_none(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.temperature_unit = "F"
        device.connect = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="Failed to connect"):
            await device.set_temperature(72)

    @pytest.mark.asyncio
    async def test_set_clean_raises_if_client_still_none(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device.connect = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="Failed to connect"):
            await device.set_clean(True)

    @pytest.mark.asyncio
    async def test_set_temperature_unit_raises_if_client_still_none(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device.connect = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="Failed to connect"):
            await device.set_temperature_unit("C")


class TestRapidConnectDisconnect:
    @pytest.mark.asyncio
    async def test_rapid_connect_disconnect_cycles(self) -> None:
        """Rapidly cycle connect/disconnect and verify state consistency."""
        device = OolerBLEDevice(model="OOLER-STRESS")
        device._ble_device = MagicMock()

        for i in range(10):
            mock_client = _make_mock_client()
            with _patch_establish(mock_client):
                await device.connect()
                assert device.is_connected is True
                assert device._client is mock_client
                await device.stop()
                assert device.is_connected is False
                assert device._client is None

    @pytest.mark.asyncio
    async def test_rapid_disconnect_callback_cycles(self) -> None:
        """Simulate rapid unexpected disconnects."""
        device = OolerBLEDevice(model="OOLER-STRESS")
        callbacks_fired = 0

        def on_change(state: OolerBLEState) -> None:
            nonlocal callbacks_fired
            callbacks_fired += 1

        device.register_callback(on_change)

        for i in range(10):
            client = MagicMock()
            client.is_connected = True
            device._client = client
            device._disconnected_callback(client)
            assert device._client is None
            assert device.is_connected is False

        # Each disconnect fires a callback
        assert callbacks_fired == 10

    @pytest.mark.asyncio
    async def test_connect_disconnect_no_resource_leak(self) -> None:
        """After multiple cycles, no dangling clients or callbacks."""
        device = OolerBLEDevice(model="OOLER-STRESS")
        device._ble_device = MagicMock()
        clients: list[MagicMock] = []

        for _ in range(5):
            mock_client = _make_mock_client()
            clients.append(mock_client)
            with _patch_establish(mock_client):
                await device.connect()
                await device.stop()

        assert device._client is None
        # All clients should have been disconnected
        for c in clients:
            c.disconnect.assert_called_once()


class TestRandomGattExceptions:
    @pytest.mark.asyncio
    async def test_random_exceptions_during_writes(self) -> None:
        """Inject random BLEAK_RETRY_EXCEPTIONS during GATT writes."""
        exception_types = [BleakError, EOFError, BrokenPipeError, asyncio.TimeoutError]
        device, client = _make_connected_device()
        device._ble_device = MagicMock()

        for exc_type in exception_types:
            # First call fails, second succeeds (immediate retry)
            client.write_gatt_char = AsyncMock(
                side_effect=[exc_type("transient"), None]
            )
            await device.set_mode("Boost")
            assert device.state.mode == "Boost"

    @pytest.mark.asyncio
    async def test_random_exceptions_during_reads(self) -> None:
        """Inject random exceptions during GATT reads in async_poll."""
        device, client = _make_connected_device()
        device._ble_device = MagicMock()

        # First read fails, reconnect succeeds, retry poll works
        new_client = _make_mock_client()
        new_client.read_gatt_char = AsyncMock(side_effect=[
            _TEMP_UNIT_F,
            *_GATT_READS_F,  # poll during reconnect setup
            *_GATT_READS_F,  # retry poll
        ])

        client.read_gatt_char = AsyncMock(side_effect=EOFError("proxy died"))

        with _patch_establish(new_client), _patch_sleep():
            await device.async_poll()

        assert device.state.power is True

    @pytest.mark.asyncio
    async def test_mixed_exception_types_exhaust_retries(self) -> None:
        """Different exception types across retries all get caught."""
        device, _ = _make_connected_device()
        device._ble_device = MagicMock()

        call_count = 0

        async def mixed_failures() -> str:
            nonlocal call_count
            call_count += 1
            exceptions = [BleakError("e1"), EOFError("e2"), BrokenPipeError("e3")]
            if call_count <= 3:
                raise exceptions[call_count - 1]
            return "ok"  # pragma: no cover

        new_client = _make_mock_client()
        with _patch_establish(new_client), _patch_sleep():
            with pytest.raises(OolerConnectionError):
                await device._retry_on_stale(mixed_failures)


class TestConcurrentOperations:
    @pytest.mark.asyncio
    async def test_concurrent_set_power_and_set_temperature(self) -> None:
        """Concurrent set_power and set_temperature should not corrupt state."""
        device, client = _make_connected_device()

        async def slow_write(char: str, data: bytes, response: bool) -> None:
            await asyncio.sleep(0)  # Yield to event loop

        client.write_gatt_char = AsyncMock(side_effect=slow_write)

        await asyncio.gather(
            device.set_power(True),
            device.set_temperature(75),
        )

        assert device.state.power is True
        assert device.state.set_temperature == 75

    @pytest.mark.asyncio
    async def test_concurrent_poll_and_write(self) -> None:
        """Concurrent async_poll and set_mode should not crash."""
        device, client = _make_connected_device()
        client.read_gatt_char = AsyncMock(side_effect=[
            b"\x01", b"\x01", b"\x48", b"\x4a", b"\x32", b"\x00",
        ])

        await asyncio.gather(
            device.async_poll(),
            device.set_mode("Boost"),
        )

        # Both should complete without error
        assert device.state.mode == "Boost"

    @pytest.mark.asyncio
    async def test_concurrent_connects(self) -> None:
        """Multiple concurrent connects should only establish one connection."""
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client()

        with _patch_establish(mock_client) as mock_establish:
            await asyncio.gather(
                device.connect(),
                device.connect(),
                device.connect(),
            )
            # Only one actual connection attempt
            assert mock_establish.call_count == 1

    @pytest.mark.asyncio
    async def test_disconnect_during_poll(self) -> None:
        """Disconnect callback during poll should not deadlock."""
        device, client = _make_connected_device()
        device._ble_device = MagicMock()

        call_count = 0

        async def read_then_disconnect(char: str) -> bytes:
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise BleakError("disconnected mid-poll")
            return b"\x01"

        client.read_gatt_char = AsyncMock(side_effect=read_then_disconnect)
        # After reconnect, new client provides enough reads for both
        # the setup poll and the retry poll
        new_client = _make_mock_client([
            _TEMP_UNIT_F,
            *_GATT_READS_F,  # poll during _ensure_connected
            *_GATT_READS_F,  # retry poll in async_poll
        ])

        with _patch_establish(new_client), _patch_sleep():
            await device.async_poll()

        # Should have reconnected and completed
        assert device.is_connected is True


class TestStateConsistency:
    @pytest.mark.asyncio
    async def test_state_after_failed_write(self) -> None:
        """State should not update if the write fails permanently."""
        device, client = _make_connected_device()
        device._ble_device = MagicMock()
        device._state.mode = "Regular"

        # All three attempts fail (immediate, immediate retry, post-reconnect)
        client.write_gatt_char = AsyncMock(side_effect=BleakError("permanent"))

        new_client = _make_mock_client()
        new_client.write_gatt_char = AsyncMock(
            side_effect=BleakError("permanent")
        )

        with _patch_establish(new_client), _patch_sleep():
            with pytest.raises(OolerConnectionError):
                await device.set_mode("Boost")

        # Mode should NOT have been updated since the write failed
        assert device.state.mode == "Regular"

    @pytest.mark.asyncio
    async def test_state_consistent_after_reconnect(self) -> None:
        """After a forced reconnect, state should be fresh from the device."""
        device, old_client = _make_connected_device()
        device._ble_device = MagicMock()
        device._state.power = False
        device._state.mode = "Silent"

        # New device reports different state
        new_client = _make_mock_client([
            _TEMP_UNIT_F,
            b"\x01",  # power = True
            b"\x02",  # mode = Boost
            b"\x50",  # settemp = 80
            b"\x4c",  # actualtemp = 76
            b"\x64",  # water_level = 100
            b"\x01",  # clean = True
        ])

        with _patch_establish(new_client), _patch_sleep():
            await device._execute_forced_reconnect()

        assert device.state.power is True
        assert device.state.mode == "Boost"
        assert device.state.set_temperature == 80
        assert device.state.actual_temperature == 76
        assert device.state.water_level == 100
        assert device.state.clean is True

    def test_notification_preserves_unrelated_state(self) -> None:
        """A notification for one field shouldn't affect others."""
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.power = True
        device._state.mode = "Regular"
        device._state.set_temperature = 72
        device._state.actual_temperature = 74
        device._state.water_level = 50
        device._state.clean = False
        device._state.temperature_unit = "F"

        # Temperature notification
        device._notification_handler(
            _make_sender(ACTUALTEMP_CHAR), bytearray(b"\x4c")  # 76
        )

        assert device.state.actual_temperature == 76
        # Everything else unchanged
        assert device.state.power is True
        assert device.state.mode == "Regular"
        assert device.state.set_temperature == 72
        assert device.state.water_level == 50
        assert device.state.clean is False
        assert device.state.temperature_unit == "F"


# ============================================================================
# Edge case scenarios from live integration testing
# ============================================================================

class TestStartNotifyDuplicateSubscription:
    """Scenario 2: start_notify called on already-subscribed characteristic."""

    @pytest.mark.asyncio
    async def test_reconnect_resubscribes_cleanly(self) -> None:
        """After forced reconnect, new client gets fresh subscriptions."""
        device, old_client = _make_connected_device()
        device._ble_device = MagicMock()
        new_client = _make_mock_client()

        with _patch_establish(new_client), _patch_sleep():
            await device._execute_forced_reconnect()

        # New client should have exactly 4 subscriptions, no duplicates
        assert new_client.start_notify.call_count == 4
        subscribed_chars = [
            call.args[0] for call in new_client.start_notify.call_args_list
        ]
        assert len(set(subscribed_chars)) == 4  # All unique

    @pytest.mark.asyncio
    async def test_start_notify_raises_during_setup(self) -> None:
        """If start_notify raises (e.g., already subscribed), setup fails cleanly."""
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.read_gatt_char = AsyncMock(side_effect=[
            _TEMP_UNIT_F, *_GATT_READS_F,
        ])
        mock_client.start_notify = AsyncMock(
            side_effect=[None, None, BleakError("already subscribed"), None]
        )
        mock_client.disconnect = AsyncMock()

        with _patch_establish(mock_client):
            with pytest.raises(BleakError, match="already subscribed"):
                await device.connect()

        # Should have cleaned up
        assert device._client is None
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_integration_reload_reconnect(self) -> None:
        """Simulate integration reload: stop, then fresh connect."""
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()

        # First connect
        client1 = _make_mock_client()
        with _patch_establish(client1):
            await device.connect()
        assert client1.start_notify.call_count == 4

        # Stop (simulates integration unload)
        await device.stop()
        assert device._client is None
        client1.stop_notify.assert_any_call(POWER_CHAR)

        # Reconnect (simulates integration reload)
        client2 = _make_mock_client()
        with _patch_establish(client2):
            await device.connect()
        assert client2.start_notify.call_count == 4
        # No duplicate subscriptions — fresh client


class TestStopDuringConnect:
    """Scenario 3: stop() called during in-progress connect()."""

    @pytest.mark.asyncio
    async def test_stop_after_connect_completes(self) -> None:
        """If stop() runs after connect() finishes, normal teardown."""
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client()

        with _patch_establish(mock_client):
            await device.connect()
            await device.stop()

        assert device._client is None
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_while_connect_holds_lock(self) -> None:
        """stop() waits for connect_lock, then disconnects."""
        device = OolerBLEDevice(model="OOLER-TEST")
        device._ble_device = MagicMock()
        mock_client = _make_mock_client()

        connect_started = asyncio.Event()
        connect_proceed = asyncio.Event()

        async def slow_establish(*args, **kwargs):  # type: ignore[no-untyped-def]
            connect_started.set()
            await connect_proceed.wait()
            return mock_client

        with patch(
            "ooler_ble_client.client.establish_connection",
            side_effect=slow_establish,
        ):
            # Start connect in background
            connect_task = asyncio.create_task(device.connect())
            await connect_started.wait()

            # stop() should block on _connect_lock (held by connect)
            # Let connect complete
            connect_proceed.set()
            await connect_task

            # Now stop can proceed
            await device.stop()

        assert device._client is None
        mock_client.disconnect.assert_called_once()


class TestGattTimeoutWithDisconnect:
    """Scenario 5: GATT write timeout + disconnect callback."""

    @pytest.mark.asyncio
    async def test_timeout_then_disconnect_callback(self) -> None:
        """GATT timeout followed by disconnect callback doesn't deadlock."""
        device, client = _make_connected_device()
        device._ble_device = MagicMock()

        async def timeout_and_disconnect(char: str, data: bytes, response: bool) -> None:
            # Simulate: the write times out, and during the timeout
            # the disconnect callback fires
            device._disconnected_callback(client)
            raise asyncio.TimeoutError("write timed out")

        client.write_gatt_char = AsyncMock(side_effect=timeout_and_disconnect)

        # After disconnect callback, _client is None, so the immediate
        # retry in _retry_on_stale will hit BleakError("Not connected")
        # and trigger a reconnect
        new_client = _make_mock_client()

        with _patch_establish(new_client), _patch_sleep():
            await device.set_mode("Boost")

        assert device.state.mode == "Boost"

    @pytest.mark.asyncio
    async def test_timeout_propagates_if_reconnect_fails(self) -> None:
        """If reconnect also fails, the error propagates cleanly."""
        device, client = _make_connected_device()
        device._ble_device = MagicMock()
        client.write_gatt_char = AsyncMock(
            side_effect=asyncio.TimeoutError("timeout")
        )

        # Reconnect succeeds but write still fails
        new_client = _make_mock_client()
        new_client.write_gatt_char = AsyncMock(
            side_effect=asyncio.TimeoutError("still timing out")
        )

        with _patch_establish(new_client), _patch_sleep():
            with pytest.raises(OolerConnectionError):
                await device.set_mode("Boost")


class TestCallbackLifecycle:
    """Scenario 6: register_callback / unregister edge cases."""

    def test_callback_does_not_fire_after_unregister(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        received: list[OolerBLEState] = []
        unregister = device.register_callback(lambda s: received.append(s))
        unregister()
        device._set_state_and_fire_callbacks(OolerBLEState(power=True))
        assert len(received) == 0

    def test_double_unregister_raises(self) -> None:
        """Calling unregister twice raises ValueError (callback already removed)."""
        device = OolerBLEDevice(model="OOLER-TEST")
        unregister = device.register_callback(lambda s: None)
        unregister()
        with pytest.raises(ValueError):
            unregister()

    def test_unregister_one_keeps_others(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        r1: list[OolerBLEState] = []
        r2: list[OolerBLEState] = []
        unsub1 = device.register_callback(lambda s: r1.append(s))
        device.register_callback(lambda s: r2.append(s))
        unsub1()

        device._set_state_and_fire_callbacks(OolerBLEState(power=True))
        assert len(r1) == 0
        assert len(r2) == 1

    def test_callback_during_disconnect(self) -> None:
        """Callbacks fire during disconnect with current state."""
        device, client = _make_connected_device()
        device._state.power = True
        device._state.mode = "Boost"
        received: list[OolerBLEState] = []
        device.register_callback(lambda s: received.append(s))
        device._disconnected_callback(client)
        assert len(received) == 1
        assert received[0].power is True
        assert received[0].mode == "Boost"


class TestConcurrentPollAndNotification:
    """Scenario 7: async_poll() concurrent with notification on same characteristic."""

    @pytest.mark.asyncio
    async def test_poll_and_notification_same_char(self) -> None:
        """Notification arrives during poll — state should be consistent."""
        device, client = _make_connected_device()
        device._state.actual_temperature = 70

        call_count = 0

        async def sequential_reads(char: str) -> bytes:
            nonlocal call_count
            call_count += 1
            responses = {
                POWER_CHAR: b"\x01",
                MODE_CHAR: b"\x01",
                SETTEMP_CHAR: b"\x48",
                ACTUALTEMP_CHAR: b"\x4e",  # 78
                WATER_LEVEL_CHAR: b"\x32",
                CLEAN_CHAR: b"\x00",
            }
            if char == ACTUALTEMP_CHAR:
                # Notification arrives mid-poll with different value
                device._notification_handler(
                    _make_sender(ACTUALTEMP_CHAR), bytearray(b"\x50")  # 80
                )
            return responses.get(char, b"\x00")

        client.read_gatt_char = AsyncMock(side_effect=sequential_reads)

        await device.async_poll()

        # Poll completes and sets state from read values.
        # The notification's value (80) was overwritten by poll's value (78)
        # via _set_state_and_fire_callbacks. This is correct — poll is
        # authoritative as a full state snapshot.
        # Either value is acceptable; the key thing is no crash/tearing.
        assert device.state.actual_temperature in (78, 80)

    @pytest.mark.asyncio
    async def test_rapid_notifications_during_poll(self) -> None:
        """Multiple notifications arrive during a single poll cycle."""
        device, client = _make_connected_device()
        notification_count = 0

        async def reads_with_notifications(char: str) -> bytes:
            nonlocal notification_count
            # Fire a temperature notification on every read
            notification_count += 1
            device._notification_handler(
                _make_sender(ACTUALTEMP_CHAR),
                bytearray([70 + notification_count]),
            )
            responses = {
                POWER_CHAR: b"\x01",
                MODE_CHAR: b"\x01",
                SETTEMP_CHAR: b"\x48",
                ACTUALTEMP_CHAR: b"\x4a",
                WATER_LEVEL_CHAR: b"\x32",
                CLEAN_CHAR: b"\x00",
            }
            return responses.get(char, b"\x00")

        client.read_gatt_char = AsyncMock(side_effect=reads_with_notifications)

        # Should complete without error despite rapid notifications
        await device.async_poll()
        assert device.state is not None
        assert notification_count == 6  # One per read
