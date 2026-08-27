"""Capture a labelled snapshot of both Oolers, and diff against the previous one.

Bracket every state change with this: run it before you touch a unit, and
again immediately after. The diff is what identifies whatever changed.

Usage:
    python3 diagnostics/capture.py "before factory reset"
    python3 diagnostics/capture.py "after factory reset"
    python3 diagnostics/capture.py --list
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import sys
from datetime import datetime

from bleak import BleakClient, BleakScanner

SNAP_DIR = "snapshots"

# Values that move on their own; shown separately so they don't bury real changes.
VOLATILE = {
    "ACTUALTEMP",
    "AMBIENT_TEMPERATURE_F",
    "RELATIVE_HUMIDITY",
    "THERMAL_EFFORT",
    "POWER_RAIL",
    "PUMP_LEVEL",
    "CURRENT_TIME",
    "LIFETIME",
    "RUNTIME",
    "UV_RUNTIME",
    "DEVICE_LOGS",
    "WATER_LEVEL",
    # Changes between consecutive reads even on an untouched unit (verified
    # 2026-08-26 against 601 while only 603 was being operated on). Opaque.
    "SCHEDULE_HEADER",
}


def char_names() -> dict[str, str]:
    from ooler_ble_client import const

    names = {}
    for key in dir(const):
        val = getattr(const, key)
        if isinstance(val, str) and re.fullmatch(r"[0-9a-f\-]{36}", val):
            names[val] = key.replace("_CHAR", "")
    return names


async def capture_device(address: str, name: str, label: str) -> dict:
    chars: dict[str, dict] = {}
    async with BleakClient(address, timeout=20.0) as client:
        for service in client.services:
            for char in service.characteristics:
                entry = {
                    "service": service.uuid,
                    "properties": list(char.properties),
                }
                if "read" in char.properties:
                    try:
                        entry["value"] = (await client.read_gatt_char(char)).hex(" ")
                    except Exception as exc:  # noqa: BLE001
                        entry["error"] = str(exc)
                chars[char.uuid] = entry
    return {
        "device": name,
        "address": address,
        "timestamp": datetime.now().isoformat(),
        "label": label,
        "characteristics": chars,
    }


def previous_snapshot(name: str, exclude: str) -> dict | None:
    files = sorted(f for f in glob.glob(f"{SNAP_DIR}/{name}_*.json") if f != exclude)
    if not files:
        return None
    with open(files[-1]) as fh:
        return json.load(fh)


def show_diff(prev: dict, cur: dict, names: dict[str, str]) -> None:
    old = {u: c.get("value") for u, c in prev["characteristics"].items()}
    new = {u: c.get("value") for u, c in cur["characteristics"].items()}
    interesting, noise = [], []
    for uuid in sorted(set(old) | set(new)):
        a, b = old.get(uuid), new.get(uuid)
        if a is None or b is None or a == b:
            continue
        label = names.get(uuid, uuid[:8])
        row = f"    {label:<24} {a[:30]:<32} -> {b[:30]}"
        (noise if label in VOLATILE else interesting).append(row)

    prev_label = prev.get("label") or prev["timestamp"][:16]
    print(f"  diff vs '{prev_label}' ({prev['timestamp'][:19]}):")
    if interesting:
        print("  CHANGED:")
        print("\n".join(interesting))
    else:
        print("    (nothing changed outside known-volatile values)")
    if noise:
        print(f"  volatile ({len(noise)}): " + ", ".join(
            r.split()[0] for r in noise
        ))


async def main() -> None:
    args = [a for a in sys.argv[1:]]
    if args and args[0] == "--list":
        for path in sorted(glob.glob(f"{SNAP_DIR}/*.json")):
            with open(path) as fh:
                snap = json.load(fh)
            lbl = snap.get("label") or "(no label)"
            print(f"  {snap['timestamp'][:19]}  {snap['device']:<20} {lbl}")
        return

    label = " ".join(args).strip()
    if not label:
        print('Give this capture a label, e.g.:\n  python3 diagnostics/capture.py "before unplug"')
        sys.exit(2)

    os.makedirs(SNAP_DIR, exist_ok=True)
    names = char_names()

    print("Scanning ...")
    found = await BleakScanner.discover(timeout=12.0)
    oolers = [d for d in found if d.name and "ooler" in d.name.lower()]
    if not oolers:
        print("No Oolers found. Is HA holding the connection, or are you out of range?")
        sys.exit(1)

    for dev in sorted(oolers, key=lambda d: d.name or ""):
        print(f"\n=== {dev.name} — capturing '{label}'")
        try:
            snap = await capture_device(dev.address, dev.name or "unknown", label)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: {exc}")
            continue
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"{SNAP_DIR}/{dev.name}_{stamp}.json"
        with open(path, "w") as fh:
            json.dump(snap, fh, indent=1)
        print(f"  saved {path}")
        prev = previous_snapshot(dev.name or "unknown", path)
        if prev:
            show_diff(prev, snap, names)


if __name__ == "__main__":
    asyncio.run(main())
