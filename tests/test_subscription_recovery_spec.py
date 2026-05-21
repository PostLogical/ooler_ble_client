"""Behavioral acceptance tests for the poll/state recovery chain.

These encode the six fault-injection scenarios from the ops-side recovery
spec (authored 2026-05-21 from a live Tawaret incident trace). They exercise
the *library*'s observable surface — the connection-event stream emitted by
``OolerBLEDevice`` — under injected faults:

    SUBSCRIPTION_MISMATCH -> SUBSCRIPTION_RECOVERED (Tier 1, re-subscribe)
                          -> FORCED_RECONNECT       (Tier 2, full reconnect)

Two places where the spec describes a system that does not exist in this
library, and how they are handled here:

1. **The "60s reconnect cooldown" (spec cases 5c, 6) does not exist.**
   It belonged to the deleted 0.11.0 gap watchdog (a 30s-tick background
   task that needed a wall-clock cooldown to avoid rapid re-fire). 0.11.1
   replaced that with the poll-driven detector in ``async_poll``: a forced
   reconnect can only fire on the *second consecutive* poll mismatch
   (the ``_tier1_pending`` gate). Cascade prevention is therefore
   *structural*, not temporal — temporal spacing comes from the consumer's
   poll cadence, not a library timer. The "no cascade" invariant is
   asserted here as "every forced reconnect is preceded by fresh poll
   evidence, and no two forced reconnects occur without an intervening
   mismatch" — see :class:`TestFuzzSoak`.

2. **``forced_reconnect_counts`` / ``last_subscription_mismatch`` are
   integration-side** (``custom_components/ooler/coordinator.py``), not part
   of this library. Their library equivalent is the connection-event stream,
   so e.g. ``forced_reconnect_counts["subscription_mismatch"] == 1`` is
   asserted here as "exactly one FORCED_RECONNECT event whose
   ``detail.trigger == 'subscription_mismatch'``."
"""
from __future__ import annotations

import random
from contextlib import AbstractContextManager
from typing import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakError

from ooler_ble_client import (
    ConnectionEvent,
    ConnectionEventType,
    OolerBLEDevice,
)
from ooler_ble_client.const import (
    ACTUALTEMP_CHAR,
    CLEAN_CHAR,
    MODE_CHAR,
    MODE_INT_TO_MODE_STATE,
    POWER_CHAR,
    SETTEMP_CHAR,
    WATER_LEVEL_CHAR,
)
from ooler_ble_client.models import OolerBLEState

ET = ConnectionEventType

_TEMP_UNIT_F = b"\x00"
_NOTIFY_CHARS = (POWER_CHAR, MODE_CHAR, SETTEMP_CHAR, ACTUALTEMP_CHAR)
# The four notify-backed fields, as their OolerBLEState attribute names.
_NOTIFY_FIELDS = ("power", "mode", "set_temperature", "actual_temperature")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _poll_reads(
    *,
    power: bool = True,
    mode: int = 1,  # Regular
    settemp_f: int = 72,
    actualtemp: int = 74,
    water_level: int = 50,
    clean: bool = False,
) -> list[bytes]:
    """The 6 GATT-read bytes for one ``_read_all_characteristics`` call,
    in the order the client reads them."""
    return [
        int(power).to_bytes(1, "little"),
        mode.to_bytes(1, "little"),
        settemp_f.to_bytes(1, "little"),
        actualtemp.to_bytes(1, "little"),
        water_level.to_bytes(1, "little"),
        int(clean).to_bytes(1, "little"),
    ]


def _make_connected_device() -> tuple[OolerBLEDevice, MagicMock]:
    """A device with a mock client attached and the detector already armed.

    Cached state is primed as if a first post-connect poll had run:
    power=on, Regular, 72°F set, 74 actual, display unit F.
    """
    device = OolerBLEDevice(model="OOLER-92106080603")  # the incident's Tawaret
    client = MagicMock()
    client.is_connected = True
    client.write_gatt_char = AsyncMock()
    client.read_gatt_char = AsyncMock()
    client.start_notify = AsyncMock()
    client.stop_notify = AsyncMock()
    client.disconnect = AsyncMock()
    device._client = client
    device._state.temperature_unit = "F"
    device._state.power = True
    device._state.mode = "Regular"
    device._state.set_temperature = 72
    device._state.actual_temperature = 74
    device._consistency_check_armed = True
    return device, client


def _patch_sleep() -> AbstractContextManager[AsyncMock]:
    return patch("asyncio.sleep", new_callable=AsyncMock)


def _of(events: list[ConnectionEvent], *types: ConnectionEventType) -> list[ConnectionEvent]:
    return [e for e in events if e.type in types]


def _health_events(events: list[ConnectionEvent]) -> list[ConnectionEvent]:
    return _of(
        events,
        ET.SUBSCRIPTION_MISMATCH,
        ET.SUBSCRIPTION_RECOVERED,
        ET.FORCED_RECONNECT,
    )


# ---------------------------------------------------------------------------
# Case 1 — Tier 1 succeeds (happy path)
# ---------------------------------------------------------------------------


class TestCase1Tier1HappyPath:
    """Inject cached != GATT divergence -> poll detects mismatch -> re-subscribe
    -> next poll clean. One mismatch, one recovered, NO forced reconnect;
    library equivalent of ``forced_reconnect_counts == {}``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", _NOTIFY_FIELDS)
    async def test_tier1_resolves_on_next_poll(self, field: str) -> None:
        device, client = _make_connected_device()

        # Poll 1 diverges on the parametrized field; poll 2 confirms recovery
        # (cache, now corrected by poll 1, matches the steady device value).
        if field == "power":
            reads = _poll_reads(power=False) + _poll_reads(power=False)
        elif field == "mode":
            reads = _poll_reads(mode=2) + _poll_reads(mode=2)  # Boost
        elif field == "set_temperature":
            reads = _poll_reads(settemp_f=68) + _poll_reads(settemp_f=68)
        else:  # actual_temperature
            reads = _poll_reads(actualtemp=76) + _poll_reads(actualtemp=76)
        client.read_gatt_char.side_effect = reads

        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)

        reconnect = AsyncMock()
        device._execute_forced_reconnect = reconnect  # type: ignore[method-assign]

        await device.async_poll()  # mismatch -> Tier 1
        await device.async_poll()  # clean -> confirms recovery

        mismatches = _of(events, ET.SUBSCRIPTION_MISMATCH)
        assert len(mismatches) == 1
        assert mismatches[0].detail == {"fields": [field]}
        assert len(_of(events, ET.SUBSCRIPTION_RECOVERED)) == 1
        # forced_reconnect_counts == {}  (library: zero FORCED_RECONNECT events)
        assert _of(events, ET.FORCED_RECONNECT) == []
        reconnect.assert_not_called()
        assert device._tier1_pending is False
        # Re-subscribe actually rebound all four characteristics.
        assert {c.args[0] for c in client.start_notify.call_args_list} == set(_NOTIFY_CHARS)


# ---------------------------------------------------------------------------
# Case 2 — false recovery -> Tier 2 (the 2026-05-21 incident, priority case)
# ---------------------------------------------------------------------------


class TestCase2FalseRecovery:
    """The incident: Tier 1 receives notifications immediately and reports
    SUCCESS (SUBSCRIPTION_RECOVERED), yet the *next* poll still diverges.

    Locks in the key property: escalation must not trust Tier 1's self-report
    — it acts on the next poll's evidence. This runs the REAL forced-reconnect
    path (establish_connection patched) so "after reconnect the next poll is
    clean" is exercised end-to-end, not mocked.
    """

    @pytest.mark.asyncio
    async def test_recovered_reported_yet_escalates_on_next_poll(self) -> None:
        device, old_client = _make_connected_device()
        device._ble_device = MagicMock()

        # Poll 1: actual=76 vs cached 74 -> Tier 1 re-subscribes (reports
        # success). Poll 2: actual=78 vs corrected cache 76 -> still diverging
        # -> Tier 2. (The subscription is "really" still broken, modeled by
        # the second poll continuing to diverge despite Tier 1's success.)
        old_client.read_gatt_char.side_effect = (
            _poll_reads(actualtemp=76) + _poll_reads(actualtemp=78)
        )

        # The reconnect establishes a fresh, healthy client whose reads settle
        # at the true current value (78): one temp-unit read, the internal
        # post-connect poll, then poll 3.
        new_client = MagicMock()
        new_client.is_connected = True
        new_client.write_gatt_char = AsyncMock()
        new_client.start_notify = AsyncMock()
        new_client.stop_notify = AsyncMock()
        new_client.disconnect = AsyncMock()
        new_client.read_gatt_char = AsyncMock(
            side_effect=[_TEMP_UNIT_F]
            + _poll_reads(actualtemp=78)
            + _poll_reads(actualtemp=78)
        )

        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)

        with patch(
            "ooler_ble_client.client.establish_connection",
            new_callable=AsyncMock,
            return_value=new_client,
        ), _patch_sleep():
            await device.async_poll()  # Poll 1 -> Tier 1 (reports recovered)
            assert device._tier1_pending is True
            # Tier 1 self-reported success before any escalation:
            assert [e.type for e in _health_events(events)] == [
                ET.SUBSCRIPTION_MISMATCH,
                ET.SUBSCRIPTION_RECOVERED,
            ]
            assert _of(events, ET.FORCED_RECONNECT) == []  # not yet

            await device.async_poll()  # Poll 2 -> Tier 2 (real reconnect)
            await device.async_poll()  # Poll 3 -> clean, on the new client

        # The ordered story of the incident, in events:
        types = [e.type for e in events]
        assert types == [
            ET.SUBSCRIPTION_MISMATCH,    # 03:12:33 poll
            ET.SUBSCRIPTION_RECOVERED,   # Tier 1 "re-subscribed" — reports success
            ET.SUBSCRIPTION_MISMATCH,    # 03:17:33 next poll STILL mismatches
            ET.FORCED_RECONNECT,         # 03:17:33 Tier 2
            ET.CONNECTED,                # 03:17:37 reconnected
        ]

        # forced_reconnect_counts["subscription_mismatch"] == 1
        forced = _of(events, ET.FORCED_RECONNECT)
        assert len(forced) == 1
        assert forced[0].detail == {"trigger": "subscription_mismatch"}

        # After reconnect the next poll is clean (no new health events from
        # poll 3) and the device now reflects the true value.
        assert device._client is new_client
        assert device._state.actual_temperature == 78
        assert device._tier1_pending is False
        assert device.is_connected is True


# ---------------------------------------------------------------------------
# Case 3 — poll-failure path (trigger == "poll_failure")
# ---------------------------------------------------------------------------


class TestCase3PollFailure:
    @pytest.mark.asyncio
    async def test_poll_raise_forces_reconnect_with_poll_failure_trigger(self) -> None:
        device, client = _make_connected_device()

        fresh = _poll_reads()
        n = {"calls": 0}

        async def read(_char: str) -> bytes:
            n["calls"] += 1
            if n["calls"] == 1:
                raise BleakError("transient read failure")
            return fresh[(n["calls"] - 2) % len(fresh)]

        client.read_gatt_char.side_effect = read

        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)

        # Fake reconnect that fires the event itself (so we assert on the
        # observable surface) and leaves the client usable for the retry read.
        async def fake_reconnect(trigger: str = "unknown") -> None:
            device._fire_connection_event(
                ET.FORCED_RECONNECT, detail={"trigger": trigger}
            )

        device._execute_forced_reconnect = fake_reconnect  # type: ignore[method-assign]

        await device.async_poll()

        forced = _of(events, ET.FORCED_RECONNECT)
        assert len(forced) == 1
        assert forced[0].detail == {"trigger": "poll_failure"}


# ---------------------------------------------------------------------------
# Case 4 — coast immunity (0.11.0 cascade regression guard)
# ---------------------------------------------------------------------------


class TestCase4CoastImmunity:
    """Long quiet window: polls succeed and return the SAME stable values,
    zero notifications. No mismatch, no forced reconnect, *regardless of
    duration*. This is exactly what the gap-watchdog got wrong."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stable",
        [
            dict(power=True, mode=1, settemp_f=72, actualtemp=72),
            dict(power=True, mode=0, settemp_f=68, actualtemp=68),  # Silent, cooled
            dict(power=False, mode=1, settemp_f=72, actualtemp=90),  # off, warm
            dict(power=True, mode=2, settemp_f=55, actualtemp=55),  # Boost, near LO
        ],
    )
    async def test_no_event_across_long_coast(self, stable: dict[str, int]) -> None:
        device, client = _make_connected_device()
        # Prime cache to the coast values so the very first poll already agrees.
        device._state.power = bool(stable["power"])
        device._state.mode = MODE_INT_TO_MODE_STATE[stable["mode"]]
        device._state.set_temperature = stable["settemp_f"]
        device._state.actual_temperature = stable["actualtemp"]

        one_poll = _poll_reads(
            power=bool(stable["power"]),
            mode=stable["mode"],
            settemp_f=stable["settemp_f"],
            actualtemp=stable["actualtemp"],
        )
        duration = 250  # "regardless of duration" — far past any finite threshold
        client.read_gatt_char.side_effect = [b for _ in range(duration) for b in one_poll]

        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)
        reconnect = AsyncMock()
        device._execute_forced_reconnect = reconnect  # type: ignore[method-assign]

        for _ in range(duration):
            await device.async_poll()

        assert _health_events(events) == []
        reconnect.assert_not_called()
        client.stop_notify.assert_not_called()
        client.start_notify.assert_not_called()
        assert device._tier1_pending is False


# ---------------------------------------------------------------------------
# Case 5 — fuzz / soak
# ---------------------------------------------------------------------------


def _byte_for(truth: dict[str, int], char: str) -> bytes:
    return {
        POWER_CHAR: int(truth["power"]).to_bytes(1, "little"),
        MODE_CHAR: truth["mode"].to_bytes(1, "little"),
        SETTEMP_CHAR: truth["settemp_f"].to_bytes(1, "little"),
        ACTUALTEMP_CHAR: truth["actualtemp"].to_bytes(1, "little"),
        WATER_LEVEL_CHAR: (50).to_bytes(1, "little"),
        CLEAN_CHAR: (0).to_bytes(1, "little"),
    }[char]


def _mutate(truth: dict[str, int], field: str, rng: random.Random) -> None:
    """Change one notify-backed field on the 'true' device to a new value."""
    if field == "power":
        truth["power"] = 0 if truth["power"] else 1
    elif field == "mode":
        truth["mode"] = rng.choice([m for m in (0, 1, 2) if m != truth["mode"]])
    elif field == "settemp_f":
        choices = [t for t in (60, 65, 68, 72, 75, 80) if t != truth["settemp_f"]]
        truth["settemp_f"] = rng.choice(choices)
    else:  # actualtemp
        delta = rng.choice([-3, -2, -1, 1, 2, 3])
        truth["actualtemp"] = max(50, min(100, truth["actualtemp"] + delta))
        if truth["actualtemp"] == 74:  # ensure a real change vs the primed cache
            truth["actualtemp"] = 75


_TRUTH_TO_STATE: dict[str, tuple[str, Callable[[int], object]]] = {
    "power": ("power", bool),
    "mode": ("mode", lambda v: MODE_INT_TO_MODE_STATE[v]),
    "settemp_f": ("set_temperature", lambda v: v),
    "actualtemp": ("actual_temperature", lambda v: v),
}
# Map a fuzz mutation field to the OolerBLEState attribute it would touch.
_FUZZ_FIELDS = ("power", "mode", "settemp_f", "actualtemp")


def _deliver(device: OolerBLEDevice, truth: dict[str, int], field: str) -> None:
    """Simulate a delivered notification updating the cache for one field."""
    attr, conv = _TRUTH_TO_STATE[field]
    setattr(device._state, attr, conv(truth[field]))


def _sync_cache(device: OolerBLEDevice, truth: dict[str, int]) -> None:
    """Simulate a fresh connection's poll syncing the whole cache to truth."""
    for field in _FUZZ_FIELDS:
        _deliver(device, truth, field)


class TestFuzzSoak:
    """Randomized {missed-notification on a random field; re-subscribe-takes?}.

    Invariants (the cooldown invariant 5c is re-expressed structurally — see
    the module docstring):
      (a) converges to a healthy, quiescent subscription;
      (b) no unbounded escalation: forced-reconnect count <= mismatch count,
          and every forced reconnect is preceded by fresh poll evidence;
      (c) structural no-cascade: no two forced reconnects without an
          intervening mismatch (replaces the deleted 60s wall-clock cooldown);
      (d) every escalation carries trigger == "subscription_mismatch" and is
          followed by a CONNECTED recovery.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("seed", [1, 7, 42, 99, 2026])
    async def test_invariants_hold_under_fuzz(self, seed: int) -> None:
        rng = random.Random(seed)
        device, client = _make_connected_device()
        truth = dict(power=1, mode=1, settemp_f=72, actualtemp=74)
        sub = {"healthy": True}

        async def read(char: str) -> bytes:
            return _byte_for(truth, char)

        client.read_gatt_char.side_effect = read

        # Tier 1 re-subscribe: every start_notify call here belongs to a
        # 4-char batch. Decide once per batch whether the rebind "takes".
        resub = {"i": 0, "takes": False}

        async def start_notify(_char: str, _handler: object) -> None:
            if resub["i"] % 4 == 0:
                resub["takes"] = rng.random() < 0.5
            resub["i"] += 1
            if resub["takes"]:
                sub["healthy"] = True

        client.start_notify.side_effect = start_notify

        # Tier 2: a successful forced reconnect always re-establishes a healthy
        # subscription and re-polls (mirrors the real path's inner poll). It
        # fires FORCED_RECONNECT then CONNECTED, like the real sequence.
        async def fake_reconnect(trigger: str = "unknown") -> None:
            device._fire_connection_event(
                ET.FORCED_RECONNECT, detail={"trigger": trigger}
            )
            sub["healthy"] = True
            device._tier1_pending = False
            _sync_cache(device, truth)
            device._consistency_check_armed = True
            device._fire_connection_event(ET.CONNECTED)

        device._execute_forced_reconnect = fake_reconnect  # type: ignore[method-assign]

        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)

        for _ in range(400):
            # Fault injection: the proxy silently drops the subscription
            # (is_connected stays True; notifications just stop arriving).
            # This is the real failure mode being recovered from.
            if sub["healthy"] and rng.random() < 0.15:
                sub["healthy"] = False
            if rng.random() < 0.6:
                field = rng.choice(_FUZZ_FIELDS)
                _mutate(truth, field, rng)
                if sub["healthy"]:
                    _deliver(device, truth, field)  # notification arrives
                # else: missed notification -> cache goes stale
            await device.async_poll()

        # (a) Convergence: heal the subscription, then settle to quiescence.
        sub["healthy"] = True
        await device.async_poll()  # clears any pending Tier 1
        n_before = len(events)
        await device.async_poll()  # a settled stream must emit nothing
        assert len(events) == n_before
        assert device._tier1_pending is False
        assert device._consistency_check_armed is True

        mismatches = _of(events, ET.SUBSCRIPTION_MISMATCH)
        forced = _of(events, ET.FORCED_RECONNECT)

        # (b) No unbounded escalation.
        assert len(forced) <= len(mismatches)

        # (d) Every escalation is mismatch-triggered and recovers.
        assert all(e.detail == {"trigger": "subscription_mismatch"} for e in forced)
        assert len(_of(events, ET.CONNECTED)) >= len(forced)

        # (b)/(c) Structural no-cascade: in the filtered mismatch/forced
        # stream, every forced reconnect is immediately preceded by a
        # mismatch, and no two forced reconnects are adjacent (each needs
        # fresh poll evidence — the replacement for the 60s cooldown).
        filtered = [
            e.type
            for e in events
            if e.type in (ET.SUBSCRIPTION_MISMATCH, ET.FORCED_RECONNECT)
        ]
        for i, et in enumerate(filtered):
            if et is ET.FORCED_RECONNECT:
                assert i > 0 and filtered[i - 1] is ET.SUBSCRIPTION_MISMATCH

    @pytest.mark.asyncio
    @pytest.mark.parametrize("seed", [3, 11, 555])
    async def test_healthy_stream_emits_zero_events(self, seed: int) -> None:
        """Invariant (d), positive side: a never-broken subscription with an
        active device produces zero subscription-health events."""
        rng = random.Random(seed)
        device, client = _make_connected_device()
        truth = dict(power=1, mode=1, settemp_f=72, actualtemp=74)

        async def read(char: str) -> bytes:
            return _byte_for(truth, char)

        client.read_gatt_char.side_effect = read

        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)
        reconnect = AsyncMock()
        device._execute_forced_reconnect = reconnect  # type: ignore[method-assign]

        for _ in range(200):
            if rng.random() < 0.7:
                field = rng.choice(_FUZZ_FIELDS)
                _mutate(truth, field, rng)
                _deliver(device, truth, field)  # always delivered (healthy)
            await device.async_poll()

        assert _health_events(events) == []
        reconnect.assert_not_called()
        client.start_notify.assert_not_called()


# ---------------------------------------------------------------------------
# Case 6 — forced reconnect itself fails
# ---------------------------------------------------------------------------


class TestCase6ForcedReconnectFails:
    """The spec frames this as "60s cooldown engaged, no immediate re-fire,
    recovery resumes after cooldown." There is no wall-clock cooldown in this
    design; the poll-driven equivalent is: a failing Tier 2 is swallowed (no
    crash, no wedge), and recovery resumes on a later poll cycle when fresh
    evidence re-drives the ladder. This asserts that equivalent.
    """

    @pytest.mark.asyncio
    async def test_failing_tier2_is_swallowed_and_recovery_resumes(self) -> None:
        device, client = _make_connected_device()

        # Continuous divergence: actual climbs 76,78,80,82 across four polls.
        client.read_gatt_char.side_effect = (
            _poll_reads(actualtemp=76)
            + _poll_reads(actualtemp=78)
            + _poll_reads(actualtemp=80)
            + _poll_reads(actualtemp=82)
        )

        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)

        attempts = {"n": 0}

        async def flaky_reconnect(trigger: str = "unknown") -> None:
            # Mirror the real path: the FORCED_RECONNECT event fires before the
            # attempt, and _tier1_pending is reset regardless of outcome.
            device._fire_connection_event(
                ET.FORCED_RECONNECT, detail={"trigger": trigger}
            )
            device._tier1_pending = False
            attempts["n"] += 1
            if attempts["n"] == 1:
                device._force_reconnecting = False
                raise BleakError("reconnect failed")
            device._fire_connection_event(ET.CONNECTED)  # second attempt succeeds

        device._execute_forced_reconnect = flaky_reconnect  # type: ignore[method-assign]

        # Poll 1: Tier 1. Poll 2: Tier 2 raises — must not propagate.
        await device.async_poll()
        await device.async_poll()  # no exception escapes here
        assert attempts["n"] == 1
        assert device.is_connected is True  # flag cleared, not wedged
        assert device._tier1_pending is False

        # Recovery resumes: continued divergence re-drives the ladder and the
        # next Tier 2 succeeds.
        await device.async_poll()  # Poll 3: fresh Tier 1
        assert device._tier1_pending is True
        await device.async_poll()  # Poll 4: Tier 2 again, succeeds
        assert attempts["n"] == 2

        forced = _of(events, ET.FORCED_RECONNECT)
        assert len(forced) == 2
        assert all(e.detail == {"trigger": "subscription_mismatch"} for e in forced)
        # The successful second attempt produced a recovery.
        assert len(_of(events, ET.CONNECTED)) == 1
