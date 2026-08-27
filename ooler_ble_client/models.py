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
    SUBSCRIPTION_MISMATCH = "subscription_mismatch"
    SUBSCRIPTION_RECOVERED = "subscription_recovered"
    FORCED_RECONNECT = "forced_reconnect"
    #: The device was caught replacing the setpoint on its own after being
    #: powered off, and has been repaired -- see
    #: :meth:`OolerBLEDevice.clear_stuck_setpoint_bug`. Emitted because the
    #: repair briefly runs the pump and moves the setpoint, which is otherwise
    #: unexplained in a consumer's history. ``detail`` carries ``wanted`` (the
    #: setpoint before the device interfered) and ``stuck_at`` (what it
    #: substituted).
    STUCK_SETPOINT_REPAIRED = "stuck_setpoint_repaired"
    #: The repair has been applied :data:`MAX_STUCK_SETPOINT_REPAIRS` times in a
    #: row and the device keeps substituting the setpoint, so the client has
    #: stopped trying. It keeps watching and will resume if the device settles.
    #: ``detail`` carries ``consecutive``.
    STUCK_SETPOINT_UNFIXABLE = "stuck_setpoint_unfixable"


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
