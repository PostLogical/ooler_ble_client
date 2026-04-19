"""Minimal test: write a known pattern to SCHEDULE_TIMES and read back raw bytes."""

from __future__ import annotations

import asyncio
import struct

from bleak import BleakClient, BleakScanner

from ooler_ble_client.const import (
    SCHEDULE_HEADER_CHAR,
    SCHEDULE_TIMES_CHAR,
    SCHEDULE_TEMPS_CHAR,
)


async def main() -> None:
    print("Scanning...")
    devices = await BleakScanner.discover(timeout=10.0)
    oolers = [d for d in devices if d.name and "ooler" in d.name.lower()]
    if not oolers:
        print("No Ooler found.")
        return

    d = oolers[0]
    print(f"Connecting to {d.name} ({d.address})...")

    async with BleakClient(d.address, timeout=20.0) as client:
        # Read current header
        header = await client.read_gatt_char(SCHEDULE_HEADER_CHAR)
        seq = int.from_bytes(header, "little")
        print(f"Current header seq: {seq} (raw: {header.hex()})")

        # Read current times
        times_before = await client.read_gatt_char(SCHEDULE_TIMES_CHAR)
        print(f"Times before (first 20 bytes): {times_before[:20].hex(' ')}")

        # Write a simple known pattern: just 2 time entries
        # Mon 22:00 = 1320 = 0x0528, Tue 06:00 = 1800 = 0x0708
        test_times = bytearray(140)
        struct.pack_into("<H", test_times, 0, 1320)  # 0x28 0x05
        struct.pack_into("<H", test_times, 2, 1800)  # 0x08 0x07

        test_temps = bytearray([0xFF] * 70)
        test_temps[0] = 68  # 68°F
        test_temps[1] = 0   # OFF

        new_seq = seq + 1

        print(f"\nWriting header seq={new_seq}...")
        await client.write_gatt_char(
            SCHEDULE_HEADER_CHAR, new_seq.to_bytes(2, "little"), response=True
        )

        print(f"Writing times (first 10 bytes): {bytes(test_times[:10]).hex(' ')}")
        await client.write_gatt_char(SCHEDULE_TIMES_CHAR, bytes(test_times), response=True)

        print(f"Writing temps (first 10 bytes): {bytes(test_temps[:10]).hex(' ')}")
        await client.write_gatt_char(SCHEDULE_TEMPS_CHAR, bytes(test_temps), response=True)

        # Now read back
        print("\nReading back...")
        header_back = await client.read_gatt_char(SCHEDULE_HEADER_CHAR)
        times_back = await client.read_gatt_char(SCHEDULE_TIMES_CHAR)
        temps_back = await client.read_gatt_char(SCHEDULE_TEMPS_CHAR)

        print(f"Header readback: {header_back.hex()} (seq={int.from_bytes(header_back, 'little')})")
        print(f"Times readback (first 20 bytes):  {bytes(times_back[:20]).hex(' ')}")
        print(f"Times expected  (first 20 bytes): {bytes(test_times[:20]).hex(' ')}")
        print(f"Temps readback (first 10 bytes):  {bytes(temps_back[:10]).hex(' ')}")
        print(f"Temps expected  (first 10 bytes): {bytes(test_temps[:10]).hex(' ')}")

        match = bytes(times_back) == bytes(test_times)
        print(f"\nTimes match: {match}")
        if not match:
            # Check if byte-swapped
            swapped = bytearray(140)
            for i in range(0, len(test_times), 2):
                swapped[i] = test_times[i+1]
                swapped[i+1] = test_times[i]
            if bytes(times_back) == bytes(swapped):
                print("Times are BYTE-SWAPPED (device stores big-endian)")
            else:
                print("Times differ in some other way")
                for i in range(0, 20, 2):
                    w = struct.unpack_from("<H", test_times, i)[0]
                    r = struct.unpack_from("<H", times_back, i)[0]
                    if w != r:
                        print(f"  offset {i}: wrote {test_times[i]:02x} {test_times[i+1]:02x} = {w}, read {times_back[i]:02x} {times_back[i+1]:02x} = {r}")

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
