"""Constants for the ooler_ble_client library."""
import logging

_LOGGER = logging.getLogger(__package__)

DEFAULT_ATTEMPTS = 3
DISCONNECT_DELAY = 120

MODE_INT_TO_MODE_STATE = ["Silent", "Regular", "Boost"]

POWER_CHARACTERISTIC = "7a2623ff-bd92-4c13-be9f-7023aa4ecb85"
MODE_CHARACTERISTIC = "cafe2421-d04c-458f-b1c0-253c6c97e8e8"
SETTEMP_CHARACTERISTIC = "6aa46711-a29d-4f8a-88e2-044ca1fd03ff"
ACTUALTEMP_CHARACTERISTIC = "e8ebded3-9dca-45c2-a2d8-ceffb901474d"