from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class OolerBLEState:
    power: int = None
    mode: int = None
    set_temperature: int = None
    actual_temperature: int = None