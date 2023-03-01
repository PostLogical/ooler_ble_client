from __future__ import annotations

import asyncio
import logging

from bleak import BleakScanner
from bleak.assigned_numbers import AdvertisementDataType
from bleak.backends.bluezdbus.advertisement_monitor import OrPattern
from bleak.backends.bluezdbus.scanner import BlueZScannerArgs
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

logger = logging.getLogger(__name__)


#def simple_callback(device: BLEDevice, advertisement_data: AdvertisementData):
#    logger.info(f"{device.address} RSSI: {device.rssi}, {advertisement_data}")

def device_in_pairing_mode(
    device: BLEDevice,
    advertisement_data: AdvertisementData,
):
    logger.error("Made it to device_in_pairing_mode callback")
    logger.error("Device address is: %s", device.address)
    if device.address == "84:71:27:57:9F:D7":
        return True


async def main():
    scanner = BleakScanner(
        device_in_pairing_mode,
        # select passive scanning
        scanning_mode="passive",
        # at least one or_pattern is required, otherwise BlueZ will (silently) reject the advertisement monitor
        # (bleak will check for the condition and raise an exception since BlueZ is silent on the matter)
        bluez=BlueZScannerArgs(
            or_patterns=[
                OrPattern(0, AdvertisementDataType.FLAGS, b"\x02"),
            ]
        ),
    )

    while True:
        print("(re)starting scanner")
        await scanner.start()
        await asyncio.sleep(15.0)
        await scanner.stop()


if __name__ == "__main__":
    # logging.basicConfig(
    #     level=logging.INFO,
    #     format="%(asctime)-15s %(name)-8s %(levelname)s: %(message)s",
    # )
    # service_uuids = sys.argv[1:]
    asyncio.run(main())
