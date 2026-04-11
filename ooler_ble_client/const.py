"""Constants for the ooler_ble_client library.

Characteristic UUIDs verified against firmware 15.20 on Ooler model 999.
The device silently drops writes to all characteristics when powered off.

Temperature boundaries:
  SET_TEMP always stores Fahrenheit regardless of DISPLAY_TEMP_UNIT.
  ACTUAL_TEMP reports in the current display unit (F or C).
  Valid SET_TEMP values: 45 (LO), 55-115, 120 (HI).
  Values 46-54 snap to 45; values 116-119 snap to 120.
"""

from __future__ import annotations

import logging

from .models import OolerMode

_LOGGER = logging.getLogger(__package__)

MODE_INT_TO_MODE_STATE: list[OolerMode] = ["Silent", "Regular", "Boost"]

# Temperature boundaries (Fahrenheit). LO and HI are special sentinel values
# that tell the device to cool/heat as aggressively as possible.
TEMP_LO_F = 45
TEMP_MIN_F = 55
TEMP_MAX_F = 115
TEMP_HI_F = 120

# -- Device info service (0000180a-0000-1000-8000-00805f9b34fb) --
# Standard BLE Device Information Service.
MANUFACTURER_NAME_CHAR = "00002a29-0000-1000-8000-00805f9b34fb"  # read; "Kryo, Inc."
FIRMWARE_REVISION_CHAR = "00002a26-0000-1000-8000-00805f9b34fb"  # read; e.g. "15.20"
MODEL_NUMBER_CHAR = "00002a24-0000-1000-8000-00805f9b34fb"  # read; e.g. "999"
HARDWARE_ID_CHAR = "a2b8f087-c75f-4646-a97a-22db6b748c94"  # read; 6 bytes, unique per device

# -- Main control service (5c293993-d039-4225-92f6-31fa62101e96) --
# All verified read/write/notify unless noted.
POWER_CHAR = "7a2623ff-bd92-4c13-be9f-7023aa4ecb85"
MODE_CHAR = "cafe2421-d04c-458f-b1c0-253c6c97e8e8"
SETTEMP_CHAR = "6aa46711-a29d-4f8a-88e2-044ca1fd03ff"
ACTUALTEMP_CHAR = "e8ebded3-9dca-45c2-a2d8-ceffb901474d"  # read/notify only
WATER_LEVEL_CHAR = "8db5b9db-dbf6-47e6-a9dd-0612a1349a5b"  # read/notify; reports 1, 50, or 100
CLEAN_CHAR = "e9bf509a-b1c5-4243-9514-352ad2d851f6"  # clean forces SET_TEMP=75 while active
DISPLAY_TEMPERATURE_UNIT_CHAR = "2c988613-fe15-4067-85bc-8e59d5e0b1e3"  # 0=F, 1=C
THERMAL_EFFORT_CHAR = "fdff37ff-901d-40c6-b7e0-dd5797bd2989"  # read/notify; 2 bytes; 0 when off
PUMP_LEVEL_CHAR = "5a914d86-9b5e-4a35-ad3d-3e5936d485b2"  # read/notify; 2 bytes; 0 when off
POWER_RAIL_CHAR = "acab07ec-fc95-451d-88e5-4565a364a806"  # read/notify; constant 23 observed
RELATIVE_HUMIDITY_CHAR = "654b8162-7090-4084-8d94-4eb33e917e9c"  # read/notify; percentage
AMBIENT_TEMPERATURE_F_CHAR = "7c0ea228-2616-4765-a726-beb5f4a0fa71"  # read/notify; always F
# Unknown purpose; read/notify. Never changed across all testing (always 00 00).
UNKNOWN_F30D_CHAR = "f30d875a-7297-43ac-9f5b-1d7eed4446eb"
# Unknown purpose; read/notify. Was 0xFF on a device exhibiting an 80°F revert
# bug, 0xFE on the other. Changed to 0x00 during testing; revert may have stopped.
UNKNOWN_9234_CHAR = "923445f2-9438-4d81-98c9-904b69b94eca"
# Unknown purpose; read/notify. Always 0x00 across all testing.
UNKNOWN_AF8D_CHAR = "af8d892b-693d-495d-ac95-eb849a5ac40c"

# -- Write-only command service (1d14d6ee-fd63-4fa1-bfa4-8f47b42119f0) --
WRITE_COMMAND_CHAR = "f7bf3564-fb6d-4e53-88a4-5e37e0326063"  # write only; purpose unknown

# -- Command/response service (4bf69dcd-412d-494c-9348-f2f364e5c6ce) --
# Likely a command/response pair: write a command, receive a response via indicate.
CMD_WRITE_CHAR = "abf9e9a9-058c-46d3-9570-1782d0fd1d5d"  # write only
CMD_RESPONSE_CHAR = "8b56f100-bed3-4858-89d0-eef0da6168fd"  # indicate only

# -- Current time service (00001805-0000-1000-8000-00805f9b34fb) --
# Standard BLE Current Time Service. The device has an internal clock.
CURRENT_TIME_CHAR = "00002a2b-0000-1000-8000-00805f9b34fb"  # read/write/notify
LOCAL_TIME_INFO_CHAR = "00002a0f-0000-1000-8000-00805f9b34fb"  # read/write; observed 0xEC04 (1260)
REFERENCE_TIME_INFO_CHAR = "00002a14-0000-1000-8000-00805f9b34fb"  # read; observed 0x04FFFFFF

# -- Diagnostics service (28dfbeff-61e0-4aa2-9eea-ede0b86f3f65) --
LIFETIME_CHAR = "5d30781f-1d06-4790-bbb8-5e1d7da96383"  # read; 4-byte LE counter
RUNTIME_CHAR = "1a5c6dae-34de-4265-9fa6-0a59f7f683ee"  # read; 4-byte LE counter
UV_RUNTIME_CHAR = "0ab6ff00-8d1b-475e-bcfa-ed3467f1f890"  # read; 4-byte LE counter
DEVICE_LOGS_CHAR = "e6a505a4-9f0b-4755-b234-13243240da23"  # read; rolling event log
SUB_FIRMWARE_CHAR = "9a5f99ef-4370-4e87-a073-7769cd8dd35c"  # read; e.g. "1.58"
# Unknown purpose; read/notify. Always 0x00 across all testing.
UNKNOWN_51B9_CHAR = "51b91d16-ff96-459d-aa02-0895044be049"
# Unknown purpose; read/write. Constant 0x0000003C (60). Possibly a timeout in seconds.
UNKNOWN_1A7F_CHAR = "1a7f1561-ae85-43a6-956f-a90ede82f623"
# Unknown purpose; read/write. Constant 0x00000005 (5). Matches pumpH/pumpC in config JSON.
UNKNOWN_8AB5_CHAR = "8ab57bec-d4d2-4d5a-bd55-2f89f5949823"

# -- Device config service (dc5e0473-d2ec-4f23-9b61-cd7bae046f76) --
SERIAL_NUMBER_CHAR = "136e24c6-c486-4a74-bb0a-d18b985970a6"  # read; zero-padded string
DEVICE_CONFIG_JSON_CHAR = "a397436e-0927-4029-8ea4-7368c2f08d09"  # read; calibration JSON
MAX_TEMP_CHAR = "adffd248-9588-427e-a226-aeb96c340be7"  # read; matches config JSON "hi" (120)
MIN_TEMP_CHAR = "cfcea17c-f46d-491f-94a3-aae40daac395"  # read; matches config JSON "lo" (51-53)
DELTA_HEAT_CHAR = "3a59cb22-9332-435d-b3b4-74e63477958c"  # read; matches config JSON "deltaH"
DELTA_COOL_CHAR = "be83c9a6-462d-43b7-9528-28a87865e565"  # read; matches config JSON "deltaC"
# Write-only; purpose unknown. On the same service as serial number and config.
CONFIG_WRITE_CHAR = "87c9fb8d-f243-4412-98cf-cc0c97b3d106"

# -- Schedule service (b430cd72-3a7f-4720-86fd-66ae8f6f3493) --
# One active schedule at a time. Disabled schedules are app-side only.
# Schedule names are app-side only — not stored on the device.
# Times are uint16 LE minute-of-week values (Mon 00:00=0, Sun 23:59=10079).
# Temps are 1:1 with times: 0x00=OFF, 1-120=°F, 0xFE=warm wake marker, 0xFF=unused.
# IMPORTANT: The device byte-swaps uint16 values on GATT write — pre-swap
# times and header bytes so the device stores the intended LE values.
# See sleep_schedule.py for full encoding/decoding logic.
SCHEDULE_HEADER_CHAR = "8cb4ec90-cd94-4f69-b963-5473fbd94ec8"  # read/write; uint16 LE sequence counter
SCHEDULE_TIMES_CHAR = "8cb4ec90-cd94-4f69-b963-5473fbd94ea9"  # read/write; 70 × uint16 LE minute-of-week
SCHEDULE_TEMPS_CHAR = "fa242bc0-bf85-41f7-8dbb-53ba2e8b0895"  # read/write; 70 × uint8 temperature
SCHEDULE_META_CHAR = "fa242bc0-bf85-41f7-8dbb-53ba2e8b08a3"  # read-only; firmware-internal state flag

# -- OTA/unknown service (4d44eb61-87dd-402c-ad4c-41928e08c8eb) --
# Standard BLE Central Address Resolution characteristic, repurposed or vestigial.
UNKNOWN_2AAA_CHAR = "00002aaa-0000-1000-8000-00805f9b34fb"  # read/write/notify; always 0x0000