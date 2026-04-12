"""Tests for the notification-staleness watchdog and connection-event channel.

These tests exercise the 0.11.0 fix for silent notification stalls on
ESPHome BLE proxies. The watchdog itself is a background task with a
30-second cadence, so the tests drive it deterministically via the
extracted ``_watchdog_tick()`` method and the injectable monotonic clock.
"""
from __future__ import annotations

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
    _NOTIFY_STALL_TIMEOUT_SECONDS,
    _WATCHDOG_RECONNECT_COOLDOWN_SECONDS,
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
    device._state.power = power
    return device, client


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
# _watchdog_tick()
# ---------------------------------------------------------------------------


class TestWatchdogTick:
    @pytest.mark.asyncio
    async def test_no_client_is_noop(self) -> None:
        device = OolerBLEDevice(model="OOLER-WD")
        device._state.power = True
        device._last_notification_monotonic = 0.0
        clock = FakeClock()
        clock.advance(_NOTIFY_STALL_TIMEOUT_SECONDS + 1)
        device._monotonic = clock
        # Should not raise, should not try to reconnect (no client).
        await device._watchdog_tick()

    @pytest.mark.asyncio
    async def test_power_off_gates_watchdog(self) -> None:
        """When the device is off, the notification stream legitimately
        stops and the watchdog must not fire.
        """
        device, _ = _make_connected_powered_device(power=False)
        clock = FakeClock()
        device._monotonic = clock
        device._last_notification_monotonic = clock.now
        clock.advance(_NOTIFY_STALL_TIMEOUT_SECONDS * 10)
        reconnect_mock = AsyncMock()
        device._execute_forced_reconnect = reconnect_mock  # type: ignore[method-assign]
        await device._watchdog_tick()
        reconnect_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_stamp_is_noop(self) -> None:
        device, _ = _make_connected_powered_device()
        device._last_notification_monotonic = None
        reconnect_mock = AsyncMock()
        device._execute_forced_reconnect = reconnect_mock  # type: ignore[method-assign]
        await device._watchdog_tick()
        reconnect_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_under_threshold_is_noop(self) -> None:
        device, _ = _make_connected_powered_device()
        clock = FakeClock()
        device._monotonic = clock
        device._last_notification_monotonic = clock.now
        clock.advance(_NOTIFY_STALL_TIMEOUT_SECONDS - 1)
        reconnect_mock = AsyncMock()
        device._execute_forced_reconnect = reconnect_mock  # type: ignore[method-assign]
        await device._watchdog_tick()
        reconnect_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_fires_forced_reconnect_above_threshold(self) -> None:
        device, _ = _make_connected_powered_device()
        clock = FakeClock()
        device._monotonic = clock
        device._last_notification_monotonic = clock.now
        clock.advance(_NOTIFY_STALL_TIMEOUT_SECONDS + 1)

        events: list[ConnectionEvent] = []
        device.register_connection_event_callback(events.append)

        reconnect_mock = AsyncMock()
        device._execute_forced_reconnect = reconnect_mock  # type: ignore[method-assign]
        await device._watchdog_tick()

        reconnect_mock.assert_called_once_with(trigger="notify_stall")
        types = [e.type for e in events]
        assert ConnectionEventType.NOTIFY_STALL in types
        stall = next(e for e in events if e.type == ConnectionEventType.NOTIFY_STALL)
        assert stall.detail is not None
        assert stall.detail["stall_duration_seconds"] == pytest.approx(
            _NOTIFY_STALL_TIMEOUT_SECONDS + 1
        )

    @pytest.mark.asyncio
    async def test_cooldown_suppresses_reconnect(self) -> None:
        device, _ = _make_connected_powered_device()
        clock = FakeClock()
        device._monotonic = clock
        device._last_notification_monotonic = clock.now
        device._force_reconnect_cooldown_until = (
            clock.now + _WATCHDOG_RECONNECT_COOLDOWN_SECONDS
        )
        clock.advance(_NOTIFY_STALL_TIMEOUT_SECONDS + 1)
        # Now still inside cooldown window.
        device._force_reconnect_cooldown_until = clock.now + 10
        reconnect_mock = AsyncMock()
        device._execute_forced_reconnect = reconnect_mock  # type: ignore[method-assign]
        await device._watchdog_tick()
        reconnect_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconnect_failure_enters_cooldown(self) -> None:
        device, _ = _make_connected_powered_device()
        clock = FakeClock()
        device._monotonic = clock
        device._last_notification_monotonic = clock.now
        clock.advance(_NOTIFY_STALL_TIMEOUT_SECONDS + 5)
        device._execute_forced_reconnect = AsyncMock(  # type: ignore[method-assign]
            side_effect=BleakError("cannot reconnect")
        )
        await device._watchdog_tick()
        assert device._force_reconnect_cooldown_until > clock.now


# ---------------------------------------------------------------------------
# Notification handler timestamp
# ---------------------------------------------------------------------------


class TestNotificationTimestamp:
    def test_handler_stamps_last_notification(self) -> None:
        device = OolerBLEDevice(model="OOLER-WD")
        clock = FakeClock()
        device._monotonic = clock
        sender = MagicMock()
        sender.uuid = POWER_CHAR
        device._notification_handler(sender, bytearray(b"\x01"))
        assert device._last_notification_monotonic == clock.now

    def test_handler_stamps_even_on_unknown_uuid(self) -> None:
        device = OolerBLEDevice(model="OOLER-WD")
        clock = FakeClock()
        device._monotonic = clock
        sender = MagicMock()
        sender.uuid = "unknown-uuid"
        device._notification_handler(sender, bytearray(b"\x00"))
        assert device._last_notification_monotonic == clock.now


# ---------------------------------------------------------------------------
# set_power(False) resets the grace period
# ---------------------------------------------------------------------------


class TestSetPowerStamps:
    @pytest.mark.asyncio
    async def test_power_off_stamps_timestamp(self) -> None:
        device, _ = _make_connected_powered_device()
        clock = FakeClock()
        device._monotonic = clock
        device._last_notification_monotonic = 0.0
        clock.advance(500)
        await device.set_power(False)
        assert device._last_notification_monotonic == clock.now


# ---------------------------------------------------------------------------
# Flap suppression: is_connected during forced reconnect
# ---------------------------------------------------------------------------


class TestFlapSuppression:
    @pytest.mark.asyncio
    async def test_is_connected_stays_true_during_forced_reconnect(self) -> None:
        device, old_client = _make_connected_powered_device()
        device._ble_device = MagicMock()

        seen: list[bool] = []

        async def capture_is_connected(*args, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(device.is_connected)
            return _make_mock_client()

        with patch(
            "ooler_ble_client.client.establish_connection",
            side_effect=capture_is_connected,
        ), _patch_sleep():
            await device._execute_forced_reconnect(trigger="notify_stall")

        # During the window where _client was None and establish_connection
        # was running, is_connected should have been True via the
        # _force_reconnecting flag.
        assert seen == [True]
        # And after success it still reflects the new live client.
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
                await device._execute_forced_reconnect(trigger="notify_stall")
        assert device._force_reconnecting is False
        assert device.is_connected is False

    @pytest.mark.asyncio
    async def test_disconnected_callback_suppressed_during_forced_reconnect(
        self,
    ) -> None:
        """The state callback and DISCONNECTED event are suppressed so the
        coordinator does not see a transient unavailability.
        """
        device, old_client = _make_connected_powered_device()
        state_events: list[object] = []
        conn_events: list[ConnectionEvent] = []
        device.register_callback(lambda s: state_events.append(s))
        device.register_connection_event_callback(conn_events.append)

        # Simulate the disconnect callback firing mid-forced-reconnect.
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
            await device._execute_forced_reconnect(trigger="notify_stall")

        forced = [e for e in events if e.type == ConnectionEventType.FORCED_RECONNECT]
        assert len(forced) == 1
        assert forced[0].detail == {"trigger": "notify_stall"}

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
        # Second call should not raise even though the callback is gone.
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
# _establish_with_shutdown_backoff()
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


# ---------------------------------------------------------------------------
# Watchdog lifecycle: started on connect, cancelled on stop
# ---------------------------------------------------------------------------


class TestWatchdogLifecycle:
    @pytest.mark.asyncio
    async def test_watchdog_task_started_on_connect_when_enabled(self) -> None:
        device = OolerBLEDevice(model="OOLER-WD")
        device._watchdog_enabled = True
        device._ble_device = MagicMock()
        with _patch_establish(_make_mock_client()):
            await device.connect()
        try:
            assert device._watchdog_task is not None
            assert not device._watchdog_task.done()
        finally:
            await device.stop()

    @pytest.mark.asyncio
    async def test_watchdog_task_cancelled_on_stop(self) -> None:
        device = OolerBLEDevice(model="OOLER-WD")
        device._watchdog_enabled = True
        device._ble_device = MagicMock()
        with _patch_establish(_make_mock_client()):
            await device.connect()
        task = device._watchdog_task
        assert task is not None
        await device.stop()
        assert task.done()
        assert device._watchdog_task is None

    @pytest.mark.asyncio
    async def test_watchdog_initial_timestamp_set_on_connect(self) -> None:
        device = OolerBLEDevice(model="OOLER-WD")
        device._watchdog_enabled = True
        device._ble_device = MagicMock()
        with _patch_establish(_make_mock_client()):
            await device.connect()
        try:
            assert device._last_notification_monotonic is not None
        finally:
            await device.stop()
