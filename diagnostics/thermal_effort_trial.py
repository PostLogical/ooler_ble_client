"""Test whether THERMAL_EFFORT == 0 identifies a deep clean.

CLEAN_CHAR is asserted both by a user-started deep clean and by the device's
own hourly UV cycle. Snapshot evidence says THERMAL_EFFORT separates them: 0
during a deep clean, non-zero during the UV cycle, and never 0 on a powered
unit that is not cleaning (~40 observations). That last claim is the weak one,
because every one of those snapshots was taken on a unit actively working. A
unit sitting at its setpoint with nothing to do might read 0 for innocent
reasons, which would sink the whole idea.

This drives the device through the states that could produce an innocent 0 and
records what it reads:

  cool       setpoint below water temp   -- expect effort > 0
  idle       setpoint at water temp      -- THE TEST: a 0 here kills it
  heat       setpoint above water temp   -- a 0 here kills it too, since a
                                             heating unit would look like a
                                             deep clean
  deepclean  CLEAN=1, then cancelled     -- expect effort == 0

Every sample and every command is written to CSV, so the run is auditable
afterwards and nothing needs doing by hand.

DEVICE_LOGS is deliberately never read: reading drains it, and the vendor app
reads the same buffer.

The deep clean is ALWAYS cancelled, and the dwell is capped well below the
~45 minutes a full one takes. A deep clean run to completion arms the setpoint
override bug; one that is cancelled does not.

Usage:
    python diagnostics/thermal_effort_trial.py <address> [--out trial.csv]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path

from bleak import BleakClient

from ooler_ble_client.const import (
    ACTUALTEMP_CHAR,
    CLEAN_CHAR,
    CLEAN_TEMP_F,
    POWER_CHAR,
    PUMP_LEVEL_CHAR,
    RUNTIME_CHAR,
    SETTEMP_CHAR,
    THERMAL_EFFORT_CHAR,
    UV_RUNTIME_CHAR,
)

# Never a full clean: this only ever runs long enough to sample it.
MAX_CLEAN_SECONDS = 300.0

FIELDS = [
    "timestamp", "phase", "kind", "note",
    "power", "clean", "set_temp_f", "actual_f",
    "thermal_effort", "pump_level", "uv_runtime", "runtime",
]


class Trial:
    """Owns the connection so a dropped link does not end the run.

    The device accepts one connection at a time and long holds are fragile --
    two runs died around the 16 minute mark. A UV cycle can be an hour of
    runtime away, so reads and writes reconnect and retry rather than raising,
    and the caller keeps sampling across the gap.
    """

    def __init__(self, address: str, writer: csv.DictWriter) -> None:
        self._address = address
        self._writer = writer
        self._client: BleakClient | None = None
        self.phase = "setup"
        self.reconnects = 0
        self.lost_samples = 0

    async def connect(self, attempts: int = 8, backoff: float = 5.0) -> None:
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                client = BleakClient(self._address, timeout=20.0)
                await client.connect()
                self._client = client
                return
            except Exception as err:  # noqa: BLE001 - retrying is the point
                last = err
                if attempt + 1 < attempts:
                    await asyncio.sleep(backoff)
        raise RuntimeError(f"could not connect after {attempts} attempts: {last}")

    async def close(self) -> None:
        if self._client is not None and self._client.is_connected:
            await self._client.disconnect()
        self._client = None

    async def _live(self) -> BleakClient:
        if self._client is None or not self._client.is_connected:
            self.reconnects += 1
            print(f"  ... link lost, reconnecting (#{self.reconnects})", flush=True)
            await self.connect()
        assert self._client is not None
        return self._client

    async def _u8(self, uuid: str) -> int:
        for attempt in (1, 2):
            try:
                client = await self._live()
                return int.from_bytes(await client.read_gatt_char(uuid), "little")
            except Exception:  # noqa: BLE001 - one reconnect, then give up
                if attempt == 2:
                    raise
                self._client = None
        raise AssertionError("unreachable")

    async def sample(self, kind: str = "sample", note: str = "") -> dict[str, int]:
        row = {
            "power": await self._u8(POWER_CHAR),
            "clean": await self._u8(CLEAN_CHAR),
            "set_temp_f": await self._u8(SETTEMP_CHAR),
            "actual_f": await self._u8(ACTUALTEMP_CHAR),
            "thermal_effort": await self._u8(THERMAL_EFFORT_CHAR),
            "pump_level": await self._u8(PUMP_LEVEL_CHAR),
            "uv_runtime": await self._u8(UV_RUNTIME_CHAR),
            "runtime": await self._u8(RUNTIME_CHAR),
        }
        self._writer.writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "phase": self.phase, "kind": kind, "note": note, **row,
        })
        print(f"  [{self.phase:<9}] power={row['power']} clean={row['clean']} "
              f"set={row['set_temp_f']} actual={row['actual_f']} "
              f"effort={row['thermal_effort']} pump={row['pump_level']}"
              f"{'  ' + note if note else ''}", flush=True)
        return row

    async def command(self, uuid: str, value: int, note: str) -> None:
        """Write to the device, recording it in the same log as the samples."""
        print(f"  [{self.phase:<9}] COMMAND {note}", flush=True)
        for attempt in (1, 2):
            try:
                client = await self._live()
                await client.write_gatt_char(
                    uuid, value.to_bytes(1, "little"), True
                )
                break
            except Exception:  # noqa: BLE001 - one reconnect, then give up
                if attempt == 2:
                    raise
                self._client = None
        await self.sample(kind="command", note=note)

    async def sample_or_skip(self) -> dict[str, int] | None:
        """Sample, tolerating a drop we could not immediately recover.

        A long soak must outlive a gap it cannot close on the spot; losing a
        sample is preferable to ending the run and stranding the device.
        """
        try:
            return await self.sample()
        except Exception as err:  # noqa: BLE001 - keep soaking
            self.lost_samples += 1
            print(f"  ... sample lost ({err}); continuing", flush=True)
            self._client = None
            return None

    async def watch_phase(
        self, phase: str, seconds: float, interval: float
    ) -> list[int]:
        """Sample one phase for a while; return the effort readings."""
        self.phase = phase
        efforts: list[int] = []
        elapsed = 0.0
        while elapsed < seconds:
            efforts.append((await self.sample())["thermal_effort"])
            await asyncio.sleep(interval)
            elapsed += interval
        return efforts


@asynccontextmanager
async def _session(
    address: str, writer: csv.DictWriter
) -> AsyncIterator[Trial]:
    """A Trial with the link opened, and always closed on the way out."""
    t = Trial(address, writer)
    await t.connect()
    print("Connected\n", flush=True)
    try:
        yield t
    finally:
        if t.reconnects or t.lost_samples:
            print(f"  ({t.reconnects} reconnects, {t.lost_samples} lost samples)",
                  flush=True)
        with suppress(Exception):
            await t.close()


async def run(address: str, out: Path, interval: float, clean_seconds: float) -> None:
    if clean_seconds > MAX_CLEAN_SECONDS:
        raise SystemExit(
            f"--clean-seconds {clean_seconds} exceeds the {MAX_CLEAN_SECONDS}s cap; "
            "a deep clean left to finish arms the setpoint override bug"
        )

    with out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()

        print(f"Connecting to {address} ...", flush=True)
        async with _session(address, writer) as t:

            found = await t.sample(kind="baseline", note="as found")
            original_power, original_setpoint = found["power"], found["set_temp_f"]
            results: dict[str, list[int]] = {}

            try:
                if not original_power:
                    await t.command(POWER_CHAR, 1, "power on")
                    await asyncio.sleep(60)  # let the pump and sensors settle

                water = (await t.sample(note="pre-test water temp"))["actual_f"]

                await t.command(SETTEMP_CHAR, water - 6, "setpoint below water")
                results["cool"] = await t.watch_phase("cool", 180, interval)

                water = (await t.sample(note="re-read before idle"))["actual_f"]
                await t.command(SETTEMP_CHAR, water, "setpoint AT water temp")
                results["idle"] = await t.watch_phase("idle", 360, interval)

                await t.command(SETTEMP_CHAR, water + 8, "setpoint above water")
                results["heat"] = await t.watch_phase("heat", 240, interval)

                await t.command(CLEAN_CHAR, 1, "START deep clean")
                results["deepclean"] = await t.watch_phase(
                    "deepclean", clean_seconds, interval
                )
            finally:
                # Always: stop any clean we started, then put the device back
                # as it was found. A clean left running would finish on its own
                # and arm the override bug -- so that write is retried, and
                # confirmed by reading the bit back rather than assumed.
                t.phase = "restore"
                for attempt in range(4):
                    try:
                        await t.command(CLEAN_CHAR, 0, f"CANCEL deep clean (try {attempt + 1})")
                        if (await t.sample(note="confirm clean stopped"))["clean"] == 0:
                            break
                    except Exception as err:  # noqa: BLE001 - must not give up here
                        print(f"  !! cancel failed: {err}", flush=True)
                        await asyncio.sleep(2)
                else:
                    print(
                        "\n!! COULD NOT CONFIRM THE CLEAN STOPPED. A clean left to "
                        "finish arms the setpoint-override bug. Cancel it now from "
                        "the Ooler app or Home Assistant.",
                        flush=True,
                    )
                try:
                    await t.command(SETTEMP_CHAR, original_setpoint, "restore setpoint")
                    if not original_power:
                        await t.command(POWER_CHAR, 0, "restore power off")
                except Exception as err:  # noqa: BLE001 - report, do not mask
                    print(f"  !! restore failed, device left as-is: {err}", flush=True)

    print("\n=== result ===", flush=True)
    for phase, efforts in results.items():
        if not efforts:
            continue
        zeros = sum(1 for e in efforts if e == 0)
        print(f"  {phase:<10} n={len(efforts):<4} min={min(efforts):<4} "
              f"max={max(efforts):<4} zeros={zeros}", flush=True)

    idle_zeros = sum(1 for e in results.get("idle", []) if e == 0)
    heat_zeros = sum(1 for e in results.get("heat", []) if e == 0)
    clean_nonzero = sum(1 for e in results.get("deepclean", []) if e != 0)
    print()
    if idle_zeros or heat_zeros:
        print("VERDICT: THERMAL_EFFORT reads 0 without a deep clean. "
              "It cannot identify one.", flush=True)
    elif clean_nonzero:
        print("VERDICT: THERMAL_EFFORT was non-zero during a deep clean. "
              "It cannot identify one.", flush=True)
    else:
        print("VERDICT: 0 only during the deep clean, never otherwise.", flush=True)
    print(f"Full log: {out}", flush=True)


async def soak(
    address: str, out: Path, interval: float, minutes: float, _unused: float
) -> None:
    """Hold the setpoint at CLEAN_TEMP_F and wait for a UV cycle. PASSIVE.

    Starts no clean and cancels nothing -- writing CLEAN=0 here would abort
    the device's own UV cycle, which is the thing being measured. The only
    writes are power-on (if needed) and the setpoint.

    Setpoint is pinned at CLEAN_TEMP_F because that is the only case needing
    a discriminator: a UV cycle at 75 is what today's rule calls a deep clean.

    Answers:
      - Does UV_RUNTIME advance continuously through a UV cycle, as it does
        through the first two minutes of a deep clean, and how many lamp
        seconds does one cycle burn?
      - Does THERMAL_EFFORT stay CORRELATED with the temperature delta during
        a UV cycle? A deep clean departed from the normal curve by an order of
        magnitude at act-set +1 and +2. If a UV cycle stays on the curve, the
        differential is the discriminator.

    Then fills the gap in the normal-control curve at act-set +1 and +2 --
    exactly where the deep clean departed, and where run 1 happened to take
    no samples, so that comparison currently rests on interpolation.
    """
    with out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()

        print(f"Connecting to {address} ...", flush=True)
        async with _session(address, writer) as t:

            found = await t.sample(kind="baseline", note="as found")
            original_power, original_setpoint = found["power"], found["set_temp_f"]
            soak_efforts: list[int] = []
            uv_efforts: list[int] = []
            uv_lamp_seconds = 0

            try:
                if not original_power:
                    await t.command(POWER_CHAR, 1, "power on")
                    await asyncio.sleep(60)
                await t.command(SETTEMP_CHAR, CLEAN_TEMP_F, "setpoint to CLEAN_TEMP_F")

                t.phase = "soak"
                was_clean = False
                captured = False
                uv_start_lamp = 0
                elapsed = 0.0
                while elapsed < minutes * 60 and not captured:
                    row = await t.sample_or_skip()
                    if row is None:
                        # Lost the link on this tick. Keep the cycle-edge state
                        # as it was rather than guessing, and try again.
                        await asyncio.sleep(interval)
                        elapsed += interval
                        continue
                    is_clean = bool(row["clean"])
                    if is_clean and not was_clean:
                        print("  *** CLEAN high -- a cycle we did not start ***",
                              flush=True)
                        uv_start_lamp = row["uv_runtime"]
                        await t.sample(kind="event", note="UV cycle started")
                    elif was_clean and not is_clean:
                        uv_lamp_seconds = row["uv_runtime"] - uv_start_lamp
                        print(f"  *** CLEAN low -- cycle over, {uv_lamp_seconds} "
                              f"lamp seconds ***", flush=True)
                        await t.sample(kind="event", note="UV cycle ended")
                        captured = True
                    was_clean = is_clean
                    (uv_efforts if is_clean else soak_efforts).append(
                        row["thermal_effort"]
                    )
                    await asyncio.sleep(interval)
                    elapsed += interval

                if not captured:
                    print("  (no UV cycle seen within the window)", flush=True)

                # Fill the normal-control curve where the deep clean departed
                # from it. Run 1 has no samples at act-set +1 or +2, so that
                # comparison currently rests on interpolation.
                water = (await t.sample(note="water before curve fill"))["actual_f"]
                for offset in (1, 2):
                    await t.command(
                        SETTEMP_CHAR, water - offset, f"setpoint {offset}F below water"
                    )
                    await t.watch_phase(f"curve+{offset}", 240, interval)
            finally:
                # No clean of ours to stop, and CLEAN=0 here would abort the
                # device's own cycle -- the thing being measured. Setpoint and
                # power only.
                t.phase = "restore"
                try:
                    await t.command(SETTEMP_CHAR, original_setpoint, "restore setpoint")
                    if not original_power:
                        await t.command(POWER_CHAR, 0, "restore power off")
                except Exception as err:  # noqa: BLE001 - report, do not mask
                    print(f"  !! restore failed, device left as-is: {err}", flush=True)

    print("\n=== result ===", flush=True)
    print(f"  UV cycle lamp seconds: "
          f"{uv_lamp_seconds if uv_lamp_seconds else 'no cycle captured'}", flush=True)
    for label, efforts in (
        ("soak (no clean)", soak_efforts),
        ("uv cycle", uv_efforts),
    ):
        if efforts:
            zeros = sum(1 for e in efforts if e == 0)
            print(f"  {label:<16} n={len(efforts):<4} min={min(efforts):<4} "
                  f"max={max(efforts):<4} zeros={zeros}", flush=True)
        else:
            print(f"  {label:<16} not observed", flush=True)
    print(f"\nFull log: {out}", flush=True)


async def predict(
    address: str, out: Path, phase: int, cycles: int, slow: float, fast: float
) -> None:
    """Test whether RUNTIME predicts when the UV cycle fires.

    The recorder history says cycles come exactly 60.0 minutes apart while the
    unit runs -- ~100 consecutive gaps over 12 nights -- and RUNTIME advances
    1/s while powered. Together those predict that RUNTIME mod 3600 at every
    cycle start is the same number. One measured cycle put it at 2251.

    So this is a pre-registered prediction, not a fishing trip: sample slowly
    until RUNTIME approaches the next value congruent to `phase`, sample fast
    through the window, and record the RUNTIME at which CLEAN actually went
    high. The error between predicted and actual is the result.

    If it holds, RUNTIME alone says when a UV cycle is due, and an assertion
    outside the window is a deep clean -- no setpoint, effort or temperature
    involved. The counter is not reliably monotonic across resets, so a
    consumer would re-learn the phase whenever RUNTIME goes backwards.

    Starts no clean and changes no setting beyond powering the unit on.
    """
    with out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        print(f"Connecting to {address} ...", flush=True)
        async with _session(address, writer) as t:
            found = await t.sample(kind="baseline", note="as found")
            original_power = found["power"]
            results: list[tuple[int, int]] = []
            try:
                if not original_power:
                    await t.command(POWER_CHAR, 1, "power on")
                    await asyncio.sleep(30)

                for cycle in range(cycles):
                    now = (await t.sample(note="planning"))["runtime"]
                    target = now - (now % 3600) + phase
                    while target <= now:
                        target += 3600
                    print(f"  cycle {cycle + 1}: predicting RUNTIME {target} "
                          f"({(target - now) / 60:.0f} min of runtime away)",
                          flush=True)
                    t.phase = f"predict{cycle + 1}"

                    was_clean = False
                    while True:
                        row = await t.sample_or_skip()
                        if row is None:
                            await asyncio.sleep(fast)
                            continue
                        rt = row["runtime"]
                        if bool(row["clean"]) and not was_clean:
                            error = rt - target
                            print(f"  *** CLEAN high at RUNTIME {rt} -- predicted "
                                  f"{target}, error {error:+d}s ***", flush=True)
                            await t.sample(kind="event", note=f"cycle start, "
                                           f"predicted {target}, error {error:+d}")
                            results.append((target, rt))
                            break
                        was_clean = bool(row["clean"])
                        if rt > target + 900:
                            print(f"  !! no cycle by RUNTIME {rt}, 15 min past "
                                  f"prediction -- model wrong", flush=True)
                            results.append((target, -1))
                            break
                        await asyncio.sleep(fast if rt > target - 300 else slow)
            finally:
                t.phase = "restore"
                try:
                    if not original_power:
                        await t.command(POWER_CHAR, 0, "restore power off")
                except Exception as err:  # noqa: BLE001 - report, do not mask
                    print(f"  !! restore failed, device left as-is: {err}", flush=True)

    print("\n=== result ===", flush=True)
    for target, actual in results:
        if actual < 0:
            print(f"  predicted {target}: NO CYCLE (model wrong)", flush=True)
        else:
            print(f"  predicted {target}  actual {actual}  "
                  f"error {actual - target:+d}s", flush=True)


async def restore(
    address: str, out: Path, setpoint: int | None, power_on: bool | None
) -> None:
    """Put the device into a stated power/setpoint state, and log it.

    A run whose link drops leaves the device however the test left it. This
    exists so putting it back is a recorded command like every other, rather
    than something done by hand outside the protocol.

    Never touches CLEAN: a clean this did not start is not its to cancel.
    """
    with out.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if out.stat().st_size == 0:
            writer.writeheader()
        print(f"Connecting to {address} ...", flush=True)
        async with _session(address, writer) as t:
            t.phase = "restore"
            await t.sample(kind="baseline", note="as found before restore")
            # Setpoint first: the device drops writes while off, so a restore
            # that powers down first would silently discard the temperature.
            if setpoint is not None:
                await t.command(SETTEMP_CHAR, setpoint, f"restore setpoint {setpoint}")
            if power_on is not None:
                await t.command(
                    POWER_CHAR, int(power_on),
                    f"restore power {'on' if power_on else 'off'}",
                )
            await t.sample(kind="baseline", note="final state")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", help="Device address")
    parser.add_argument(
        "--mode", choices=("sweep", "soak", "restore", "predict"), default="sweep"
    )
    parser.add_argument(
        "--phase", type=int, default=2251,
        help="predict mode: RUNTIME mod 3600 at which UV cycles are expected",
    )
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--restore-setpoint", type=int)
    parser.add_argument("--restore-power", choices=("on", "off"))
    parser.add_argument("--out", type=Path, default=Path("thermal_effort_trial.csv"))
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--clean-seconds", type=float, default=120.0)
    parser.add_argument(
        "--soak-minutes", type=float, default=90.0,
        help="soak mode: how long to hold at CLEAN_TEMP_F waiting for a UV cycle",
    )
    args = parser.parse_args()
    if args.mode == "predict":
        asyncio.run(predict(
            args.address, args.out, args.phase, args.cycles, 60.0, 5.0,
        ))
    elif args.mode == "restore":
        asyncio.run(restore(
            args.address, args.out, args.restore_setpoint,
            None if args.restore_power is None else args.restore_power == "on",
        ))
    elif args.mode == "soak":
        asyncio.run(soak(
            args.address, args.out, args.interval,
            args.soak_minutes, args.clean_seconds,
        ))
    else:
        asyncio.run(run(args.address, args.out, args.interval, args.clean_seconds))


if __name__ == "__main__":
    main()
