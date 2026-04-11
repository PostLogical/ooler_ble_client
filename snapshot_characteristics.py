"""Snapshot all BLE characteristics on an Ooler, then diff against previous.

Workflow:
  1. Run the script — it reads everything and saves a snapshot
  2. It disconnects automatically
  3. Open the Ooler app, change a setting
  4. Close/disconnect the app
  5. Run the script again — it reads everything, diffs against the last snapshot

Each run saves a timestamped JSON file. The script automatically compares
against the most recent previous snapshot and shows what changed.

Usage:
    python snapshot_characteristics.py                # scan for Oolers
    python snapshot_characteristics.py AA:BB:CC:..    # specific device
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner

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

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"

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
    "00002a29-0000-1000-8000-00805f9b34fb": "MANUFACTURER_NAME",
    "00002a26-0000-1000-8000-00805f9b34fb": "FIRMWARE_REV",
    "00002a24-0000-1000-8000-00805f9b34fb": "MODEL_NUMBER",
    "00002a2b-0000-1000-8000-00805f9b34fb": "CURRENT_TIME",
    "00002a0f-0000-1000-8000-00805f9b34fb": "LOCAL_TIME_INFO",
    "00002a14-0000-1000-8000-00805f9b34fb": "REF_TIME_INFO",
    "00002aaa-0000-1000-8000-00805f9b34fb": "UNKNOWN_2AAA",
}

# Characteristics that change every read and aren't useful for diffing
NOISY_UUIDS = {
    "00002a2b-0000-1000-8000-00805f9b34fb",  # CURRENT_TIME
    ACTUALTEMP_CHAR,  # fluctuates constantly
}


def char_name(uuid: str) -> str:
    return KNOWN_NAMES.get(uuid, uuid)


def decode_friendly(uuid: str, hex_str: str) -> str:
    """Best-effort human-readable decode."""
    data = bytes.fromhex(hex_str.replace(" ", ""))
    if not data:
        return ""
    if uuid == POWER_CHAR:
        return "ON" if data[0] else "OFF"
    if uuid == MODE_CHAR and data[0] < len(MODE_INT_TO_MODE_STATE):
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
    try:
        text = data.decode("utf-8")
        if text.isprintable():
            return f'"{text}"'
    except (UnicodeDecodeError, ValueError):
        pass
    if len(data) <= 4:
        return f"int={int.from_bytes(data, 'little')}"
    return ""


async def take_snapshot(address: str, name: str | None = None) -> dict:
    """Connect, read all characteristics, disconnect, return snapshot dict."""
    label = f"{name} ({address})" if name else address
    print(f"Connecting to {label} ...")

    snapshot: dict = {
        "device": name or address,
        "address": address,
        "timestamp": datetime.now().isoformat(),
        "characteristics": {},
    }

    async with BleakClient(address, timeout=20.0) as client:
        print(f"Connected. Reading all characteristics...")

        for service in client.services:
            for char in service.characteristics:
                if "read" not in char.properties:
                    snapshot["characteristics"][char.uuid] = {
                        "service": service.uuid,
                        "properties": list(char.properties),
                        "value": None,
                        "note": "not readable",
                    }
                    continue
                try:
                    data = await client.read_gatt_char(char)
                    hex_str = bytes(data).hex(" ")
                    snapshot["characteristics"][char.uuid] = {
                        "service": service.uuid,
                        "properties": list(char.properties),
                        "value": hex_str,
                    }
                except Exception as e:
                    snapshot["characteristics"][char.uuid] = {
                        "service": service.uuid,
                        "properties": list(char.properties),
                        "value": None,
                        "note": f"read error: {e}",
                    }

    print("Disconnected.\n")
    return snapshot


def save_snapshot(snapshot: dict) -> Path:
    """Save snapshot to a JSON file."""
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    device_name = snapshot.get("device", "unknown").replace(" ", "_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{device_name}_{ts}.json"
    path = SNAPSHOT_DIR / filename
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2)
    return path


def find_previous_snapshot(address: str) -> dict | None:
    """Find the most recent previous snapshot for this device."""
    if not SNAPSHOT_DIR.exists():
        return None
    candidates = []
    for p in SNAPSHOT_DIR.glob("*.json"):
        try:
            with open(p) as f:
                data = json.load(f)
            if data.get("address") == address:
                candidates.append((p.stat().st_mtime, data))
        except (json.JSONDecodeError, KeyError):
            continue
    if not candidates:
        return None
    # Return the most recent (we'll compare against second-most-recent if called after save)
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def diff_snapshots(old: dict, new: dict, label: str | None = None) -> None:
    """Print differences between two snapshots."""
    old_chars = old["characteristics"]
    new_chars = new["characteristics"]

    old_time = old.get("timestamp", "?")
    new_time = new.get("timestamp", "?")

    header = f"DIFF: {old_time}  ->  {new_time}"
    if label:
        header += f"  ({label})"
    print(header)
    print("=" * 70)

    changes = 0
    all_uuids = sorted(set(old_chars) | set(new_chars))

    for uuid in all_uuids:
        if uuid in NOISY_UUIDS:
            continue

        old_val = old_chars.get(uuid, {}).get("value")
        new_val = new_chars.get(uuid, {}).get("value")

        if old_val == new_val:
            continue

        name = char_name(uuid)
        changes += 1

        if old_val is None and new_val is not None:
            friendly = decode_friendly(uuid, new_val)
            f_str = f"  ({friendly})" if friendly else ""
            print(f"  + {name:25s} {new_val}{f_str}")
        elif old_val is not None and new_val is None:
            print(f"  - {name:25s} was {old_val}")
        else:
            old_friendly = decode_friendly(uuid, old_val) if old_val else ""
            new_friendly = decode_friendly(uuid, new_val) if new_val else ""
            arrow = ""
            if old_friendly and new_friendly:
                arrow = f"  ({old_friendly} -> {new_friendly})"
            elif new_friendly:
                arrow = f"  ({new_friendly})"
            print(f"  * {name:25s} {old_val}")
            print(f"    {'':25s} -> {new_val}{arrow}")

    if changes == 0:
        print("  (no changes)")
    print()


def print_snapshot(snapshot: dict) -> None:
    """Print a snapshot in readable form."""
    print(f"Snapshot: {snapshot['device']}  at  {snapshot['timestamp']}")
    print("-" * 70)
    for uuid, info in snapshot["characteristics"].items():
        name = char_name(uuid)
        val = info.get("value")
        if val is None:
            note = info.get("note", "")
            print(f"  {name:25s} [{note}]")
        else:
            friendly = decode_friendly(uuid, val)
            f_str = f"  => {friendly}" if friendly else ""
            print(f"  {name:25s} {val}{f_str}")
    print()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot Ooler BLE characteristics")
    parser.add_argument("address", nargs="?", help="Device address (scans if omitted)")
    parser.add_argument(
        "--label", "-l", type=str, default=None,
        help="Label for this snapshot (e.g. 'after setting temp to 70')"
    )
    args = parser.parse_args()

    if args.address:
        address = args.address
        name = None
    else:
        print("Scanning for Ooler devices (10s) ...")
        devices = await BleakScanner.discover(timeout=10.0)
        oolers = [d for d in devices if d.name and "ooler" in d.name.lower()]

        if not oolers:
            print("No Ooler devices found.")
            return

        if len(oolers) == 1:
            address, name = oolers[0].address, oolers[0].name
        else:
            print(f"Found {len(oolers)} Oolers:")
            for i, d in enumerate(oolers):
                print(f"  {i + 1}. {d.name} ({d.address})")
            choice = input("Which one? [1]: ").strip() or "1"
            d = oolers[int(choice) - 1]
            address, name = d.address, d.name

    # Find previous snapshot before taking new one
    previous = find_previous_snapshot(address)

    # Take new snapshot
    snapshot = await take_snapshot(address, name)
    if args.label:
        snapshot["label"] = args.label
    print_snapshot(snapshot)

    # Save
    path = save_snapshot(snapshot)
    print(f"Saved to: {path}")

    # Diff against previous if available
    if previous:
        print()
        diff_snapshots(previous, snapshot, args.label)
    else:
        print("(No previous snapshot to compare against)")
        print("Run again after making a change in the Ooler app to see a diff.")


if __name__ == "__main__":
    asyncio.run(main())
