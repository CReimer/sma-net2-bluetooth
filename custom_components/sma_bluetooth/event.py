"""Event entities for SMA inverter state transitions."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SMABluetoothCoordinator
from .device import inverter_device_info

EVENT_WARNING = "warning"
EVENT_FAULT = "fault"
EVENT_GRID_CONNECTED = "grid_connected"
EVENT_GRID_DISCONNECTED = "grid_disconnected"
EVENT_TYPES = [
    EVENT_WARNING,
    EVENT_FAULT,
    EVENT_GRID_CONNECTED,
    EVENT_GRID_DISCONNECTED,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one event entity per inverter."""
    coordinator: SMABluetoothCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        SMAInverterEvent(coordinator, serial)
        for serial in sorted(set(coordinator.data) & coordinator.owned_serials)
    )


class SMAInverterEvent(CoordinatorEntity[SMABluetoothCoordinator], EventEntity):
    """Publish meaningful inverter status transitions."""

    _attr_event_types = EVENT_TYPES
    _attr_has_entity_name = True
    _attr_name = "Events"

    def __init__(self, coordinator: SMABluetoothCoordinator, serial: str) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = f"sma_bluetooth_{serial}_events"
        self._attr_suggested_object_id = f"sma_{serial}_events"
        values = coordinator.data[serial].values
        self._last_status = values.get("status")
        self._last_relay = values.get("relay_status")

    @property
    def available(self) -> bool:
        """Mark inverter events unavailable while the plant sleeps."""
        return super().available and not self.coordinator.sleeping

    @property
    def device_info(self) -> DeviceInfo:
        return inverter_device_info(self.coordinator, self._serial)

    def _handle_coordinator_update(self) -> None:
        inverter = self.coordinator.data.get(self._serial)
        if inverter is None:
            super()._handle_coordinator_update()
            return

        status = inverter.values.get("status")
        relay = inverter.values.get("relay_status")
        if status != self._last_status:
            normalized = str(status).casefold()
            event_type = None
            if "warn" in normalized:
                event_type = EVENT_WARNING
            elif "fault" in normalized or "error" in normalized:
                event_type = EVENT_FAULT
            if event_type:
                self._trigger_event(
                    event_type,
                    {"previous_status": self._last_status, "status": status},
                )
            self._last_status = status

        if relay != self._last_relay:
            normalized = str(relay).casefold()
            event_type = None
            if normalized == "closed":
                event_type = EVENT_GRID_CONNECTED
            elif normalized == "open":
                event_type = EVENT_GRID_DISCONNECTED
            if event_type:
                self._trigger_event(
                    event_type,
                    {"previous_status": self._last_relay, "status": relay},
                )
            self._last_relay = relay

        super()._handle_coordinator_update()
