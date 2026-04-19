"""Clean write test: write a known schedule with response=True, read back, verify."""

from __future__ import annotations

import asyncio
import struct

from bleak import BleakScanner, BleakClient

TIMES_CHAR = "8cb4ec90-cd94-4f69-b963-5473fbd94ea9"
HEADER_CHAR = "8cb4ec90-cd94-4f69-b963-5473fbd94ec8"
TEMPS_CHAR = "fa242bc0-bf85-41f7-8dbb-53ba2e8b0895"


async def main() -> None:
    print("Scanning...")
    devices = await BleakScanner.discover(timeout=10.0)
    oolers = [d for d in devices if d.name and "ooler" in d.name.lower()]
    if not oolers:
        print("No Oolers found")
        return
    d = oolers[0]
    print(f"Connecting to {d.name}...")

    async with BleakClient(d, timeout=30.0) as client:
        print(f"Connected. MTU: {client.mtu_size}")

        # Read baseline
        header = await client.read_gatt_char(HEADER_CHAR)
        times_orig = await client.read_gatt_char(TIMES_CHAR)
        temps_orig = await client.read_gatt_char(TEMPS_CHAR)
        seq = int.from_bytes(header, "little")
        print(f"\nBaseline: seq={seq}")
        print(f"  Times[0:10]: {bytes(times_orig[:10]).hex(' ')}")
        print(f"  Temps[0:5]:  {bytes(temps_orig[:5]).hex(' ')}")

        # Build simple schedule: Mon 22:00=68F, Tue 06:00=OFF
        # Write as BIG-ENDIAN so device byte-swaps to LE for storage
        test_times = bytearray(140)
        struct.pack_into(">H", test_times, 0, 1320)  # Mon 22:00 = BE 0x0528
        struct.pack_into(">H", test_times, 2, 1800)  # Tue 06:00 = BE 0x0708
        test_temps = bytearray([0xFF] * 70)
        test_temps[0] = 68
        test_temps[1] = 0

        # Write ALL with response=True, order: times, temps, header
        print("\n=== Writing LE schedule (response=True) ===")
        new_seq = seq + 1
        print(f"  Times to write: {bytes(test_times[:4]).hex(' ')}")
        print(f"  Temps to write: {bytes(test_temps[:4]).hex(' ')}")
        print(f"  Header to write: seq={new_seq}")

        await client.write_gatt_char(TIMES_CHAR, bytes(test_times), response=True)
        await client.write_gatt_char(TEMPS_CHAR, bytes(test_temps), response=True)
        await client.write_gatt_char(HEADER_CHAR, new_seq.to_bytes(2, "big"), response=True)
        await asyncio.sleep(0.5)

        # Read back
        h_back = await client.read_gatt_char(HEADER_CHAR)
        t_back = await client.read_gatt_char(TIMES_CHAR)
        p_back = await client.read_gatt_char(TEMPS_CHAR)

        print(f"\n=== Readback ===")
        print(f"  Header: seq={int.from_bytes(h_back, 'little')} (expected {new_seq})")
        print(f"  Times[0:4]: {bytes(t_back[:4]).hex(' ')} (expected {bytes(test_times[:4]).hex(' ')})")
        print(f"  Temps[0:4]: {bytes(p_back[:4]).hex(' ')} (expected {bytes(test_temps[:4]).hex(' ')})")

        # After device byte-swaps our BE write, it should read back as LE
        expected_times_le = bytearray(140)
        struct.pack_into("<H", expected_times_le, 0, 1320)
        struct.pack_into("<H", expected_times_le, 2, 1800)

        times_ok = bytes(t_back) == bytes(expected_times_le)
        temps_ok = bytes(p_back) == bytes(test_temps)
        header_ok = int.from_bytes(h_back, "little") == new_seq
        print(f"\n  Times readback matches LE expectation: {times_ok}")
        print(f"  Temps match:  {temps_ok}")
        print(f"  Header match: {header_ok}")

        if not times_ok:
            v_read_le = struct.unpack_from("<H", t_back, 0)[0]
            print(f"\n  Times[0] read as LE={v_read_le} (expected 1320)")
            print(f"  Raw bytes: {bytes(t_back[:4]).hex(' ')}")

        # Restore original
        # Restore: write the original data back. Since original was LE on-device,
        # and device swaps on write, we need to write the byte-swapped version.
        print("\n=== Restoring (byte-swap original for write) ===")
        restore_seq = int.from_bytes(h_back, "little") + 1
        # Byte-swap each uint16 in times_orig for the write
        times_to_write = bytearray(times_orig)
        for i in range(0, len(times_to_write), 2):
            times_to_write[i], times_to_write[i + 1] = times_to_write[i + 1], times_to_write[i]
        await client.write_gatt_char(TIMES_CHAR, bytes(times_to_write), response=True)
        await client.write_gatt_char(TEMPS_CHAR, bytes(temps_orig), response=True)
        await client.write_gatt_char(HEADER_CHAR, restore_seq.to_bytes(2, "big"), response=True)
        await asyncio.sleep(0.3)
        t_rest = await client.read_gatt_char(TIMES_CHAR)
        print(f"  Times restored: {bytes(t_rest) == bytes(times_orig)}")

    print("\nDisconnected cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
