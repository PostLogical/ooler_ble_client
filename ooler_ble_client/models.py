from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from bleak.exc import BleakError

OolerMode = Literal["Silent", "Regular", "Boost"]
TemperatureUnit = Literal["C", "F"]


class OolerConnectionError(BleakError):
    """Raised when all retry attempts are exhausted."""


@dataclass
class OolerBLEState:
    power: bool | None = None
    mode: OolerMode | None = None
    set_temperature: int | None = None
    actual_temperature: int | None = None
    water_level: int | None = None
    clean: bool | None = None
    temperature_unit: TemperatureUnit | None = None
