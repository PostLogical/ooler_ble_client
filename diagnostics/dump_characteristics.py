"""Diagnostic script: scan for Ooler devices and dump all BLE characteristics.

Usage:
    python dump_characteristics.py              # scan and dump all Oolers found
    python dump_characteristics.py AA:BB:CC:..  # dump a specific device by address
"""

from __future__ import annotations

import asyncio
import sys

from bleak import BleakClient, BleakScanner
from bleak.backends.service import BleakGATTService

from ooler_ble_client.const import (
    ACTUALTEMP_CHAR,
    AMBIENT_TEMPERATURE_F_CHAR,
    CLEAN_CHAR,
    CMD_RESPONSE_CHAR,
    CMD_WRITE_CHAR,
    CONFIG_WRITE_CHAR,
    CURRENT_TIME_CHAR,
    DELTA_COOL_CHAR,
    DELTA_HEAT_CHAR,
    DEVICE_CONFIG_JSON_CHAR,
    DEVICE_LOGS_CHAR,
    DISPLAY_TEMPERATURE_UNIT_CHAR,
    FIRMWARE_REVISION_CHAR,
    HARDWARE_ID_CHAR,
    LIFETIME_CHAR,
    LOCAL_TIME_INFO_CHAR,
    MANUFACTURER_NAME_CHAR,
    MAX_TEMP_CHAR,
    MIN_TEMP_CHAR,
    MODE_CHAR,
    MODEL_NUMBER_CHAR,
    POWER_CHAR,
    POWER_RAIL_CHAR,
    PUMP_LEVEL_CHAR,
    REFERENCE_TIME_INFO_CHAR,
    RELATIVE_HUMIDITY_CHAR,
    RUNTIME_CHAR,
    SCHEDULE_HEADER_CHAR,
    SCHEDULE_META_CHAR,
    SCHEDULE_TEMPS_CHAR,
    SCHEDULE_TIMES_CHAR,
    SERIAL_NUMBER_CHAR,
    SETTEMP_CHAR,
    SUB_FIRMWARE_CHAR,
    THERMAL_EFFORT_CHAR,
    UNKNOWN_2AAA_CHAR,
    UNKNOWN_51B9_CHAR,
    UNKNOWN_1A7F_CHAR,
    UNKNOWN_8AB5_CHAR,
    UNKNOWN_9234_CHAR,
    UNKNOWN_AF8D_CHAR,
    UNKNOWN_F30D_CHAR,
    UV_RUNTIME_CHAR,
    WATER_LEVEL_CHAR,
    WRITE_COMMAND_CHAR,
)

# Map known UUIDs to friendly names
KNOWN_UUIDS: dict[str, str] = {
    POWER_CHAR: "POWER",
    MODE_CHAR: "MODE",
    SETTEMP_CHAR: "SET_TEMP",
    ACTUALTEMP_CHAR: "ACTUAL_TEMP",
    WATER_LEVEL_CHAR: "WATER_LEVEL",
    CLEAN_CHAR: "CLEAN",
    DISPLAY_TEMPERATURE_UNIT_CHAR: "DISPLAY_TEMP_UNIT",
    THERMAL_EFFORT_CHAR: "THERMAL_EFFORT",
    PUMP_LEVEL_CHAR: "PUMP_LEVEL",
    POWER_RAIL_CHAR: "POWER_RAIL",
    RELATIVE_HUMIDITY_CHAR: "RELATIVE_HUMIDITY",
    AMBIENT_TEMPERATURE_F_CHAR: "AMBIENT_TEMP_F",
    UNKNOWN_F30D_CHAR: "UNKNOWN_F30D",
    UNKNOWN_9234_CHAR: "UNKNOWN_9234",
    UNKNOWN_AF8D_CHAR: "UNKNOWN_AF8D",
    MANUFACTURER_NAME_CHAR: "MANUFACTURER_NAME",
    FIRMWARE_REVISION_CHAR: "FIRMWARE_REV",
    MODEL_NUMBER_CHAR: "MODEL_NUMBER",
    HARDWARE_ID_CHAR: "HARDWARE_ID",
    WRITE_COMMAND_CHAR: "WRITE_COMMAND",
    CMD_WRITE_CHAR: "CMD_WRITE",
    CMD_RESPONSE_CHAR: "CMD_RESPONSE",
    CURRENT_TIME_CHAR: "CURRENT_TIME",
    LOCAL_TIME_INFO_CHAR: "LOCAL_TIME_INFO",
    REFERENCE_TIME_INFO_CHAR: "REF_TIME_INFO",
    LIFETIME_CHAR: "LIFETIME",
    RUNTIME_CHAR: "RUNTIME",
    UV_RUNTIME_CHAR: "UV_RUNTIME",
    DEVICE_LOGS_CHAR: "DEVICE_LOGS",
    SUB_FIRMWARE_CHAR: "SUB_FIRMWARE",
    UNKNOWN_51B9_CHAR: "UNKNOWN_51B9",
    UNKNOWN_1A7F_CHAR: "UNKNOWN_1A7F",
    UNKNOWN_8AB5_CHAR: "UNKNOWN_8AB5",
    SERIAL_NUMBER_CHAR: "SERIAL_NUMBER",
    DEVICE_CONFIG_JSON_CHAR: "DEVICE_CONFIG_JSON",
    MAX_TEMP_CHAR: "MAX_TEMP",
    MIN_TEMP_CHAR: "MIN_TEMP",
    DELTA_HEAT_CHAR: "DELTA_HEAT",
    DELTA_COOL_CHAR: "DELTA_COOL",
    CONFIG_WRITE_CHAR: "CONFIG_WRITE",
    SCHEDULE_HEADER_CHAR: "SCHEDULE_HEADER",
    SCHEDULE_TIMES_CHAR: "SCHEDULE_TIMES",
    SCHEDULE_TEMPS_CHAR: "SCHEDULE_TEMPS",
    SCHEDULE_META_CHAR: "SCHEDULE_META",
    UNKNOWN_2AAA_CHAR: "UNKNOWN_2AAA",
}


def format_value(data: bytes) -> str:
    """Format raw bytes as hex, int, and UTF-8 (if possible)."""
    hex_str = data.hex(" ")
    parts = [f"hex={hex_str}"]
    if len(data) == 1:
        parts.append(f"int={data[0]}")
    elif len(data) == 2:
        parts.append(f"int={int.from_bytes(data, 'little')}")
    try:
        text = data.decode("utf-8")
        if text.isprintable():
            parts.append(f'utf8="{text}"')
    except (UnicodeDecodeError, ValueError):
        pass
    return "  ".join(parts)


async def dump_device(address: str, name: str | None = None) -> None:
    """Connect to a device and dump all characteristics."""
    label = f"{name} ({address})" if name else address
    print(f"\n{'='*70}")
    print(f"Connecting to {label} ...")

    try:
        async with BleakClient(address, timeout=20.0) as client:
            print(f"Connected: {client.is_connected}")
            print(f"{'='*70}\n")

            for service in client.services:
                print(f"Service: {service.uuid}  {service.description or ''}")
                for char in service.characteristics:
                    uuid = char.uuid
                    label = KNOWN_UUIDS.get(uuid, "** UNKNOWN **")
                    props = ", ".join(char.properties)

                    value_str = ""
                    if "read" in char.properties:
                        try:
                            raw = await client.read_gatt_char(char)
                            value_str = format_value(raw)
                        except Exception as e:
                            value_str = f"READ ERROR: {e}"

                    print(f"  {uuid}  [{props}]")
                    print(f"    {label}")
                    if value_str:
                        print(f"    {value_str}")
                    print()

    except Exception as e:
        print(f"Failed to connect to {label}: {e}")


async def scan_and_dump() -> None:
    """Scan for Ooler devices and dump each one."""
    print("Scanning for BLE devices (10s) ...")
    devices = await BleakScanner.discover(timeout=10.0)

    oolers = [d for d in devices if d.name and "ooler" in d.name.lower()]

    if not oolers:
        print("\nNo devices with 'ooler' in the name found.")
        print("All discovered devices:")
        for d in sorted(devices, key=lambda x: x.name or ""):
            print(f"  {d.address}  {d.name or '(no name)'}")
        print("\nTip: re-run with the device address as an argument:")
        print("  python dump_characteristics.py <ADDRESS>")
        return

    print(f"\nFound {len(oolers)} Ooler(s):")
    for d in oolers:
        print(f"  {d.address}  {d.name}")

    for d in oolers:
        await dump_device(d.address, d.name)


async def main() -> None:
    if len(sys.argv) > 1:
        for addr in sys.argv[1:]:
            await dump_device(addr)
    else:
        await scan_and_dump()


if __name__ == "__main__":
    asyncio.run(main())
