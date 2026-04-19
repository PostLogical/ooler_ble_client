"""Tests for models and state management."""
from __future__ import annotations

from ooler_ble_client import OolerBLEState, OolerConnectionError


class TestOolerBLEState:
    def test_default_state(self) -> None:
        state = OolerBLEState()
        assert state.power is None
        assert state.mode is None
        assert state.set_temperature is None
        assert state.actual_temperature is None
        assert state.water_level is None
        assert state.clean is None
        assert state.temperature_unit is None

    def test_state_with_values(self) -> None:
        state = OolerBLEState(
            power=True,
            mode="Regular",
            set_temperature=72,
            actual_temperature=74,
            water_level=50,
            clean=False,
            temperature_unit="F",
        )
        assert state.power is True
        assert state.mode == "Regular"
        assert state.set_temperature == 72
        assert state.actual_temperature == 74
        assert state.water_level == 50
        assert state.clean is False
        assert state.temperature_unit == "F"

    def test_state_equality(self) -> None:
        state1 = OolerBLEState(power=True, mode="Silent")
        state2 = OolerBLEState(power=True, mode="Silent")
        assert state1 == state2

    def test_state_inequality(self) -> None:
        state1 = OolerBLEState(power=True, mode="Silent")
        state2 = OolerBLEState(power=True, mode="Boost")
        assert state1 != state2


class TestOolerConnectionError:
    def test_inherits_from_bleak_error(self) -> None:
        from bleak.exc import BleakError

        err = OolerConnectionError("test")
        assert isinstance(err, BleakError)

    def test_message(self) -> None:
        err = OolerConnectionError("connection lost")
        assert str(err) == "connection lost"
