"""
app/modes.py — Runtime mode definitions.
"""
from __future__ import annotations

from enum import Enum


class Mode(str, Enum):
    MEASUREMENT = "measurement"
    PAPER = "paper"

    @classmethod
    def from_str(cls, s: str) -> "Mode":
        try:
            return cls(s.lower())
        except ValueError:
            raise ValueError(f"Unknown mode '{s}'. Valid: {[m.value for m in cls]}")
