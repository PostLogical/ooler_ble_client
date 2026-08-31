"""Establish what values each Ooler characteristic actually reports.

Before the library can reject a reading as "the device answered before it
was ready", it has to know what a healthy device emits -- an over-strict
range would discard real data, which is worse than the bug it fixes. So
this drives the device across its legal range and records every raw
response, and the observed values become the floor for any validation.

Phases (2-4 of the protocol; phase 1, draining the reservoir, is manual
and uses --phase sample alongside):

  setpoints  Write each boundary setpoint and read it back. The device
             clamps 46-54 onto 45 and 116-119 onto 120, so what a poll can
             return is a smaller set than what it accepts, and only the
             read-back values establish it. MIN_TEMP/MAX_TEMP are recorded
             as context, not as a range: they describe accepted input, and
             on these units MIN_TEMP (51 and 53) is itself inside the clamp
             band, so it is never a value a poll returns.
  temps      Sample ACTUALTEMP while the unit runs at LO and at HI, in
             both display units, to find the real operating envelope. This
             is the range we have the least evidence for.
  sample     Read-only sampling on an interval. Use during phase 1, or to
             watch an untouched device.

Phase 2b sweeps the mode enum. CLEAN is never written: starting a clean
changes the setpoint and, run to completion, arms the setpoint-override
behaviour the library works around, so its 0/1 domain is left to the
deep-clean logs. POWER is seen in both states across the run.

The device only accepts writes while powered on. If it is off and a write
phase was asked for, the script refuses unless --power-on is passed. The
original setpoint, mode, display unit and power state are restored at the
end, including after Ctrl+C or an error.

Settings are restored in a ``finally``, which covers a normal exit and
Ctrl+C but not the process being killed -- and a killed write phase leaves
the unit powered on at whichever extreme it had reached. Detach anything
long so a closing terminal cannot kill it:

    nohup python3 value_domain_sweep.py 601 --phase temps --power-on &

Usage:
    python3 value_domain_sweep.py --phase setpoints --power-on
    python3 value_domain_sweep.py --phase temps --power-on --soak 30
    python3 value_domain_sweep.py --phase sample            # read-only
    python3 value_domain_sweep.py 92106080601 --phase all --power-on
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
from collections import defaultdict
from collections.abc import Coroutine
from typing import Any
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from bleak.backends.device import BLEDevice

from ooler_ble_client.const import (
    ACTUALTEMP_CHAR,
    CLEAN_CHAR,
    DISPLAY_TEMPERATURE_UNIT_CHAR,
    MAX_TEMP_CHAR,
    MIN_TEMP_CHAR,
    MODE_CHAR,
    MODE_INT_TO_MODE_STATE,
    POWER_CHAR,
    SETTEMP_CHAR,
    TEMP_HI_F,
    TEMP_LO_F,
    WATER_LEVEL_CHAR,
)

# Read on every sample. Order matches the library's own poll.
SAMPLED: tuple[tuple[str, str], ...] = (
    ("power", POWER_CHAR),
    ("mode", MODE_CHAR),
    ("set_temperature", SETTEMP_CHAR),
    ("actual_temperature", ACTUALTEMP_CHAR),
    ("water_level", WATER_LEVEL_CHAR),
    ("clean", CLEAN_CHAR),
)

# Boundaries of the documented clamping behaviour, plus a midpoint. The
# device is expected to snap 46-53 to LO and 116-119 to HI; 54 and 116 are
# the values the library passes through as LO/HI requests.
SETPOINT_SWEEP: tuple[int, ...] = (
    TEMP_LO_F, 46, 53, 54, 55, 75, 115, 116, 119, TEMP_HI_F,
)

# The ranges proposed for library-side validation. Nothing here constrains
# the sweep -- they are printed against what was observed so that a range
# which would have rejected real data is impossible to miss.
PROPOSED_VALID: dict[str, str] = {
    "power": "0 or 1",
    "mode": "0-2",
    "clean": "0 or 1",
    "water_level": "1-100",
    "set_temperature": "45, 54-116, or 120 (raw F)",
    "actual_temperature": "33-125 F / 1-52 C",
}


def _proposed_rejects(field: str, value: int, unit: str) -> bool:
    """Would the proposed validation have thrown this reading away?"""
    if field in ("power", "clean"):
        return value not in (0, 1)
    if field == "mode":
        return not 0 <= value < len(MODE_INT_TO_MODE_STATE)
    if field == "water_level":
        return not 1 <= value <= 100
    if field == "set_temperature":
        return not (value in (TEMP_LO_F, TEMP_HI_F) or 54 <= value <= 116)
    if field == "actual_temperature":
        return not (1 <= value <= 52 if unit == "C" else 33 <= value <= 125)
    return False


# A dropped link used to end a run: every later read recorded an error, the
# restore could not land, and the unit was left at whichever extreme it had
# reached. Observed on a 90-minute soak that lost its link at 37 minutes
# with the host stationary, so range is not the only way this happens.
_RECONNECT_ATTEMPTS = 12
_RECONNECT_BACKOFF_SECONDS = 15.0
# BLEDevice handles can go stale; re-scan periodically rather than retrying
# a handle that will never work again.
_RESCAN_EVERY = 3


async def find_ooler(target: str | None, timeout: float) -> BLEDevice | None:
    """Scan for an Ooler by name fragment or address."""
    seen: dict[str, BLEDevice] = {}

    def on_detect(device: BLEDevice, _adv) -> None:  # noqa: ANN001
        if "ooler" in (device.name or "").lower():
            seen[device.address] = device

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
            d for d in found
            if needle in (d.name or "").lower() or needle in d.address.lower()
        ]
        if not matches:
            names = ", ".join(d.name or d.address for d in found)
            print(f"No Ooler matching {target!r}. Saw: {names}")
            return None
        return matches[0]
    if len(found) == 1:
        return found[0]
    print(f"Found {len(found)} Oolers:")
    for i, device in enumerate(found, start=1):
        print(f"  {i}. {device.name} ({device.address})")
    return found[int((input("Which one? [1]: ").strip() or "1")) - 1]


class Sweep:
    """Drives one device and accumulates every value it reported."""

    def __init__(self, device: BLEDevice, out: Path) -> None:
        self._device = device
        self._client: BleakClient | None = None
        self._fh = out.open("a", encoding="utf-8")
        self._observed: dict[str, set[int]] = defaultdict(set)
        self._rejected: list[dict[str, object]] = []
        self.unit = "F"
        # The device's own published setpoint bounds, read in phase 2.
        self.min_temp: int | None = None
        self.max_temp: int | None = None
        # Link losses survived, and how long they cost. Reported in the
        # summary: a run with gaps is weaker evidence than one without, and
        # a gap that swallowed the end of a soak can make a still-moving
        # temperature look settled.
        self.reconnects = 0
        self.gap_seconds = 0.0

    def close(self) -> None:
        self._fh.close()

    async def connect(self) -> None:
        """Open the link, or raise if it cannot be opened at all."""
        client = BleakClient(self._device, timeout=20.0)
        await client.connect()
        self._client = client

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - diagnostic script
                pass

    async def ensure_connected(self, why: str) -> bool:
        """Reconnect if the link is gone. False if it stayed gone.

        Re-scans every few attempts: a BLEDevice handle can go stale, and
        retrying a dead handle forever gets nowhere.
        """
        client = self._client
        if client is not None and client.is_connected:
            return True
        started = time.monotonic()
        for attempt in range(1, _RECONNECT_ATTEMPTS + 1):
            await self.disconnect()
            if attempt % _RESCAN_EVERY == 0:
                found = await find_ooler(self._device.address, 8.0)
                if found is not None:
                    self._device = found
            try:
                await self.connect()
            except Exception as err:  # noqa: BLE001 - diagnostic script
                print(f"  reconnect {attempt}/{_RECONNECT_ATTEMPTS} failed"
                      f" ({why}): {type(err).__name__}: {err}")
                await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
                continue
            gap = time.monotonic() - started
            self.reconnects += 1
            self.gap_seconds += gap
            print(f"  reconnected after {gap:.0f}s ({why})")
            return True
        print(f"  giving up reconnecting ({why})")
        return False

    async def read(self, uuid: str) -> dict[str, object]:
        """Read a characteristic, reconnecting once before giving up."""
        for attempt in (1, 2):
            client = self._client
            try:
                if client is None or not client.is_connected:
                    raise BleakError("not connected")
                data = bytes(await client.read_gatt_char(uuid))
                return {"hex": data.hex(), "len": len(data),
                        "int": int.from_bytes(data, "little")}
            except Exception as err:  # noqa: BLE001 - diagnostic script
                if attempt == 2 or not await self.ensure_connected(f"read {uuid[:8]}"):
                    return {"error": f"{type(err).__name__}: {err}"}
        return {"error": "unreachable"}

    async def read_int(self, uuid: str) -> int | None:
        """Read a characteristic as an int, or None if it failed or was empty."""
        result = await self.read(uuid)
        value = result.get("int")
        return value if isinstance(value, int) and result.get("len") else None

    async def write(self, uuid: str, value: int) -> bool:
        """Write a byte, reconnecting once before giving up."""
        for attempt in (1, 2):
            client = self._client
            try:
                if client is None or not client.is_connected:
                    raise BleakError("not connected")
                await client.write_gatt_char(
                    uuid, value.to_bytes(1, "little"), response=True
                )
                return True
            except Exception as err:  # noqa: BLE001 - diagnostic script
                if attempt == 2 or not await self.ensure_connected("write"):
                    print(f"  write to {uuid[:8]} failed: {type(err).__name__}: {err}")
                    return False
        return False

    async def sample(self, phase: str, label: str) -> dict[str, object]:
        """Read every sampled characteristic once and record the result."""
        record: dict[str, object] = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "device": self._device.name,
            "phase": phase,
            "label": label,
            "display_unit": self.unit,
        }
        reads: dict[str, object] = {}
        for field, uuid in SAMPLED:
            result = await self.read(uuid)
            reads[field] = result
            value = result.get("int")
            if isinstance(value, int) and result.get("len"):
                # ACTUALTEMP reports in the display unit, so pooling both
                # would produce a domain spanning two scales and describing
                # neither -- 20C and 67F are the same reading.
                key = (
                    f"{field} ({self.unit})"
                    if field == "actual_temperature"
                    else field
                )
                self._observed[key].add(value)
                if _proposed_rejects(field, value, self.unit):
                    self._rejected.append(
                        {"field": field, "value": value, "unit": self.unit,
                         "phase": phase, "label": label}
                    )
        record["reads"] = reads
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()
        return record

    def describe(self, record: dict[str, object]) -> str:
        reads = record["reads"]
        assert isinstance(reads, dict)
        parts = [f"[{record['label']:>14}]"]
        for field, _ in SAMPLED:
            result = reads[field]
            assert isinstance(result, dict)
            shown = result.get("hex") if result.get("len") else result.get("error", "<empty>")
            parts.append(f"{field.split('_')[0]}={shown}")
        return "  ".join(parts)

    async def soak(self, phase: str, label: str, seconds: float, interval: float) -> None:
        """Sample on an interval for a while, printing changes only."""
        deadline = asyncio.get_running_loop().time() + seconds
        previous: str | None = None
        while asyncio.get_running_loop().time() < deadline:
            record = await self.sample(phase, label)
            line = self.describe(record)
            if line != previous:
                print(f"  {datetime.now():%H:%M:%S}  {line}")
                previous = line
            await asyncio.sleep(interval)

    async def set_unit(self, unit: str) -> None:
        """Switch the display unit; ACTUALTEMP reports in whatever it is."""
        await self.write(DISPLAY_TEMPERATURE_UNIT_CHAR, 1 if unit == "C" else 0)
        self.unit = unit
        await asyncio.sleep(1.0)

    async def set_power(self, on: bool) -> None:
        await self.write(POWER_CHAR, int(on))
        await asyncio.sleep(2.0)

    async def set_setpoint(self, temp_f: int) -> None:
        """Write a setpoint. Always Fahrenheit, whatever the display unit."""
        await self.write(SETTEMP_CHAR, temp_f)

    async def verify_restore(
        self,
        power: int | None,
        setpoint: int | None,
        mode: int | None,
        unit: int | None,
    ) -> bool:
        """Read the settings back and confirm they match the baseline.

        Restoring is a write like any other, and the device drops writes
        while off -- so a restore can fail silently and leave the unit
        somewhere the operator did not put it. Checking costs four reads.
        """
        print("\nVerifying the restore ...")
        checks = (
            ("power", power, await self.read_int(POWER_CHAR)),
            ("setpoint", setpoint, await self.read_int(SETTEMP_CHAR)),
            ("mode", mode, await self.read_int(MODE_CHAR)),
            ("display unit", unit, await self.read_int(DISPLAY_TEMPERATURE_UNIT_CHAR)),
        )
        ok = True
        for label, expected, actual in checks:
            if expected is None:
                continue
            if expected == actual:
                print(f"  {label:13} {actual}  ok")
            else:
                ok = False
                print(f"  {label:13} {actual}  MISMATCH, expected {expected}")
        if not ok:
            print("\n  The device was NOT left as it was found. Put it back by hand.")
        return ok

    async def phase_setpoints(
        self, settle: float, values: tuple[int, ...]
    ) -> None:
        """Write each boundary setpoint and read back what the device kept."""
        print("\n--- Phase 2: setpoint domain ---")
        print("  Bounds the device publishes about itself:")
        for name, uuid in (("MIN_TEMP", MIN_TEMP_CHAR), ("MAX_TEMP", MAX_TEMP_CHAR)):
            result = await self.read(uuid)
            value = result.get("int") if result.get("len") else None
            if isinstance(value, int):
                setattr(self, name.lower(), value)
                print(f"    {name:9} {value:>4} F   (raw {result.get('hex')})")
            else:
                print(f"    {name:9}  --      {result}")
            self._fh.write(
                json.dumps({"phase": "setpoints", "label": name, "read": result}) + "\n"
            )
        if self.min_temp is not None and self.max_temp is not None:
            print(
                f"    LO sentinel {TEMP_LO_F} F sits below MIN_TEMP; "
                f"HI sentinel {TEMP_HI_F} F vs MAX_TEMP {self.max_temp} F"
            )

        print(f"  {'written':>9}  {'read back':>9}   raw")
        readbacks: set[int | None] = set()
        for wanted in values:
            await self.set_setpoint(wanted)
            await asyncio.sleep(settle)
            record = await self.sample("setpoints", f"wrote {wanted}")
            reads = record["reads"]
            assert isinstance(reads, dict)
            got = reads["set_temperature"]
            assert isinstance(got, dict)
            value = got.get("int")
            readbacks.add(value if isinstance(value, int) else None)
            note = "" if value == wanted else "  <- clamped"
            print(f"  {wanted:>9}  {value:>9}   {got.get('hex')}{note}")

        if len(readbacks) == 1:
            # Every write read back the same value, so none of them landed.
            # The sweep recorded a domain of one, which is not the device's.
            print(
                "\n  WARNING: the setpoint never changed across the whole sweep."
                "\n  The device is dropping writes -- check that it is powered on."
                "\n  This phase's data is not usable."
            )

    async def phase_modes(self, settle: float) -> None:
        """Write each mode and read it back.

        The protocol assumed the enum domain would be confirmed free of
        charge by whatever else was running, but nothing else changes the
        mode -- a sweep that never leaves Regular proves only that Regular
        exists. CLEAN is deliberately not exercised here: starting a clean
        changes the setpoint and, run to completion, arms the very
        setpoint-override behaviour the library works around.
        """
        print("\n--- Phase 2b: mode domain ---")
        print(f"  {'written':>9}  {'read back':>9}   name")
        for mode_int, name in enumerate(MODE_INT_TO_MODE_STATE):
            await self.write(MODE_CHAR, mode_int)
            await asyncio.sleep(settle)
            record = await self.sample("modes", f"wrote {name}")
            reads = record["reads"]
            assert isinstance(reads, dict)
            got = reads["mode"]
            assert isinstance(got, dict)
            value = got.get("int")
            note = "" if value == mode_int else "  <- not accepted"
            print(f"  {mode_int:>9}  {value:>9}   {name}{note}")

    async def phase_hold(
        self, extreme: str, soak_seconds: float, interval: float
    ) -> None:
        """Hold one extreme in Fahrenheit until the temperature stops moving.

        The paired soaks in --phase temps are 30 minutes each and none of
        them plateaued: HI was still climbing 8F in its final third. An
        envelope taken from a reading that was still moving understates
        the range, and a validation bound drawn from it would sit inside
        what the device really reaches. So hold one end, long.
        """
        setpoint = TEMP_HI_F if extreme == "HI" else TEMP_LO_F
        print(f"\n--- Holding {extreme} ({setpoint}F) for "
              f"{soak_seconds / 60:.0f} min ---")
        await self.set_unit("F")
        await self.set_setpoint(setpoint)
        await self.soak("hold", f"F/{extreme}", soak_seconds, interval)

    async def phase_temps(self, soak_seconds: float, interval: float) -> None:
        """Sample ACTUALTEMP across the operating envelope, in both units."""
        print("\n--- Phase 3: actual temperature envelope ---")
        extremes = [("LO", TEMP_LO_F), ("HI", TEMP_HI_F)]
        for unit in ("F", "C"):
            await self.set_unit(unit)
            print(f"\n  Display unit {unit}")
            for label, setpoint in extremes:
                print(f"  Driving to {label} ({setpoint}F) for {soak_seconds / 60:.1f} min")
                await self.set_setpoint(setpoint)
                await self.soak("temps", f"{unit}/{label}", soak_seconds, interval)
            # The display unit changes how the temperature is reported, not
            # the water. Reversing the order means the second pass starts at
            # the extreme the first pass ended on, so the unit is not driven
            # the full span again just to re-read it -- half an hour saved
            # and one less full heat-cool cycle on the hardware.
            extremes.reverse()

    def summary(self) -> None:
        """Print the observed domain per field against what was proposed."""
        print("\n" + "=" * 72)
        print("Observed value domains")
        print("-" * 72)
        keys = [f for f, _ in SAMPLED if f != "actual_temperature"]
        keys += sorted(k for k in self._observed if k.startswith("actual_temperature"))
        for key in keys:
            field = key.split(" (")[0]
            values = sorted(self._observed.get(key, set()))
            if not values:
                print(f"  {key:22} (nothing read)")
                continue
            shown = (
                ", ".join(str(v) for v in values)
                if len(values) <= 12
                else f"{len(values)} distinct, {min(values)}..{max(values)}"
            )
            print(f"  {key:22} {shown}")
            print(f"  {'':22} proposed valid: {PROPOSED_VALID[field]}")
        if self.min_temp is not None and self.max_temp is not None:
            print(f"  device-published setpoint bounds: "
                  f"{self.min_temp}-{self.max_temp} F (what it ACCEPTS)")
            print("  Not a validation range: the device clamps what it accepts")
            print("  onto a smaller set of values it reports back, so only the")
            print("  written/read-back pairs above say what a poll can return.")
        if self.reconnects or self.gap_seconds:
            print(f"  link losses survived: {self.reconnects}, "
                  f"{self.gap_seconds:.0f}s total offline")
            print("  Samples are missing for those gaps. A soak that lost its")
            print("  link near the end can look settled when it was still moving.")
        print("-" * 72)
        if self._rejected:
            print(f"  {len(self._rejected)} reading(s) the proposed ranges WOULD HAVE")
            print("  REJECTED. Every one is a healthy device's real output, so the")
            print("  range is wrong, not the reading:")
            for item in self._rejected:
                print(f"    {item['field']}={item['value']} ({item['unit']}) "
                      f"during {item['phase']}/{item['label']}")
        else:
            print("  No observed reading would have been rejected.")
        print("=" * 72)


async def run(args: argparse.Namespace) -> int:
    print(f"Scanning for Oolers ({args.scan_timeout}s) ...")
    device = await find_ooler(args.target, args.scan_timeout)
    if device is None:
        print("No Ooler devices found.")
        return 1
    print(f"Target: {device.name} ({device.address})")

    writes_needed = args.phase in ("setpoints", "temps", "hold", "all")
    # Named per device: both units can be swept concurrently, and a
    # timestamp alone collides when two processes start in the same second.
    slug = (device.name or device.address).replace(":", "").lower()
    out = args.out or Path(__file__).parent / (
        f"value_domain_{slug}_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    )

    sweep = Sweep(device, out)
    await sweep.connect()
    try:
        # Everything that will be put back afterwards.
        original_power = await sweep.read_int(POWER_CHAR)
        original_setpoint = await sweep.read_int(SETTEMP_CHAR)
        original_mode = await sweep.read_int(MODE_CHAR)
        original_unit = await sweep.read_int(DISPLAY_TEMPERATURE_UNIT_CHAR)
        sweep.unit = "C" if original_unit == 1 else "F"
        print(
            f"Baseline: power={original_power} setpoint={original_setpoint}F "
            f"mode={original_mode} unit={sweep.unit}"
        )

        if writes_needed and not original_power:
            if not args.power_on:
                print(
                    "\nThe device is off and drops writes while off, so this phase"
                    "\nwould record nothing. Re-run with --power-on to let the"
                    "\nscript turn it on; it is turned back off at the end."
                )
                sweep.close()
                return 2
            print("Device is off; powering on for the sweep (restored at the end).")
            await sweep.set_power(True)

        try:
            await sweep.sample("baseline", "start")
            if args.phase in ("setpoints", "all"):
                await sweep.phase_setpoints(
                    args.settle,
                    (TEMP_LO_F, 75, TEMP_HI_F) if args.smoke else SETPOINT_SWEEP,
                )
                await sweep.phase_modes(args.settle)
            if args.phase in ("temps", "all"):
                await sweep.phase_temps(args.soak * 60, args.sample_interval)
            if args.phase == "hold":
                await sweep.phase_hold(
                    args.hold, args.soak * 60, args.sample_interval
                )
            if args.phase == "sample":
                print(
                    f"\n--- Sampling every {args.sample_interval}s. Ctrl+C to stop. ---"
                    "\nFor phase 1, drain the reservoir in steps and note the time of"
                    "\neach step; changes print as they happen."
                )
                await sweep.soak(
                    "sample", args.label or "sample", args.duration * 60,
                    args.sample_interval,
                )
        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            # Restore only what was written. Sampling is promised read-only
            # and runs while the operator is mid-drain, so writing the same
            # values back "harmlessly" would still break that promise.
            if writes_needed:
                print("\nRestoring original settings ...")
                if not await sweep.ensure_connected("restore"):
                    print("  NO LINK -- the device is still powered on at the"
                          "\n  sweep's setpoint and must be put back by hand.")

                async def restore(
                    label: str, action: Coroutine[Any, Any, object]
                ) -> None:
                    """Put one setting back, reporting rather than raising.

                    A failure here leaves the device somewhere the operator
                    did not put it, so it must be said out loud -- but it
                    must not stop the remaining settings being restored.
                    """
                    try:
                        await action
                    except Exception as err:  # noqa: BLE001 - diagnostic script
                        print(f"  WARNING: could not restore {label}: {err}")

                await restore(
                    "display unit", sweep.set_unit("C" if original_unit == 1 else "F")
                )
                if original_setpoint is None:
                    print("  WARNING: no baseline setpoint to restore")
                else:
                    await restore("setpoint", sweep.set_setpoint(original_setpoint))
                if original_mode is None:
                    print("  WARNING: no baseline mode to restore")
                else:
                    await restore("mode", sweep.write(MODE_CHAR, original_mode))
                # Power last: it is what decides whether the device is left
                # running, and the writes above only land while it is on.
                await restore("power", sweep.set_power(bool(original_power)))
                restored = await sweep.verify_restore(
                    original_power, original_setpoint, original_mode, original_unit
                )
                if args.smoke:
                    print(
                        "\nSmoke test: the full path ran end to end."
                        f"\n  restore verified: {'yes' if restored else 'NO'}"
                        "\n  Check above that reads returned bytes, that the"
                        "\n  setpoint readback changed as it was written, and that"
                        "\n  the summary lists a domain per field. If all three"
                        "\n  hold, the long run will collect what it needs."
                    )
            sweep.summary()
            print(f"Raw samples written to {out}")
            sweep.close()
    finally:
        await sweep.disconnect()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record the value domain of each Ooler characteristic"
    )
    parser.add_argument("target", nargs="?", help="Name fragment or address")
    parser.add_argument(
        "--phase", choices=("setpoints", "temps", "sample", "hold", "all"),
        default="all",
    )
    parser.add_argument(
        "--hold", choices=("LO", "HI"), default="HI",
        help="Which extreme --phase hold drives to (default: HI)",
    )
    parser.add_argument(
        "--power-on", action="store_true",
        help="Allow powering the device on for write phases (restored afterwards)",
    )
    parser.add_argument(
        "--settle", type=float, default=3.0,
        help="Seconds to wait after a setpoint write before reading back (default: 3)",
    )
    parser.add_argument(
        "--soak", type=float, default=30.0,
        help="Minutes to hold each temperature extreme (default: 30)",
    )
    parser.add_argument(
        "--sample-interval", type=float, default=5.0,
        help="Seconds between samples (default: 5)",
    )
    parser.add_argument(
        "--duration", type=float, default=600.0,
        help="Minutes to sample in --phase sample (default: 600)",
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help="Exercise the whole path in ~2 minutes: three setpoints, 30s soaks. "
             "Confirms the run collects what it needs, and that the restore at "
             "the end works, before committing two hours to it.",
    )
    parser.add_argument("--label", help="Free-text label recorded on each sample")
    parser.add_argument("--scan-timeout", type=float, default=10.0)
    parser.add_argument("--out", type=Path, help="JSONL output path")
    args = parser.parse_args()
    # Line-buffer stdout. Redirected to a file it is block-buffered, so a
    # process killed mid-run loses everything it had printed -- which is
    # how a killed sweep left no record of how far it got, while its JSONL
    # survived because that is flushed per sample.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    if args.smoke:
        # Same code path as the real run, small enough to sit through.
        args.soak = min(args.soak, 0.5)
        args.settle = min(args.settle, 1.5)
        args.sample_interval = min(args.sample_interval, 3.0)

    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
