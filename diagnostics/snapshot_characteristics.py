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
import struct
import sys
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner

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
    MODE_INT_TO_MODE_STATE,
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

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"

KNOWN_NAMES: dict[str, str] = {
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

# Characteristics that change every read and aren't useful for diffing
NOISY_UUIDS = {
    "00002a2b-0000-1000-8000-00805f9b34fb",  # CURRENT_TIME
    ACTUALTEMP_CHAR,  # fluctuates constantly
}


_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def char_name(uuid: str) -> str:
    return KNOWN_NAMES.get(uuid, uuid)


def _week_min_to_str(mins: int) -> str:
    """Convert minute-of-week to 'Day HH:MM'."""
    day = mins // 1440
    h, m = divmod(mins % 1440, 60)
    d = _DAYS[day] if day < 7 else f"Day{day}"
    return f"{d} {h:02d}:{m:02d}"


def _temp_str(t: int) -> str:
    if t == 0:
        return "OFF"
    if t == 0xFE:
        return "WARM_WAKE"
    return f"{t}°F"


def decode_schedule(chars: dict) -> str | None:
    """Decode schedule characteristics into a human-readable summary."""
    header_info = chars.get(SCHEDULE_HEADER_CHAR, {})
    times_info = chars.get(SCHEDULE_TIMES_CHAR, {})
    temps_info = chars.get(SCHEDULE_TEMPS_CHAR, {})
    meta_info = chars.get(SCHEDULE_META_CHAR, {})

    times_hex = times_info.get("value")
    temps_hex = temps_info.get("value")
    if not times_hex or not temps_hex:
        return None

    # Parse header
    header_hex = header_info.get("value")
    header_val = int.from_bytes(
        bytes.fromhex(header_hex.replace(" ", "")), "little"
    ) if header_hex else None

    # Parse meta
    meta_hex = meta_info.get("value")
    meta_bytes = bytes.fromhex(meta_hex.replace(" ", "")) if meta_hex else b"\x00\x00\x00\x00"

    # Parse times (uint16 LE minute-of-week values)
    times_data = bytes.fromhex(times_hex.replace(" ", ""))
    times: list[int] = []
    for i in range(0, len(times_data), 2):
        val = struct.unpack_from("<H", times_data, i)[0]
        if val == 0 and i > 0 and all(b == 0 for b in times_data[i:]):
            break
        times.append(val)

    # Parse temps (1:1 with times)
    temps_data = bytes.fromhex(temps_hex.replace(" ", ""))
    temps = list(temps_data[: len(times)])

    if not times or all(t == 0 for t in times):
        return "  (no schedule)"

    lines = []
    lines.append(f"  Seq: {header_val}  Meta: {list(meta_bytes)} (byte0=0b{meta_bytes[0]:08b})")
    lines.append(f"  Events: {len(times)}")

    # Group into "nights" (sequences ending with OFF)
    night: list[tuple[int, int]] = []
    for i in range(len(times)):
        night.append((times[i], temps[i]))
        if temps[i] == 0:  # OFF marks end of a night
            start_day = _DAYS[night[0][0] // 1440] if night[0][0] // 1440 < 7 else "?"
            events = " → ".join(
                f"{_week_min_to_str(t)}={_temp_str(tmp)}" for t, tmp in night
            )
            lines.append(f"  {start_day} night: {events}")
            night = []
    # Handle trailing events with no OFF (partial schedule or bedtime-only)
    if night:
        events = " → ".join(
            f"{_week_min_to_str(t)}={_temp_str(tmp)}" for t, tmp in night
        )
        lines.append(f"  (open): {events}")

    return "\n".join(lines)


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
    if uuid == SCHEDULE_HEADER_CHAR:
        return f"seq={int.from_bytes(data, 'little')}"
    if uuid == SCHEDULE_META_CHAR:
        return f"meta={list(data)} (byte0=0b{data[0]:08b})"
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

    # If any schedule char changed, show decoded before/after
    sched_uuids = {SCHEDULE_HEADER_CHAR, SCHEDULE_TIMES_CHAR, SCHEDULE_TEMPS_CHAR, SCHEDULE_META_CHAR}
    if any(old_chars.get(u, {}).get("value") != new_chars.get(u, {}).get("value") for u in sched_uuids):
        old_sched = decode_schedule(old_chars)
        new_sched = decode_schedule(new_chars)
        if old_sched or new_sched:
            print("\n  SCHEDULE BEFORE:")
            print(old_sched or "  (none)")
            print("\n  SCHEDULE AFTER:")
            print(new_sched or "  (none)")
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

    # Decoded schedule view
    sched = decode_schedule(snapshot["characteristics"])
    if sched:
        print("SCHEDULE (decoded):")
        print(sched)
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
