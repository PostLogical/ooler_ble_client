"""Monitor all BLE characteristics on an Ooler device.

Connects and polls all readable characteristics every few seconds,
printing any changes as they happen. Use alongside the Ooler app to
map which characteristics correspond to which app controls.

Usage:
    python monitor_characteristics.py                # scan and pick first Ooler
    python monitor_characteristics.py AA:BB:CC:..    # connect to specific device
    python monitor_characteristics.py --interval 2   # poll every 2 seconds (default: 3)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

from ooler_ble_client.const import (
    ACTUALTEMP_CHAR,
    AMBIENT_TEMPERATURE_F_CHAR,
    CLEAN_CHAR,
    DEVICE_LOGS_CHAR,
    DISPLAY_TEMPERATURE_UNIT_CHAR,
    LIFETIME_CHAR,
    MODE_CHAR,
    MODE_INT_TO_MODE_STATE,
    POWER_CHAR,
    POWER_RAIL_CHAR,
    PUMP_LEVEL_CHAR,
    RELATIVE_HUMIDITY_CHAR,
    RUNTIME_CHAR,
    SERIAL_NUMBER_CHAR,
    SETTEMP_CHAR,
    UV_RUNTIME_CHAR,
    WARMWAKE_CHAR,
    WATER_LEVEL_CHAR,
)

# Friendly names for known UUIDs
KNOWN_NAMES: dict[str, str] = {
    POWER_CHAR: "POWER",
    MODE_CHAR: "MODE",
    SETTEMP_CHAR: "SET_TEMP",
    ACTUALTEMP_CHAR: "ACTUAL_TEMP",
    WATER_LEVEL_CHAR: "WATER_LEVEL",
    CLEAN_CHAR: "CLEAN",
    DISPLAY_TEMPERATURE_UNIT_CHAR: "DISPLAY_TEMP_UNIT",
    WARMWAKE_CHAR: "WARM_WAKE",
    RELATIVE_HUMIDITY_CHAR: "RELATIVE_HUMIDITY",
    AMBIENT_TEMPERATURE_F_CHAR: "AMBIENT_TEMP_F",
    SERIAL_NUMBER_CHAR: "SERIAL_NUMBER",
    DEVICE_LOGS_CHAR: "DEVICE_LOGS",
    PUMP_LEVEL_CHAR: "PUMP_LEVEL",
    POWER_RAIL_CHAR: "POWER_RAIL",
    LIFETIME_CHAR: "LIFETIME",
    RUNTIME_CHAR: "RUNTIME",
    UV_RUNTIME_CHAR: "UV_RUNTIME",
    # Standard BLE characteristics
    "00002a29-0000-1000-8000-00805f9b34fb": "MANUFACTURER_NAME",
    "00002a26-0000-1000-8000-00805f9b34fb": "FIRMWARE_REV",
    "00002a24-0000-1000-8000-00805f9b34fb": "MODEL_NUMBER",
    "00002a2b-0000-1000-8000-00805f9b34fb": "CURRENT_TIME",
    "00002a0f-0000-1000-8000-00805f9b34fb": "LOCAL_TIME_INFO",
    "00002a14-0000-1000-8000-00805f9b34fb": "REF_TIME_INFO",
    "00002aaa-0000-1000-8000-00805f9b34fb": "UNKNOWN_2AAA",
}

# Short labels for services
SERVICE_NAMES: dict[str, str] = {
    "0000180a-0000-1000-8000-00805f9b34fb": "DeviceInfo",
    "1d14d6ee-fd63-4fa1-bfa4-8f47b42119f0": "WriteOnly1",
    "5c293993-d039-4225-92f6-31fa62101e96": "MainControl",
    "b430cd72-3a7f-4720-86fd-66ae8f6f3493": "Schedule?",
    "00001805-0000-1000-8000-00805f9b34fb": "TimeService",
    "4bf69dcd-412d-494c-9348-f2f364e5c6ce": "CmdResponse?",
    "28dfbeff-61e0-4aa2-9eea-ede0b86f3f65": "Diagnostics",
    "dc5e0473-d2ec-4f23-9b61-cd7bae046f76": "DeviceConfig",
    "4d44eb61-87dd-402c-ad4c-41928e08c8eb": "Unknown9",
}


def format_value(data: bytes) -> str:
    """Format bytes for display."""
    hex_str = data.hex(" ")
    parts = [f"hex={hex_str}"]
    if len(data) == 1:
        parts.append(f"int={data[0]}")
    elif len(data) == 2:
        parts.append(f"int={int.from_bytes(data, 'little')}")
    elif len(data) <= 8:
        parts.append(f"int={int.from_bytes(data, 'little')}")
    try:
        text = data.decode("utf-8")
        if text.isprintable():
            parts.append(f'"{text}"')
    except (UnicodeDecodeError, ValueError):
        pass
    return "  ".join(parts)


def char_label(char: BleakGATTCharacteristic) -> str:
    """Get a display label for a characteristic."""
    name = KNOWN_NAMES.get(char.uuid, char.uuid[:13] + "...")
    return name


def decode_friendly(uuid: str, data: bytes) -> str:
    """Decode a value with domain knowledge where possible."""
    if uuid == POWER_CHAR:
        return "ON" if data[0] else "OFF"
    if uuid == MODE_CHAR and len(data) == 1 and data[0] < len(MODE_INT_TO_MODE_STATE):
        return MODE_INT_TO_MODE_STATE[data[0]]
    if uuid == SETTEMP_CHAR:
        return f"{data[0]}°F"
    if uuid == ACTUALTEMP_CHAR:
        return f"{data[0]}°F"
    if uuid == DISPLAY_TEMPERATURE_UNIT_CHAR:
        return "Celsius" if data[0] == 1 else "Fahrenheit"
    if uuid == CLEAN_CHAR:
        return "ACTIVE" if data[0] else "off"
    if uuid == WATER_LEVEL_CHAR:
        return f"{data[0]}%"
    if uuid == AMBIENT_TEMPERATURE_F_CHAR:
        return f"{data[0]}°F"
    if uuid == RELATIVE_HUMIDITY_CHAR:
        return f"{data[0]}%"
    return ""


async def monitor(address: str, interval: float, name: str | None = None) -> None:
    """Connect and continuously monitor all readable characteristics."""
    label = f"{name} ({address})" if name else address
    print(f"Connecting to {label} ...")

    async with BleakClient(address, timeout=20.0) as client:
        print(f"Connected: {client.is_connected}\n")

        # Discover all readable characteristics
        readable: list[BleakGATTCharacteristic] = []
        for service in client.services:
            svc_name = SERVICE_NAMES.get(service.uuid, service.uuid[:13] + "...")
            for char in service.characteristics:
                if "read" in char.properties:
                    readable.append(char)

        # Print the characteristic map once
        print("Monitoring these characteristics:")
        print("-" * 70)
        for char in readable:
            svc = None
            for service in client.services:
                if char in service.characteristics:
                    svc = SERVICE_NAMES.get(service.uuid, service.uuid[:13] + "...")
                    break
            print(f"  [{svc}] {char_label(char):25s} {char.uuid}")
        print("-" * 70)
        print()

        # Initial read
        prev_values: dict[str, bytes] = {}
        print(f"{'--- Initial state ---':^70}")
        for char in readable:
            try:
                data = await client.read_gatt_char(char)
                prev_values[char.uuid] = bytes(data)
                friendly = decode_friendly(char.uuid, data)
                friendly_str = f"  => {friendly}" if friendly else ""
                print(f"  {char_label(char):25s} {format_value(data)}{friendly_str}")
            except Exception as e:
                print(f"  {char_label(char):25s} READ ERROR: {e}")
        print()
        print("=" * 70)
        print("Watching for changes... (Ctrl+C to stop)")
        print(f"Polling every {interval}s. Make changes in the Ooler app now.")
        print("=" * 70)
        print()

        # Poll loop
        try:
            while True:
                await asyncio.sleep(interval)
                now = datetime.now().strftime("%H:%M:%S")
                changes: list[str] = []

                for char in readable:
                    try:
                        data = bytes(await client.read_gatt_char(char))
                    except Exception:
                        continue

                    old = prev_values.get(char.uuid)
                    if old is not None and data != old:
                        friendly = decode_friendly(char.uuid, data)
                        old_friendly = decode_friendly(char.uuid, old)
                        friendly_str = ""
                        if friendly:
                            friendly_str = f"  ({old_friendly} -> {friendly})"
                        changes.append(
                            f"  {char_label(char):25s} "
                            f"{format_value(old)} -> {format_value(data)}"
                            f"{friendly_str}"
                        )
                    prev_values[char.uuid] = data

                if changes:
                    print(f"[{now}] CHANGED:")
                    for line in changes:
                        print(line)
                    print()

        except KeyboardInterrupt:
            print("\nStopping monitor.")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor Ooler BLE characteristics")
    parser.add_argument("address", nargs="?", help="Device address (scans if omitted)")
    parser.add_argument(
        "--interval", type=float, default=3.0, help="Poll interval in seconds (default: 3)"
    )
    args = parser.parse_args()

    if args.address:
        await monitor(args.address, args.interval)
    else:
        print("Scanning for Ooler devices (10s) ...")
        devices = await BleakScanner.discover(timeout=10.0)
        oolers = [d for d in devices if d.name and "ooler" in d.name.lower()]

        if not oolers:
            print("No Ooler devices found.")
            return

        if len(oolers) == 1:
            d = oolers[0]
            await monitor(d.address, args.interval, d.name)
        else:
            print(f"Found {len(oolers)} Oolers:")
            for i, d in enumerate(oolers):
                print(f"  {i + 1}. {d.name} ({d.address})")
            choice = input("Which one? [1]: ").strip() or "1"
            d = oolers[int(choice) - 1]
            await monitor(d.address, args.interval, d.name)


if __name__ == "__main__":
    asyncio.run(main())
