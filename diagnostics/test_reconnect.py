"""Exercise the reconnect paths without hardware."""
import asyncio, sys, types
from pathlib import Path
sys.path.insert(0, "/Users/robbyg/Documents/Coding/GitHub/ooler_ble_client")
sys.path.insert(0, "/Users/robbyg/Documents/Coding/GitHub/ooler_ble_client/diagnostics")
import value_domain_sweep as v
from bleak.exc import BleakError

v._RECONNECT_BACKOFF_SECONDS = 0.01

class FakeClient:
    def __init__(self, fail_reads=0, connected=True):
        self.is_connected = connected
        self.fail_reads = fail_reads
        self.reads = 0
        self.writes = []
    async def read_gatt_char(self, uuid):
        self.reads += 1
        if self.fail_reads > 0:
            self.fail_reads -= 1
            self.is_connected = False
            raise BleakError("disconnected")
        return bytearray(b"\x64")
    async def write_gatt_char(self, uuid, data, response=True):
        if not self.is_connected:
            raise BleakError("disconnected")
        self.writes.append((uuid, data))
    async def disconnect(self):
        self.is_connected = False

def make(out, connect_impl):
    dev = types.SimpleNamespace(name="OOLER-TEST", address="AA")
    s = v.Sweep(dev, out)
    s.connect = connect_impl.__get__(s)
    return s

async def main():
    out = Path("/tmp/_recon_test.jsonl")
    results = []

    # 1. A read that drops the link recovers on reconnect.
    async def good_connect(self): self._client = FakeClient()
    s = make(out, good_connect)
    s._client = FakeClient(fail_reads=1)
    r = await s.read("uuid-a")
    results.append(("read recovers after a drop", r.get("int") == 100 and s.reconnects == 1, r))

    # 2. A write that drops the link recovers too.
    s2 = make(out, good_connect)
    s2._client = FakeClient(); s2._client.is_connected = False
    ok = await s2.write("uuid-b", 70)
    results.append(("write recovers after a drop", ok is True and s2.reconnects == 1, ok))

    # 3. When reconnect never succeeds, read reports an error and gives up.
    async def bad_connect(self): raise BleakError("no route")
    s3 = make(out, bad_connect)
    s3._client = FakeClient(fail_reads=1)
    r3 = await s3.read("uuid-c")
    results.append(("gives up cleanly when unreachable",
                    "error" in r3 and s3.reconnects == 0, r3))

    # 4. Gap accounting is recorded for the summary.
    results.append(("gap time recorded", s.gap_seconds >= 0 and s.reconnects == 1,
                    f"{s.gap_seconds:.3f}s"))

    ok_all = True
    for name, passed, detail in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}   ({detail})")
        ok_all &= passed
    print("ALL PASS" if ok_all else "FAILURES")
    return 0 if ok_all else 1

sys.exit(asyncio.run(main()))
