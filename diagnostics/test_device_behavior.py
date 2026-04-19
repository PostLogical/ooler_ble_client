"""Test device behavior: writes-when-off and LO/HI temperature boundaries.

Connects to an Ooler and runs targeted experiments to verify:
1. Whether writes to SET_TEMP and MODE are dropped when the device is off
2. Whether temperatures between 45-55 and 115-120 are accepted or rejected
3. What values LO (45) and HI (120) actually represent

Usage:
    python test_device_behavior.py              # scan for Oolers
    python test_device_behavior.py AA:BB:CC:..  # specific device

WARNING: This script writes to the device. It will attempt to restore
original values after each test.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from bleak import BleakClient, BleakScanner

# UUIDs
POWER_CHAR = "7a2623ff-bd92-4c13-be9f-7023aa4ecb85"
MODE_CHAR = "cafe2421-d04c-458f-b1c0-253c6c97e8e8"
SETTEMP_CHAR = "6aa46711-a29d-4f8a-88e2-044ca1fd03ff"


async def read_state(client: BleakClient) -> dict:
    """Read current power, mode, and set_temp."""
    power = int.from_bytes(await client.read_gatt_char(POWER_CHAR), "little")
    mode = int.from_bytes(await client.read_gatt_char(MODE_CHAR), "little")
    settemp = int.from_bytes(await client.read_gatt_char(SETTEMP_CHAR), "little")
    return {"power": power, "mode": mode, "set_temp": settemp}


async def write_and_check(
    client: BleakClient, char: str, value: int, label: str
) -> tuple[bool, int]:
    """Write a value, read it back, return (accepted, read_back_value)."""
    await client.write_gatt_char(char, value.to_bytes(1, "little"))
    await asyncio.sleep(0.5)  # give device time to process
    read_back = int.from_bytes(await client.read_gatt_char(char), "little")
    accepted = read_back == value
    return accepted, read_back


async def test_writes_when_off(client: BleakClient) -> None:
    """Test whether SET_TEMP and MODE writes are accepted when device is off."""
    print("\n" + "=" * 60)
    print("TEST: Writes when device is OFF")
    print("=" * 60)

    # Ensure device is on first to set a known baseline
    state = await read_state(client)
    original_temp = state["set_temp"]
    original_mode = state["mode"]

    if state["power"]:
        print("Device is ON. Turning OFF for test...")
        await client.write_gatt_char(POWER_CHAR, b"\x00")
        await asyncio.sleep(1)
    else:
        print("Device is already OFF.")

    state = await read_state(client)
    print(f"  Baseline (off): temp={state['set_temp']}, mode={state['mode']}")

    # Try writing a different temp while off
    test_temp = 70 if state["set_temp"] != 70 else 65
    print(f"\n  Writing SET_TEMP={test_temp} while OFF...")
    accepted, read_back = await write_and_check(client, SETTEMP_CHAR, test_temp, "SET_TEMP")
    if accepted:
        print(f"  RESULT: ACCEPTED (read back {read_back})")
    else:
        print(f"  RESULT: DROPPED or CHANGED (wrote {test_temp}, read back {read_back})")

    # Try writing a different mode while off
    test_mode = 2 if state["mode"] != 2 else 0  # boost or silent
    mode_names = {0: "Silent", 1: "Regular", 2: "Boost"}
    print(f"\n  Writing MODE={mode_names[test_mode]} while OFF...")
    accepted, read_back = await write_and_check(client, MODE_CHAR, test_mode, "MODE")
    if accepted:
        print(f"  RESULT: ACCEPTED (read back {read_back} = {mode_names.get(read_back, '?')})")
    else:
        print(f"  RESULT: DROPPED or CHANGED (wrote {test_mode}, read back {read_back} = {mode_names.get(read_back, '?')})")

    # Restore originals
    print(f"\n  Restoring: temp={original_temp}, mode={original_mode}")
    await client.write_gatt_char(SETTEMP_CHAR, original_temp.to_bytes(1, "little"))
    await client.write_gatt_char(MODE_CHAR, original_mode.to_bytes(1, "little"))
    await asyncio.sleep(0.5)


async def test_temp_boundaries(client: BleakClient) -> None:
    """Test which temperature values the device accepts."""
    print("\n" + "=" * 60)
    print("TEST: Temperature boundary values")
    print("=" * 60)

    state = await read_state(client)
    original_temp = state["set_temp"]

    # Make sure device is on
    if not state["power"]:
        print("Turning device ON for temp tests...")
        await client.write_gatt_char(POWER_CHAR, b"\x01")
        await asyncio.sleep(1)

    # Set a known baseline first
    print("Setting baseline temp to 68°F...")
    await client.write_gatt_char(SETTEMP_CHAR, (68).to_bytes(1, "little"))
    await asyncio.sleep(0.5)

    # Test values in the interesting ranges
    test_values = [
        # Below LO
        (44, "below LO"),
        (45, "LO"),
        # Between LO and current min
        (46, "LO+1"),
        (50, "between LO and 55"),
        (54, "just below 55"),
        (55, "current _MIN_TEMP_F"),
        # Normal range
        (68, "normal (reset)"),
        # Between current max and HI
        (115, "current _MAX_TEMP_F"),
        (116, "just above 115"),
        (118, "between 115 and HI"),
        (119, "HI-1"),
        (120, "HI"),
        # Above HI
        (121, "above HI"),
    ]

    print(f"\n  {'Value':>5}  {'Label':<25}  {'Result':<40}")
    print(f"  {'-'*5}  {'-'*25}  {'-'*40}")

    for value, label in test_values:
        # Reset to 68 between each test
        await client.write_gatt_char(SETTEMP_CHAR, (68).to_bytes(1, "little"))
        await asyncio.sleep(0.3)

        accepted, read_back = await write_and_check(client, SETTEMP_CHAR, value, label)
        if accepted:
            result = f"ACCEPTED (read back {read_back})"
        else:
            result = f"REJECTED/CHANGED (wrote {value}, got {read_back})"
        print(f"  {value:>5}  {label:<25}  {result}")

    # Restore
    print(f"\n  Restoring temp to {original_temp}")
    await client.write_gatt_char(SETTEMP_CHAR, original_temp.to_bytes(1, "little"))
    await asyncio.sleep(0.5)

    # Turn off if it was off
    if not state["power"]:
        print("  Turning device back OFF")
        await client.write_gatt_char(POWER_CHAR, b"\x00")


async def run_tests(address: str, name: str | None = None) -> None:
    label = f"{name} ({address})" if name else address
    print(f"Connecting to {label} ...")

    async with BleakClient(address, timeout=20.0) as client:
        print(f"Connected.\n")

        state = await read_state(client)
        mode_names = {0: "Silent", 1: "Regular", 2: "Boost"}
        print(f"Current state: power={'ON' if state['power'] else 'OFF'}, "
              f"temp={state['set_temp']}°F, "
              f"mode={mode_names.get(state['mode'], '?')}")

        await test_writes_when_off(client)
        await test_temp_boundaries(client)

        print("\n" + "=" * 60)
        print("ALL TESTS COMPLETE")
        final = await read_state(client)
        print(f"Final state: power={'ON' if final['power'] else 'OFF'}, "
              f"temp={final['set_temp']}°F, "
              f"mode={mode_names.get(final['mode'], '?')}")
        print("=" * 60)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test Ooler device behavior")
    parser.add_argument("address", nargs="?", help="Device address")
    args = parser.parse_args()

    if args.address:
        await run_tests(args.address)
    else:
        print("Scanning for Ooler devices (10s) ...")
        devices = await BleakScanner.discover(timeout=10.0)
        oolers = [d for d in devices if d.name and "ooler" in d.name.lower()]

        if not oolers:
            print("No Ooler devices found.")
            return

        if len(oolers) == 1:
            await run_tests(oolers[0].address, oolers[0].name)
        else:
            print(f"Found {len(oolers)} Oolers:")
            for i, d in enumerate(oolers):
                print(f"  {i + 1}. {d.name} ({d.address})")
            choice = input("Which one? [1]: ").strip() or "1"
            d = oolers[int(choice) - 1]
            await run_tests(d.address, d.name)


if __name__ == "__main__":
    asyncio.run(main())
