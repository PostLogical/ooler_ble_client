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
    #: The device was caught overriding the setpoint after being powered off,
    #: and a fix was applied -- see
    #: :meth:`OolerBLEDevice.fix_setpoint_override`. ``detail`` carries
    #: ``overrode`` (what the setpoint was), ``overrode_with`` (what the device
    #: replaced it with), ``restored`` (what was written back, or ``None`` when
    #: the device's own stored value was kept) and ``attempt``. Worth logging:
    #: the fix briefly runs the pump and moves the setpoint to
    #: :data:`CLEAN_TEMP_F`, which is otherwise unexplained in a consumer's
    #: history. It does not need a person's attention.
    SETPOINT_OVERRIDE_FIXED = "setpoint_override_fixed"
    #: Every entry in :data:`FIX_CLEAN_SECONDS` has been tried and the device
    #: keeps overriding the setpoint, so the client has stopped trying. Unlike
    #: :attr:`SETPOINT_OVERRIDE_FIXED` this one does need a person: their
    #: temperature is being discarded and nothing will correct it.
    #: ``detail`` carries ``attempts``.
    SETPOINT_OVERRIDE_UNFIXABLE = "setpoint_override_unfixable"


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
