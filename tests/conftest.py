"""Shared fixtures for ooler_ble_client tests."""
from __future__ import annotations

import pytest

from ooler_ble_client.client import OolerBLEDevice


@pytest.fixture(autouse=True)
def _disable_notify_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the background notify-staleness watchdog by default.

    The watchdog is a background ``asyncio.Task`` started on every
    successful connect. Most tests don't exercise it and shouldn't
    leak a pending task when the test's event loop closes. Tests that
    explicitly exercise the watchdog re-enable it per-instance with
    ``device._watchdog_enabled = True`` before calling ``connect()``.
    """
    monkeypatch.setattr(
        OolerBLEDevice, "_watchdog_enabled_default", False
    )
