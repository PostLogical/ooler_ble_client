"""Tests for temperature conversion functions."""
from __future__ import annotations

import pytest

from ooler_ble_client.client import _f_to_c, _c_to_f


class TestFahrenheitToCelsius:
    def test_freezing_point(self) -> None:
        assert _f_to_c(32) == 0

    def test_boiling_point(self) -> None:
        assert _f_to_c(212) == 100

    def test_body_temp(self) -> None:
        assert _f_to_c(98) == 37

    def test_ooler_min(self) -> None:
        assert _f_to_c(55) == 13

    def test_ooler_max(self) -> None:
        assert _f_to_c(115) == 46

    def test_typical_ooler_setting(self) -> None:
        assert _f_to_c(72) == 22

    def test_rounding(self) -> None:
        # 70°F = 21.111...°C → rounds to 21
        assert _f_to_c(70) == 21


class TestCelsiusToFahrenheit:
    def test_freezing_point(self) -> None:
        assert _c_to_f(0) == 32

    def test_boiling_point(self) -> None:
        assert _c_to_f(100) == 212

    def test_typical_ooler_setting(self) -> None:
        assert _c_to_f(22) == 72

    def test_rounding(self) -> None:
        # 21°C = 69.8°F → rounds to 70
        assert _c_to_f(21) == 70


class TestRoundTrip:
    @pytest.mark.parametrize("temp_f", range(55, 116))
    def test_f_to_c_to_f_within_one_degree(self, temp_f: int) -> None:
        """Round-trip should be within 1°F due to rounding."""
        result = _c_to_f(_f_to_c(temp_f))
        assert abs(result - temp_f) <= 1

    @pytest.mark.parametrize("temp_c", range(13, 47))
    def test_c_to_f_to_c_within_one_degree(self, temp_c: int) -> None:
        """Round-trip should be within 1°C due to rounding."""
        result = _f_to_c(_c_to_f(temp_c))
        assert abs(result - temp_c) <= 1
