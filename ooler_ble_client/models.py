from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from bleak.exc import BleakError

OolerMode = Literal["Silent", "Regular", "Boost"]
TemperatureUnit = Literal["C", "F"]


class OolerConnectionError(BleakError):
    """Raised when all retry attempts are exhausted."""


class DeviceOffError(Exception):
    """Raised when a setting cannot be applied because the device is off.

    The device silently drops writes to every characteristic while powered
    off, so a setter that cannot write says so rather than pretending. Turn
    the device on first.

    Deliberately not a :class:`~bleak.exc.BleakError`: nothing went wrong with
    the transport, and inheriting one would let broad ``except BleakError``
    handlers swallow it.
    """


@dataclass
class OolerBLEState:
    power: bool | None = None
    mode: OolerMode | None = None
    set_temperature: int | None = None
    actual_temperature: int | None = None
    water_level: int | None = None
    #: A deep clean -- the ~45 minute cycle a person starts, and the one that
    #: arms the setpoint override. See CLEAN_TEMP_F in const.py for how it is
    #: told apart from the UV cycle below, which shares its characteristic.
    deep_clean: bool | None = None
    #: The device's own water-treatment cycle: roughly every hour of runtime,
    #: 3-5 minutes, not startable or stoppable by anyone. See UV_CLEAN_MAX_
    #: SECONDS in const.py.
    uv_clean: bool | None = None
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
    #: A poll found the CLEAN characteristic asserted on a device that is
    #: powered off. Nothing runs on a powered-off unit, so neither
    #: :attr:`OolerBLEState.deep_clean` nor :attr:`OolerBLEState.uv_clean` is
    #: set from it -- this event is how the reading stays visible. Observed
    #: once, latched for ~9 hours, cause unknown; see CLEAN_TEMP_F in const.py.
    #: ``detail`` carries ``set_temperature``, so a later reader can tell
    #: whether the setpoint looked forced at the time.
    #:
    #: Fires on every poll while the condition holds -- ~108 times over a
    #: nine-hour latch at a five-minute cadence -- so count these rather than
    #: accumulating them.
    CLEAN_ASSERTED_WHILE_OFF = "clean_asserted_while_off"


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
