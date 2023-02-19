from __future__ import annotations

from dataclasses import dataclass

@dataclass
class OolerBLEState:
    power: bool | None = None
    mode: str | None = None
    set_temperature: int | None = None
    actual_temperature: int | None = None
    connected: bool = False