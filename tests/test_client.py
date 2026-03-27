"""Tests for OolerBLEDevice client logic."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from ooler_ble_client import OolerBLEDevice, OolerBLEState
from ooler_ble_client.const import MODE_INT_TO_MODE_STATE


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


class TestNotificationHandler:
    def _make_sender(self, uuid: str) -> MagicMock:
        sender = MagicMock()
        sender.uuid = uuid
        return sender

    def test_power_on(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        from ooler_ble_client.const import POWER_CHAR

        device._notification_handler(
            self._make_sender(POWER_CHAR), bytearray(b"\x01")
        )
        assert device.state.power is True

    def test_power_off_clears_clean(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.clean = True
        from ooler_ble_client.const import POWER_CHAR

        device._notification_handler(
            self._make_sender(POWER_CHAR), bytearray(b"\x00")
        )
        assert device.state.power is False
        assert device.state.clean is False

    def test_mode(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        from ooler_ble_client.const import MODE_CHAR

        device._notification_handler(
            self._make_sender(MODE_CHAR), bytearray(b"\x02")
        )
        assert device.state.mode == "Boost"

    def test_unknown_mode_ignored(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.mode = "Regular"
        from ooler_ble_client.const import MODE_CHAR

        device._notification_handler(
            self._make_sender(MODE_CHAR), bytearray(b"\x09")
        )
        # Should keep previous mode
        assert device.state.mode == "Regular"

    def test_settemp_fahrenheit(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.temperature_unit = "F"
        from ooler_ble_client.const import SETTEMP_CHAR

        device._notification_handler(
            self._make_sender(SETTEMP_CHAR), bytearray(b"\x48")  # 72
        )
        assert device.state.set_temperature == 72

    def test_settemp_celsius_conversion(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.temperature_unit = "C"
        from ooler_ble_client.const import SETTEMP_CHAR

        device._notification_handler(
            self._make_sender(SETTEMP_CHAR), bytearray(b"\x48")  # 72°F = 22°C
        )
        assert device.state.set_temperature == 22

    def test_actual_temperature(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        from ooler_ble_client.const import ACTUALTEMP_CHAR

        device._notification_handler(
            self._make_sender(ACTUALTEMP_CHAR), bytearray(b"\x4a")  # 74
        )
        assert device.state.actual_temperature == 74

    def test_no_callback_on_unchanged_value(self) -> None:
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.actual_temperature = 74
        received: list[OolerBLEState] = []
        device.register_callback(lambda s: received.append(s))
        from ooler_ble_client.const import ACTUALTEMP_CHAR

        device._notification_handler(
            self._make_sender(ACTUALTEMP_CHAR), bytearray(b"\x4a")  # 74 again
        )
        assert len(received) == 0

    def test_exception_in_handler_does_not_propagate(self) -> None:
        """Notification handler exceptions should be caught, not propagated."""
        device = OolerBLEDevice(model="OOLER-TEST")
        sender = MagicMock()
        sender.uuid = "unknown-uuid"
        # Should not raise even with weird data
        device._notification_handler(sender, bytearray(b"\xff\xff\xff"))


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
        device = OolerBLEDevice(model="OOLER-TEST")
        device._state.temperature_unit = "F"
        # Mock connection so we hit the validation
        device._client = MagicMock()
        device._client.is_connected = True
        with pytest.raises(ValueError, match="out of range"):
            await device.set_temperature(200)
