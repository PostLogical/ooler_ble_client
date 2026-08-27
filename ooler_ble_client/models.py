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
    #: powered off -- see :meth:`OolerBLEDevice.clear_stuck_setpoint_bug`.
    #: ``detail`` carries ``wanted`` (the setpoint before the device interfered),
    #: ``stuck_at`` (what it substituted) and ``repaired`` (whether the client
    #: acted; ``False`` when ``auto_clear_stuck_setpoint_bug`` is off, in which
    #: case nothing on the device was changed). Worth surfacing when
    #: ``repaired``: the repair briefly runs the pump and moves the setpoint,
    #: which is otherwise unexplained in a consumer's history.
    STUCK_SETPOINT_DETECTED = "stuck_setpoint_detected"
    #: The repair has been applied :data:`MAX_STUCK_SETPOINT_REPAIRS` times in a
    #: row and the device keeps substituting the setpoint, so the client has
    #: stopped trying. It keeps watching and will resume if the device settles.
    #: ``detail`` carries ``consecutive``.
    STUCK_SETPOINT_UNFIXABLE = "stuck_setpoint_unfixable"
    #: A setpoint survived a full watch window with the device off after an
    #: earlier repair, so the device is behaving again. Fires on the transition
    #: only, not on every healthy power-off, so a consumer that raised something
    #: user-facing on :attr:`STUCK_SETPOINT_UNFIXABLE` has an edge to clear it
    #: on. ``detail`` carries ``after`` (repairs it took).
    STUCK_SETPOINT_RECOVERED = "stuck_setpoint_recovered"


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
