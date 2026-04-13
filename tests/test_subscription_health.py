"""Tests for the poll/state consistency detector and connection-event channel.

0.11.1 replaced the 0.11.0 gap-based notify watchdog with a detector that
runs inside ``async_poll``: after each successful GATT read, compare the
fresh values against cached state on the four notify-backed fields
(power, mode, set_temperature, actual_temperature). A disagreement is
positive evidence that a notification was missed, so the recovery ladder
re-subscribes in place (Tier 1) and escalates to a full forced reconnect
only if the next poll still shows a mismatch (Tier 2).
"""
from __future__ import annotations

from typing import Any
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
    MODE_CHAR,
    POWER_CHAR,
    SETTEMP_CHAR,
    _SHUTDOWN_ERROR_MAX_ATTEMPTS,
)


_TEMP_UNIT_F = b"\x00"
_GATT_READS_F = [
    b"\x01",  # power = True
    b"\x01",  # mode = Regular
    b"\x48",  # settemp = 72°F
    b"\x4a",  # actualtemp = 74
    b"\x32",  # water_level = 50
    b"\x00",  # clean = False
]


def _make_mock_client(reads: list[bytes] | None = None) -> MagicMock:
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(
        side_effect=reads or [_TEMP_UNIT_F] + _GATT_READS_F
    )
    client.write_gatt_char = AsyncMock()
    client.start_notify = AsyncMock()
    client.stop_notify = AsyncMock()
    client.disconnect = AsyncMock()
    return client


def _patch_establish(mock_client: MagicMock):  # type: ignore[no-untyped-def]
    return patch(
        "ooler_ble_client.client.establish_connection",
        new_callable=AsyncMock,
        return_value=mock_client,
    )


def _patch_sleep():  # type: ignore[no-untyped-def]
    return patch("asyncio.sleep", new_callable=AsyncMock)


def _make_connected_powered_device(
    *, power: bool = True
) -> tuple[OolerBLEDevice, MagicMock]:
    """Return a device with a mock client attached and cached state populated.

    Cached state is primed as if the connection had already gone through a
    first internal poll, so the consistency detector is armed and
    subsequent polls will participate in the check.
    """
    device = OolerBLEDevice(model="OOLER-WD")
    client = MagicMock()
    client.is_connected = True
    client.write_gatt_char = AsyncMock()
    client.read_gatt_char = AsyncMock()
    client.start_notify = AsyncMock()
    client.stop_notify = AsyncMock()
    client.disconnect = AsyncMock()
    device._client = client
    device._state.temperature_unit = "F"
    device._state.mode = "Regular"
    device._state.set_temperature = 72
    device._state.actual_temperature = 74
    device._state.power = power
    device._consistency_check_armed = True
    return device, client


def _poll_reads(
    *,
    power: bool = True,
    mode: int = 1,  # Regular
    settemp_f: int = 72,
    actualtemp: int = 74,
    water_level: int = 50,
    clean: bool = False,
) -> list[bytes]:
    """Build the 6 GATT-read bytes for one _read_all_characteristics call."""
    return [
        int(power).to_bytes(1, "little"),
        mode.to_bytes(1, "little"),
        settemp_f.to_bytes(1, "little"),
        actualtemp.to_bytes(1, "little"),
        water_level.to_bytes(1, "little"),
        int(clean).to_bytes(1, "little"),
    ]


# ---------------------------------------------------------------------------
# Monotonic clock injection
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# _check_notify_consistency() pure comparison
# ---------------------------------------------------------------------------


class TestCheckNotifyConsistency:
    def test_all_fields_match_returns_empty(self) -> None:
        device, _ = _make_connected_powered_device()
        # Fresh state identical to cached
        from ooler_ble_client.models import OolerBLEState

        fresh = OolerBLEState(
            power=True,
            mode="Regular",
            set_temperature=72,
            actual_temperature=74,
            water_level=50,
            clean=False,
            temperature_unit="F",
        )
        assert device._check_notify_consistency(fresh) == set()

    def test_actualtemp_mismatch_returned(self) -> None:
        from ooler_ble_client.models import OolerBLEState

        device, _ = _make_connected_powered_device()
        fresh = OolerBLEState(
            power=True,
            mode="Regular",
            set_temperature=72,
            actual_temperature=76,  # differs from cached 74
            water_level=50,
            clean=False,
            temperature_unit="F",
        )
        assert device._check_notify_consistency(fresh) == {"actual_temperature"}

    def test_multiple_mismatches_returned(self) -> None:
        from ooler_ble_client.models import OolerBLEState

        device, _ = _make_connected_powered_device()
        fresh = OolerBLEState(
            power=False,  # differs
            mode="Boost",  # differs
            set_temperature=72,
            actual_temperature=74,
            water_level=50,
            clean=False,
            temperature_unit="F",
        )
        assert device._check_notify_consistency(fresh) == {"power", "mode"}

    def test_none_cached_fields_are_skipped(self) -> None:
        """Fields with None in the cache (e.g. first poll after instantiation
        before any connect) have no baseline and must not be flagged."""
        from ooler_ble_client.models import OolerBLEState

        device = OolerBLEDevice(model="OOLER-WD")
        # device._state starts as all None (except defaults from dataclass)
        fresh = OolerBLEState(
            power=True,
            mode="Regular",
            set_temperature=72,
            actual_temperature=74,
            water_level=50,
            clean=False,
            temperature_unit="F",
        )
        assert device._check_notify_consistency(fresh) == set()


# ---------------------------------------------------------------------------
# async_poll() consistency detector end-to-end
# ---------------------------------------------------------------------------


class TestAsyncPollConsistency:
    @pytest.mark.asyncio
    async def test_match_fires_no_events_and_no_resubscribe(self) -> None:
        device, client = _make_connected_powered_device()
        client.read_gatt_char.side_effect = _poll_reads()

        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)

        await device.async_poll()

        # No subscription-health events
        assert all(
            e.type
            not in (
                ConnectionEventType.SUBSCRIPTION_MISMATCH,
                ConnectionEventType.SUBSCRIPTION_RECOVERED,
                ConnectionEventType.FORCED_RECONNECT,
            )
            for e in events
        )
        # No re-subscribe attempted
        client.stop_notify.assert_not_called()
        client.start_notify.assert_not_called()
        # Detector remains armed for the next poll
        assert device._consistency_check_armed is True
        assert device._tier1_pending is False

    @pytest.mark.asyncio
    async def test_mismatch_tier1_resubscribes_and_fires_events(self) -> None:
        device, client = _make_connected_powered_device()
        # Poll reveals ACTUALTEMP=76 vs cached 74 — missed notification.
        client.read_gatt_char.side_effect = _poll_reads(actualtemp=76)

        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)

        # Spy on _execute_forced_reconnect to prove Tier 2 did NOT run.
        reconnect_mock = AsyncMock()
        device._execute_forced_reconnect = reconnect_mock  # type: ignore[method-assign]

        await device.async_poll()

        # SUBSCRIPTION_MISMATCH fired with sorted fields list
        mismatches = [
            e for e in events if e.type == ConnectionEventType.SUBSCRIPTION_MISMATCH
        ]
        assert len(mismatches) == 1
        assert mismatches[0].detail == {"fields": ["actual_temperature"]}

        # SUBSCRIPTION_RECOVERED fired after successful re-subscribe
        recovered = [
            e for e in events if e.type == ConnectionEventType.SUBSCRIPTION_RECOVERED
        ]
        assert len(recovered) == 1

        # No FORCED_RECONNECT — Tier 1 only
        assert all(
            e.type != ConnectionEventType.FORCED_RECONNECT for e in events
        )
        reconnect_mock.assert_not_called()

        # Re-subscribe: stop_notify + start_notify called for each of the 4 chars
        assert client.stop_notify.call_count == 4
        assert client.start_notify.call_count == 4
        expected_chars = {POWER_CHAR, MODE_CHAR, SETTEMP_CHAR, ACTUALTEMP_CHAR}
        stop_chars = {call.args[0] for call in client.stop_notify.call_args_list}
        start_chars = {call.args[0] for call in client.start_notify.call_args_list}
        assert stop_chars == expected_chars
        assert start_chars == expected_chars

        # Tier 1 flagged as pending for next poll to confirm
        assert device._tier1_pending is True
        # Cached state was refreshed with the fresh poll values
        assert device._state.actual_temperature == 76

    @pytest.mark.asyncio
    async def test_tier2_escalation_on_consecutive_mismatches(self) -> None:
        device, client = _make_connected_powered_device()
        # First poll: mismatch → Tier 1. Second poll: still mismatch → Tier 2.
        # Two polls = 12 byte reads total.
        client.read_gatt_char.side_effect = (
            _poll_reads(actualtemp=76) + _poll_reads(actualtemp=78)
        )

        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)

        reconnect_mock = AsyncMock()
        device._execute_forced_reconnect = reconnect_mock  # type: ignore[method-assign]

        # Poll 1: Tier 1 re-subscribe
        await device.async_poll()
        assert device._tier1_pending is True
        reconnect_mock.assert_not_called()

        # Poll 2: still a mismatch (cache is now 76, fresh is 78) → Tier 2
        await device.async_poll()
        reconnect_mock.assert_awaited_once_with(trigger="subscription_mismatch")

    @pytest.mark.asyncio
    async def test_tier1_clears_after_clean_poll(self) -> None:
        """A matching poll after Tier 1 proves recovery worked; the flag
        clears so the next mismatch is treated as a fresh Tier 1."""
        device, client = _make_connected_powered_device()
        # Poll 1: mismatch → Tier 1, cached state now actualtemp=76.
        # Poll 2: matches (cached 76, fresh 76) → flag clears.
        client.read_gatt_char.side_effect = (
            _poll_reads(actualtemp=76) + _poll_reads(actualtemp=76)
        )

        reconnect_mock = AsyncMock()
        device._execute_forced_reconnect = reconnect_mock  # type: ignore[method-assign]

        await device.async_poll()
        assert device._tier1_pending is True
        await device.async_poll()
        assert device._tier1_pending is False
        reconnect_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_notify_raises_escalates_to_tier2(self) -> None:
        device, client = _make_connected_powered_device()
        client.read_gatt_char.side_effect = _poll_reads(actualtemp=76)
        # stop_notify raises → helper falls through to Tier 2.
        # (Individual stop_notify failures are tolerated; only start_notify
        # failures escalate. We use start_notify as the raising call to make
        # the escalation unambiguous.)
        client.start_notify.side_effect = BleakError("CCCD write failed")

        reconnect_mock = AsyncMock()
        device._execute_forced_reconnect = reconnect_mock  # type: ignore[method-assign]

        await device.async_poll()
        reconnect_mock.assert_awaited_once_with(trigger="subscription_mismatch")

    @pytest.mark.asyncio
    async def test_coast_ten_polls_no_false_positive(self) -> None:
        """Regression guard for the 0.11.0 cascade bug: during a long coast
        period where ACTUALTEMP == SETTEMP and nothing changes, ten
        consecutive polls must not fire a single subscription event."""
        device, client = _make_connected_powered_device()
        # Cached state: actualtemp=74. Coast means all 10 polls read 74.
        # But let's make cached state match the coast value first.
        device._state.actual_temperature = 72
        device._state.set_temperature = 72
        client.read_gatt_char.side_effect = sum(
            (_poll_reads(actualtemp=72, settemp_f=72) for _ in range(10)),
            [],
        )

        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)

        reconnect_mock = AsyncMock()
        device._execute_forced_reconnect = reconnect_mock  # type: ignore[method-assign]

        for _ in range(10):
            await device.async_poll()

        # Zero subscription-health events across all 10 polls
        assert all(
            e.type
            not in (
                ConnectionEventType.SUBSCRIPTION_MISMATCH,
                ConnectionEventType.SUBSCRIPTION_RECOVERED,
                ConnectionEventType.FORCED_RECONNECT,
            )
            for e in events
        )
        reconnect_mock.assert_not_called()
        client.stop_notify.assert_not_called()
        assert device._tier1_pending is False

    @pytest.mark.asyncio
    async def test_first_poll_after_instantiation_skipped(self) -> None:
        """Before the consistency detector is armed (e.g. first poll inside
        _ensure_connected after a fresh connect), mismatches must not fire."""
        device, client = _make_connected_powered_device()
        device._consistency_check_armed = False
        client.read_gatt_char.side_effect = _poll_reads(actualtemp=76)

        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)

        reconnect_mock = AsyncMock()
        device._execute_forced_reconnect = reconnect_mock  # type: ignore[method-assign]

        await device.async_poll()

        assert all(
            e.type != ConnectionEventType.SUBSCRIPTION_MISMATCH for e in events
        )
        reconnect_mock.assert_not_called()
        # Poll completed and armed the detector for next time.
        assert device._consistency_check_armed is True

    @pytest.mark.asyncio
    async def test_poll_failure_still_force_reconnects(self) -> None:
        """Regression guard: the existing `poll_failure` retry path must
        survive the watchdog removal unchanged."""
        device, client = _make_connected_powered_device()
        # First read raises → _execute_forced_reconnect → retry
        call_count = {"n": 0}
        fresh_reads = _poll_reads()

        async def read_side_effect(char: str) -> bytes:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise BleakError("transient")
            # Subsequent reads return fresh values one by one
            return fresh_reads[(call_count["n"] - 2) % len(fresh_reads)]

        client.read_gatt_char.side_effect = read_side_effect

        reconnect_mock = AsyncMock()
        device._execute_forced_reconnect = reconnect_mock  # type: ignore[method-assign]

        await device.async_poll()
        reconnect_mock.assert_awaited_once_with(trigger="poll_failure")


# ---------------------------------------------------------------------------
# API surface: NOTIFY_STALL is gone; new enum members exist
# ---------------------------------------------------------------------------


class TestConnectionEventTypeSurface:
    def test_notify_stall_enum_member_removed(self) -> None:
        with pytest.raises(AttributeError):
            _ = ConnectionEventType.NOTIFY_STALL  # type: ignore[attr-defined]

    def test_subscription_mismatch_enum_member_exists(self) -> None:
        assert ConnectionEventType.SUBSCRIPTION_MISMATCH.value == "subscription_mismatch"

    def test_subscription_recovered_enum_member_exists(self) -> None:
        assert ConnectionEventType.SUBSCRIPTION_RECOVERED.value == "subscription_recovered"


# ---------------------------------------------------------------------------
# No background watchdog task is started on connect
# ---------------------------------------------------------------------------


class TestNoWatchdogTask:
    @pytest.mark.asyncio
    async def test_no_watchdog_task_attribute_after_connect(self) -> None:
        device = OolerBLEDevice(model="OOLER-WD")
        device._ble_device = MagicMock()
        with _patch_establish(_make_mock_client()):
            await device.connect()
        # The field was removed entirely — accessing it raises AttributeError.
        assert not hasattr(device, "_watchdog_task")
        assert not hasattr(device, "_last_notification_monotonic")
        assert not hasattr(device, "_force_reconnect_cooldown_until")


# ---------------------------------------------------------------------------
# Flap suppression: is_connected during forced reconnect (unchanged from 0.11.0)
# ---------------------------------------------------------------------------


class TestFlapSuppression:
    @pytest.mark.asyncio
    async def test_is_connected_stays_true_during_forced_reconnect(self) -> None:
        device, old_client = _make_connected_powered_device()
        device._ble_device = MagicMock()

        seen: list[bool] = []

        async def capture_is_connected(*args: Any, **kwargs: Any) -> MagicMock:
            seen.append(device.is_connected)
            return _make_mock_client()

        with patch(
            "ooler_ble_client.client.establish_connection",
            side_effect=capture_is_connected,
        ), _patch_sleep():
            await device._execute_forced_reconnect(trigger="subscription_mismatch")

        assert seen == [True]
        assert device.is_connected is True
        assert device._force_reconnecting is False

    @pytest.mark.asyncio
    async def test_forced_reconnect_failure_clears_flag(self) -> None:
        device, _ = _make_connected_powered_device()
        device._ble_device = MagicMock()
        with patch(
            "ooler_ble_client.client.establish_connection",
            new_callable=AsyncMock,
            side_effect=BleakError("nope"),
        ), _patch_sleep():
            with pytest.raises(BleakError):
                await device._execute_forced_reconnect(trigger="subscription_mismatch")
        assert device._force_reconnecting is False
        assert device.is_connected is False

    @pytest.mark.asyncio
    async def test_disconnected_callback_suppressed_during_forced_reconnect(
        self,
    ) -> None:
        device, old_client = _make_connected_powered_device()
        state_events: list[object] = []
        conn_events: list[ConnectionEvent] = []
        device.register_callback(lambda s: state_events.append(s))
        device.register_connection_event_callback(conn_events.append)

        device._force_reconnecting = True
        device._disconnected_callback(old_client)
        assert state_events == []
        assert all(e.type != ConnectionEventType.DISCONNECTED for e in conn_events)


# ---------------------------------------------------------------------------
# Connection event channel
# ---------------------------------------------------------------------------


class TestConnectionEventChannel:
    @pytest.mark.asyncio
    async def test_connected_event_on_connect(self) -> None:
        device = OolerBLEDevice(model="OOLER-WD")
        device._ble_device = MagicMock()
        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)

        with _patch_establish(_make_mock_client()):
            await device.connect()

        assert any(e.type == ConnectionEventType.CONNECTED for e in events)

    @pytest.mark.asyncio
    async def test_disconnected_event_on_unexpected_disconnect(self) -> None:
        device, client = _make_connected_powered_device()
        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)
        device._disconnected_callback(client)
        types = [e.type for e in events]
        assert ConnectionEventType.DISCONNECTED in types

    @pytest.mark.asyncio
    async def test_no_disconnected_event_on_expected_disconnect(self) -> None:
        device, client = _make_connected_powered_device()
        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)
        device._expected_disconnect = True
        device._disconnected_callback(client)
        assert all(
            e.type != ConnectionEventType.DISCONNECTED for e in events
        )

    @pytest.mark.asyncio
    async def test_forced_reconnect_event_with_trigger(self) -> None:
        device, _ = _make_connected_powered_device()
        device._ble_device = MagicMock()
        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)

        with _patch_establish(_make_mock_client()), _patch_sleep():
            await device._execute_forced_reconnect(trigger="subscription_mismatch")

        forced = [e for e in events if e.type == ConnectionEventType.FORCED_RECONNECT]
        assert len(forced) == 1
        assert forced[0].detail == {"trigger": "subscription_mismatch"}

    def test_unregister_removes_callback(self) -> None:
        device = OolerBLEDevice(model="OOLER-WD")
        events: list[ConnectionEvent] = []
        unregister = device.register_connection_event_callback(events.append)
        unregister()
        device._fire_connection_event(ConnectionEventType.CONNECTED)
        assert events == []

    def test_double_unregister_is_noop(self) -> None:
        device = OolerBLEDevice(model="OOLER-WD")
        unregister = device.register_connection_event_callback(lambda e: None)
        unregister()
        unregister()

    def test_callback_exception_isolation(self) -> None:
        device = OolerBLEDevice(model="OOLER-WD")
        calls: list[int] = []

        def bad(event: ConnectionEvent) -> None:
            raise RuntimeError("boom")

        def good(event: ConnectionEvent) -> None:
            calls.append(1)

        device.register_connection_event_callback(bad)
        device.register_connection_event_callback(good)
        device._fire_connection_event(ConnectionEventType.CONNECTED)
        assert calls == [1]


# ---------------------------------------------------------------------------
# _establish_with_shutdown_backoff() — unchanged from 0.11.0
# ---------------------------------------------------------------------------


class TestShutdownBackoff:
    @pytest.mark.asyncio
    async def test_succeeds_on_retry(self) -> None:
        device = OolerBLEDevice(model="OOLER-WD")
        device._ble_device = MagicMock()
        good_client = _make_mock_client()
        with patch(
            "ooler_ble_client.client.establish_connection",
            new_callable=AsyncMock,
            side_effect=[
                BleakError("Bluetooth is already shutdown"),
                good_client,
            ],
        ), _patch_sleep():
            await device.connect()
        assert device._client is good_client

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self) -> None:
        device = OolerBLEDevice(model="OOLER-WD")
        device._ble_device = MagicMock()
        with patch(
            "ooler_ble_client.client.establish_connection",
            new_callable=AsyncMock,
            side_effect=BleakError("Bluetooth is already shutdown"),
        ) as mock_establish, _patch_sleep():
            with pytest.raises(BleakError, match="Bluetooth is already shutdown"):
                await device.connect()
        assert mock_establish.call_count == _SHUTDOWN_ERROR_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_non_shutdown_error_propagates_immediately(self) -> None:
        device = OolerBLEDevice(model="OOLER-WD")
        device._ble_device = MagicMock()
        with patch(
            "ooler_ble_client.client.establish_connection",
            new_callable=AsyncMock,
            side_effect=BleakError("some other error"),
        ) as mock_establish, _patch_sleep():
            with pytest.raises(BleakError, match="some other error"):
                await device.connect()
        assert mock_establish.call_count == 1
