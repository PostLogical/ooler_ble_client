"""Test writing a sleep schedule to the Ooler via our library.

Workflow:
  1. Connect to device, read current schedule
  2. Write a known test schedule
  3. Read it back and verify round-trip
  4. Optionally restore the original schedule

Usage:
    python test_schedule_write.py                # scan for Oolers
    python test_schedule_write.py AA:BB:CC:..    # specific device
    python test_schedule_write.py --restore      # write then restore original
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import time

from bleak import BleakScanner

from ooler_ble_client import OolerBLEDevice, OolerSleepSchedule
from ooler_ble_client.sleep_schedule import (
    SleepScheduleEvent,
    SleepScheduleNight,
    WarmWake,
    build_sleep_schedule,
    decode_sleep_schedule_events,
    encode_sleep_schedule_events,
    events_to_sleep_schedule,
    sleep_schedule_to_events,
)


def print_schedule(label: str, schedule: OolerSleepSchedule) -> None:
    print(f"\n{'='*60}")
    print(f"{label} (seq={schedule.seq}, {len(schedule.nights)} nights)")
    print(f"{'='*60}")
    if not schedule.nights:
        print("  (empty)")
        return
    for night in schedule.nights:
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        day = days[night.day] if night.day < 7 else f"Day{night.day}"
        temps = ", ".join(f"{t.strftime('%H:%M')}={temp}°F" for t, temp in night.temps)
        ww = ""
        if night.warm_wake:
            ww = f"  warm_wake={night.warm_wake.target_temp_f}°F/{night.warm_wake.duration_min}min"
        print(f"  {day}: {temps} → off={night.off_time.strftime('%H:%M')}{ww}")


def print_events(label: str, events: list[SleepScheduleEvent]) -> None:
    print(f"\n{label} ({len(events)} events):")
    for e in events:
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        day = days[e.day] if e.day < 7 else f"Day{e.day}"
        t = e.time.strftime("%H:%M")
        if e.is_off:
            temp = "OFF"
        elif e.is_warm_wake_marker:
            temp = "WARM_WAKE"
        else:
            temp = f"{e.temp_f}°F"
        print(f"  {day} {t} = {temp}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test writing schedules to Ooler")
    parser.add_argument("address", nargs="?", help="Device address (scans if omitted)")
    parser.add_argument("--restore", action="store_true", help="Restore original schedule after test")
    args = parser.parse_args()

    if args.address:
        address = args.address
        name = "Ooler"
    else:
        print("Scanning for Ooler devices (10s) ...")
        devices = await BleakScanner.discover(timeout=10.0)
        oolers = [d for d in devices if d.name and "ooler" in d.name.lower()]
        if not oolers:
            print("No Ooler devices found.")
            return
        if len(oolers) == 1:
            address, name = oolers[0].address, oolers[0].name or "Ooler"
        else:
            print(f"Found {len(oolers)} Oolers:")
            for i, d in enumerate(oolers):
                print(f"  {i + 1}. {d.name} ({d.address})")
            choice = input("Which one? [1]: ").strip() or "1"
            d = oolers[int(choice) - 1]
            address, name = d.address, d.name or "Ooler"

    print(f"\nUsing device: {name} ({address})")

    device = OolerBLEDevice(model=name)
    ble_device = None
    # Find the BLEDevice object
    devices = await BleakScanner.discover(timeout=10.0)
    for d in devices:
        if d.address == address:
            ble_device = d
            break
    if ble_device is None:
        print(f"Could not find device {address} in scan.")
        return

    device.set_ble_device(ble_device)

    try:
        print("Connecting...")
        await device.connect()
        print(f"Connected. Power={'ON' if device.state.power else 'OFF'}")

        # Step 1: Read current schedule
        original = await device.read_sleep_schedule()
        original_events = device.sleep_schedule_events[:]
        print_schedule("ORIGINAL SCHEDULE", original)
        print_events("Original events", original_events)

        # Step 2: Build a test schedule
        test_schedule = build_sleep_schedule(
            bedtime=time(23, 0),
            wake_time=time(7, 0),
            temp_f=65,
            days=[0, 1, 2, 3, 4, 5, 6],  # all days
            warm_wake=WarmWake(target_temp_f=116, duration_min=30),
        )
        test_events = sleep_schedule_to_events(test_schedule)
        print_schedule("TEST SCHEDULE (to write)", test_schedule)
        print(f"  ({len(test_events)} events)")

        # Step 3: Write the test schedule
        print("\nWriting test schedule to device...")
        await device.set_sleep_schedule(test_schedule.nights)
        print("Write complete.")

        # Step 4: Read it back
        readback = await device.read_sleep_schedule()
        readback_events = device.sleep_schedule_events[:]
        print_schedule("READBACK SCHEDULE", readback)
        print_events("Readback events", readback_events)

        # Step 5: Verify round-trip
        print("\n" + "="*60)
        print("VERIFICATION")
        print("="*60)

        # Compare events
        if readback_events == test_events:
            print("  PASS: Events match exactly!")
        else:
            print(f"  FAIL: Events differ!")
            print(f"    Written: {len(test_events)} events")
            print(f"    Read:    {len(readback_events)} events")
            for i, (w, r) in enumerate(zip(test_events, readback_events)):
                if w != r:
                    print(f"    Diff at [{i}]: wrote {w}, read {r}")
            if len(test_events) != len(readback_events):
                print(f"    Length mismatch: {len(test_events)} vs {len(readback_events)}")

        # Compare structured
        if len(readback.nights) == len(test_schedule.nights):
            print(f"  PASS: Same number of nights ({len(readback.nights)})")
        else:
            print(f"  FAIL: Night count: wrote {len(test_schedule.nights)}, read {len(readback.nights)}")

        # Compare wire bytes
        test_times, test_temps = encode_sleep_schedule_events(test_events)
        readback_times, readback_temps = encode_sleep_schedule_events(readback_events)
        if test_times == readback_times:
            print("  PASS: SCHEDULE_TIMES bytes match")
        else:
            print("  FAIL: SCHEDULE_TIMES bytes differ")
        if test_temps == readback_temps:
            print("  PASS: SCHEDULE_TEMPS bytes match")
        else:
            print("  FAIL: SCHEDULE_TEMPS bytes differ")

        # Step 6: Optionally restore
        if args.restore:
            print("\nRestoring original schedule...")
            if original_events:
                await device.set_sleep_schedule_events(original_events)
            else:
                await device.clear_sleep_schedule()
            restored = await device.read_sleep_schedule()
            print_schedule("RESTORED SCHEDULE", restored)
            if device.sleep_schedule_events == original_events:
                print("  PASS: Restore matches original")
            else:
                print("  FAIL: Restore doesn't match original")
        else:
            print("\nTest schedule left on device.")
            print("Open the Ooler app to verify it displays correctly.")
            print("Run with --restore to auto-restore the original schedule.")

    finally:
        print("\nDisconnecting...")
        await device.stop()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
