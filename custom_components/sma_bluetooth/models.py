"""Data models for SMA Bluetooth."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SMAInverter:
    """One inverter returned by the native SMA Bluetooth protocol."""

    serial: str
    susy_id: str | None = None
    name: str | None = None
    model: str | None = None
    software_version: str | None = None
    bluetooth_address: str | None = None
    network_role: str | None = None
    record_timestamp: int | None = None
    record_received_at: float | None = None
    values: dict[str, Any] = field(default_factory=dict)

    @property
    def record_clock_difference(self) -> float | None:
        """Return the raw SMA record clock difference at receipt time."""
        if self.record_timestamp is None or self.record_received_at is None:
            return None
        return self.record_timestamp - self.record_received_at
