"""Reproduce the spurious water_level=0 on the post-connect initial read.

Hammers connect/read/disconnect cycles and records the RAW bytes of the
water-level read, which the library never logs. That distinguishes the
two candidate causes the field data cannot:

  * a zero-length read (b"")  -> the read itself failed and bleak handed
    back nothing; int.from_bytes() turns it into 0
  * a literal zero byte (b"\\x00") -> the firmware answered with a
    placeholder because the value is not populated yet

WATER_LEVEL_CHAR reports 1, 50 or 100, so anything else is out of the
device's value domain and counts as a bad read here.

After the initial read, the water level is re-read every few hundred ms
until it becomes valid, giving a time-to-valid for each cycle. PUMP_LEVEL
and THERMAL_EFFORT are captured alongside to test whether the value only
settles once the pump path is active.

This script is READ-ONLY: it never writes to the device, so it cannot
change power state. Set the unit on or off yourself and pass --label to
record which condition a run represents.

Note on addresses: on macOS, bleak reports CoreBluetooth UUIDs rather
than MACs, so the ESPHome-side MAC (84:71:27:...) will not match. Pass a
name fragment instead (e.g. 92106080601), or omit the target to scan.

Usage:
    python3 water_level_race.py                       # scan and pick
    python3 water_level_race.py 92106080601 --cycles 40
    python3 water_level_race.py 601 --label robooler-off --cycles 40
    python3 water_level_race.py 601 --order water-first  # read it first
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from ooler_ble_client.const import (
    ACTUALTEMP_CHAR,
    CLEAN_CHAR,
    DISPLAY_TEMPERATURE_UNIT_CHAR,
    MODE_CHAR,
    POWER_CHAR,
    PUMP_LEVEL_CHAR,
    SETTEMP_CHAR,
    THERMAL_EFFORT_CHAR,
    WATER_LEVEL_CHAR,
)

# Values WATER_LEVEL_CHAR is known to report; anything else is a bad read.
VALID_WATER_LEVELS = (1, 50, 100)

# The order OolerBLEDevice._read_all_characteristics uses on connect,
# with the temperature unit read first as _ensure_connected does.
LIBRARY_READ_ORDER: tuple[tuple[str, str], ...] = (
    ("temp_unit", DISPLAY_TEMPERATURE_UNIT_CHAR),
    ("power", POWER_CHAR),
    ("mode", MODE_CHAR),
    ("set_temp", SETTEMP_CHAR),
    ("actual_temp", ACTUALTEMP_CHAR),
    ("water_level", WATER_LEVEL_CHAR),
    ("clean", CLEAN_CHAR),
)

# Extra context reads, taken after the batch so they never delay it.
CONTEXT_READS: tuple[tuple[str, str], ...] = (
    ("pump_level", PUMP_LEVEL_CHAR),
    ("thermal_effort", THERMAL_EFFORT_CHAR),
)


def _record_read(data: bytes) -> dict[str, object]:
    """Capture a read verbatim: hex, length, and the library's parse."""
    return {
        "hex": data.hex(),
        "len": len(data),
        "int": int.from_bytes(data, "little"),
    }


async def _read(client: BleakClient, uuid: str) -> dict[str, object]:
    """Read one characteristic, recording an error instead of raising."""
    try:
        return _record_read(bytes(await client.read_gatt_char(uuid)))
    except Exception as err:  # noqa: BLE001 - diagnostic script
        return {"error": f"{type(err).__name__}: {err}"}


async def find_ooler(
    target: str | None, timeout: float
) -> tuple[BLEDevice, int | None] | None:
    """Scan for an Ooler, returning the device and its advertised RSSI."""
    seen: dict[str, tuple[BLEDevice, int | None]] = {}

    def on_detect(device: BLEDevice, adv) -> None:  # noqa: ANN001
        name = device.name or ""
        if "ooler" not in name.lower():
            return
        seen[device.address] = (device, adv.rssi)

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()

    found = list(seen.values())
    if not found:
        return None

    if target:
        needle = target.lower()
        matches = [
            entry
            for entry in found
            if needle in (entry[0].name or "").lower()
            or needle in entry[0].address.lower()
        ]
        if not matches:
            print(f"No Ooler matching {target!r}. Saw: "
                  f"{', '.join(d.name or d.address for d, _ in found)}")
            return None
        return matches[0]

    if len(found) == 1:
        return found[0]

    print(f"Found {len(found)} Oolers:")
    for i, (device, rssi) in enumerate(found, start=1):
        print(f"  {i}. {device.name} ({device.address})  rssi={rssi}")
    choice = input("Which one? [1]: ").strip() or "1"
    return found[int(choice) - 1]


async def run_cycle(
    device: BLEDevice,
    rssi: int | None,
    cycle: int,
    order: str,
    settle_interval: float,
    settle_timeout: float,
) -> dict[str, object]:
    """Connect, take the initial reads, then watch water level settle."""
    result: dict[str, object] = {
        "cycle": cycle,
        "timestamp": datetime.now().astimezone().isoformat(),
        "address": device.address,
        "name": device.name,
        "rssi": rssi,
    }

    connect_started = time.monotonic()
    client = BleakClient(device, timeout=20.0)
    try:
        await client.connect()
    except Exception as err:  # noqa: BLE001 - diagnostic script
        result["connect_error"] = f"{type(err).__name__}: {err}"
        result["connect_seconds"] = round(time.monotonic() - connect_started, 3)
        return result

    result["connect_seconds"] = round(time.monotonic() - connect_started, 3)
    connected_at = time.monotonic()

    try:
        reads: dict[str, object] = {}
        if order == "water-first":
            reads["water_level"] = await _read(client, WATER_LEVEL_CHAR)
            result["water_read_offset"] = round(time.monotonic() - connected_at, 3)
        for field, uuid in LIBRARY_READ_ORDER:
            if field in reads:
                continue
            if field == "water_level":
                result["water_read_offset"] = round(
                    time.monotonic() - connected_at, 3
                )
            reads[field] = await _read(client, uuid)
        for field, uuid in CONTEXT_READS:
            reads[field] = await _read(client, uuid)
        result["initial"] = reads

        initial_water = reads.get("water_level", {})
        level = initial_water.get("int") if isinstance(initial_water, dict) else None
        bad = level not in VALID_WATER_LEVELS
        result["bad_initial_read"] = bad

        # Re-read until the level lands in the known domain.
        settle: list[dict[str, object]] = []
        time_to_valid: float | None = 0.0 if not bad else None
        if bad:
            deadline = time.monotonic() + settle_timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(settle_interval)
                sample = await _read(client, WATER_LEVEL_CHAR)
                offset = round(time.monotonic() - connected_at, 3)
                sample["offset"] = offset
                sample["pump_level"] = await _read(client, PUMP_LEVEL_CHAR)
                settle.append(sample)
                if sample.get("int") in VALID_WATER_LEVELS:
                    time_to_valid = offset
                    break
        result["settle"] = settle
        result["time_to_valid"] = time_to_valid
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - diagnostic script
            pass

    return result


def describe_cycle(result: dict[str, object]) -> str:
    """One console line summarising a cycle."""
    cycle = result["cycle"]
    if "connect_error" in result:
        return f"[{cycle:3d}] CONNECT FAILED after " \
               f"{result['connect_seconds']}s: {result['connect_error']}"

    initial = result.get("initial", {})
    water = initial.get("water_level", {}) if isinstance(initial, dict) else {}
    power = initial.get("power", {}) if isinstance(initial, dict) else {}
    pump = initial.get("pump_level", {}) if isinstance(initial, dict) else {}

    if "error" in water:
        water_str = f"READ ERROR ({water['error']})"
    else:
        water_str = f"hex={water.get('hex') or '<empty>'} int={water.get('int')}"

    verdict = "BAD " if result.get("bad_initial_read") else "ok  "
    settle_str = ""
    if result.get("bad_initial_read"):
        ttv = result.get("time_to_valid")
        settle_str = (
            f"  valid after {ttv}s" if ttv is not None else "  never became valid"
        )

    return (
        f"[{cycle:3d}] {verdict} connect={result['connect_seconds']}s "
        f"read@{result.get('water_read_offset')}s  water: {water_str}  "
        f"power={power.get('int')} pump={pump.get('int')}{settle_str}"
    )


def summarise(results: list[dict[str, object]], label: str | None) -> None:
    """Print aggregate stats over all cycles."""
    completed = [r for r in results if "connect_error" not in r]
    failed = len(results) - len(completed)
    bad = [r for r in completed if r.get("bad_initial_read")]

    print()
    print("=" * 70)
    print(f"Summary{f' [{label}]' if label else ''}")
    print("-" * 70)
    print(f"  cycles run        : {len(results)}")
    print(f"  connect failures  : {failed}")
    print(f"  completed         : {len(completed)}")
    if completed:
        rate = 100.0 * len(bad) / len(completed)
        print(f"  bad initial reads : {len(bad)} ({rate:.1f}%)")

    if bad:
        empty = sum(
            1
            for r in bad
            if r["initial"]["water_level"].get("len") == 0  # type: ignore[index]
        )
        zero_byte = sum(
            1
            for r in bad
            if r["initial"]["water_level"].get("len", 0) > 0  # type: ignore[index]
            and r["initial"]["water_level"].get("int") == 0  # type: ignore[index]
        )
        errored = sum(
            1
            for r in bad
            if "error" in r["initial"]["water_level"]  # type: ignore[index]
        )
        print(f"    zero-length read (b\"\")     : {empty}")
        print(f"    zero byte (e.g. b\"\\x00\")   : {zero_byte}")
        print(f"    read raised                 : {errored}")

        ttvs = [r["time_to_valid"] for r in bad if r.get("time_to_valid") is not None]
        if ttvs:
            print(f"  time-to-valid     : min={min(ttvs)}s "
                  f"median={statistics.median(ttvs)}s max={max(ttvs)}s")
        never = len(bad) - len(ttvs)
        if never:
            print(f"  never became valid: {never}")

        good_connects = [
            r["connect_seconds"] for r in completed if not r.get("bad_initial_read")
        ]
        bad_connects = [r["connect_seconds"] for r in bad]
        if good_connects and bad_connects:
            print(
                f"  connect seconds   : good median="
                f"{statistics.median(good_connects)}s  "
                f"bad median={statistics.median(bad_connects)}s"
            )
    print("=" * 70)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reproduce the spurious water_level=0 initial read"
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="Name fragment or address (scans and prompts if omitted)",
    )
    parser.add_argument(
        "--cycles", type=int, default=25, help="Connect/read cycles (default: 25)"
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=5.0,
        help="Seconds between cycles (default: 5)",
    )
    parser.add_argument(
        "--order",
        choices=("library", "water-first"),
        default="library",
        help="Read order: mirror the library, or read water level first",
    )
    parser.add_argument(
        "--settle-interval",
        type=float,
        default=0.25,
        help="Re-read interval while waiting for a valid level (default: 0.25)",
    )
    parser.add_argument(
        "--settle-timeout",
        type=float,
        default=15.0,
        help="Give up waiting for a valid level after this long (default: 15)",
    )
    parser.add_argument(
        "--scan-timeout", type=float, default=10.0, help="Scan seconds (default: 10)"
    )
    parser.add_argument(
        "--label",
        help="Free-text condition for this run, e.g. 'robooler-off'",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="JSONL output path (default: water_level_race_<timestamp>.jsonl here)",
    )
    args = parser.parse_args()

    print(f"Scanning for Oolers ({args.scan_timeout}s) ...")
    found = await find_ooler(args.target, args.scan_timeout)
    if found is None:
        print("No Ooler devices found.")
        sys.exit(1)
    device, rssi = found
    print(f"Target: {device.name} ({device.address})  rssi={rssi}")
    print(
        f"Running {args.cycles} cycles, {args.gap}s apart, order={args.order}"
        f"{f', label={args.label}' if args.label else ''}"
    )
    print("Leave the unit in the power state you want to test; "
          "this script never writes to it.")
    print("-" * 70)

    out_path = args.out or Path(__file__).parent / (
        f"water_level_race_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    )

    results: list[dict[str, object]] = []
    try:
        with out_path.open("a", encoding="utf-8") as fh:
            for cycle in range(1, args.cycles + 1):
                result = await run_cycle(
                    device,
                    rssi,
                    cycle,
                    args.order,
                    args.settle_interval,
                    args.settle_timeout,
                )
                if args.label:
                    result["label"] = args.label
                result["order"] = args.order
                results.append(result)
                fh.write(json.dumps(result) + "\n")
                fh.flush()
                print(describe_cycle(result))
                if cycle < args.cycles:
                    await asyncio.sleep(args.gap)
    except KeyboardInterrupt:
        print("\nInterrupted.")

    if results:
        summarise(results, args.label)
        print(f"Raw cycles written to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
