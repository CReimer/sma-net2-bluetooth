# SPDX-License-Identifier: Apache-2.0
"""Diagnostics for SMA Bluetooth."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import (
    CONF_BT_ADDRESS,
    CONF_KNOWN_INVERTERS,
    CONF_PLANT_NAME,
    CONF_SELECTED_SERIAL,
)
from .coordinator import SMABluetoothConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SMABluetoothConfigEntry
) -> dict[str, Any]:
    """Return redacted configuration and current inverter metadata."""
    coordinator = entry.runtime_data
    return {
        "config_entry": async_redact_data(
            dict(entry.data),
            {
                CONF_PASSWORD,
                CONF_BT_ADDRESS,
                CONF_KNOWN_INVERTERS,
                CONF_PLANT_NAME,
                CONF_SELECTED_SERIAL,
            },
        ),
        "daylight": coordinator.is_daylight(),
        "sleeping": coordinator.sleeping,
        "connection_open": coordinator.connection_open,
        "connection": {
            "configured_mode": coordinator.connection_mode,
            "effective_mode": coordinator.effective_mode,
            "net_id": coordinator.net_id,
            "current_root": "**REDACTED**" if coordinator.current_root else None,
            "adapter_active_address": "**REDACTED**"
            if coordinator.adapter_gate.active_address
            else None,
            "adapter_lock_waiters_serialized": True,
            "consecutive_transport_failures": (
                coordinator.adapter_gate.consecutive_transport_failures
            ),
            "last_adapter_recovery_at": (
                coordinator.adapter_gate.last_recovery_at.isoformat()
                if coordinator.adapter_gate.last_recovery_at is not None
                else None
            ),
            "adapter_recovery_count": coordinator.adapter_gate.recovery_count,
            "adapter_recovery_issue_open": (
                coordinator.adapter_gate.recovery_issue_open
            ),
        },
        "update_interval_seconds": (
            coordinator.update_interval.total_seconds()
            if coordinator.update_interval is not None
            else None
        ),
        "clock": {
            "checked_at": entry.options.get("last_clock_check_at"),
            "difference_seconds": entry.options.get("last_clock_difference_seconds"),
            "last_synchronized_at": entry.options.get("last_clock_sync_at"),
            "result": entry.options.get("last_clock_result"),
        },
        "inverters": {
            f"inverter_{index}": {
                "model": inverter.model,
                "software_version": inverter.software_version,
                "bluetooth_address": "**REDACTED**"
                if inverter.bluetooth_address
                else None,
                "network_role": inverter.network_role,
                "record_timestamp": inverter.record_timestamp,
                "record_received_at": inverter.record_received_at,
                "record_clock_difference": inverter.record_clock_difference,
                "entity_owner": serial in coordinator.owned_serials,
                "available_values": sorted(inverter.values),
            }
            for index, (serial, inverter) in enumerate(
                coordinator.data.items(), start=1
            )
        },
    }
