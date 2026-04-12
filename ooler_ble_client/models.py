from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

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


class ConnectionEventType(Enum):
    """Kinds of connectivity events emitted by :class:`OolerBLEDevice`."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    NOTIFY_STALL = "notify_stall"
    FORCED_RECONNECT = "forced_reconnect"


@dataclass(frozen=True)
class ConnectionEvent:
    """A connectivity event on an :class:`OolerBLEDevice`.

    ``timestamp`` is a ``time.monotonic()`` value. ``detail`` carries
    event-specific metadata (see :class:`ConnectionEventType` for the
    payload contract).
    """

    type: ConnectionEventType
    timestamp: float
    detail: dict[str, Any] | None = None
