"""Stable Home Assistant device identities for SMA Bluetooth."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_PLANT_NAME, DOMAIN, HUB_IDENTIFIER_PREFIX
from .coordinator import SMABluetoothCoordinator


def hub_identifier(entry: ConfigEntry) -> tuple[str, str]:
    """Return the stable logical connection hub identifier."""
    return (DOMAIN, f"{HUB_IDENTIFIER_PREFIX}{entry.entry_id}")


def hub_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Describe the stable Home Assistant SMA-Net2 connection hub."""
    plant_name = entry.data.get(CONF_PLANT_NAME, entry.title)
    return DeviceInfo(
        identifiers={hub_identifier(entry)},
        name=f"{plant_name} – SMA-Net2",
        manufacturer="SMA Solar Technology",
        model="SMA-Net2 Bluetooth connection",
    )


def async_ensure_hub_device(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create the hub before any inverter platform resolves its via link."""
    info = hub_device_info(entry)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={hub_identifier(entry)},
        name=info["name"],
        manufacturer=info["manufacturer"],
        model=info["model"],
    )


def inverter_device_info(
    coordinator: SMABluetoothCoordinator, serial: str
) -> DeviceInfo:
    """Describe one inverter below the stable connection hub."""
    inverter = coordinator.data[serial]
    hub = None
    if hasattr(coordinator, "hass"):
        hub = dr.async_get(coordinator.hass).async_get_device_by_identifier(
            hub_identifier(coordinator.entry), coordinator.entry.entry_id
        )
    info = DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=inverter.name or f"SMA {serial}",
        manufacturer="SMA Solar Technology",
        model=inverter.model,
        sw_version=inverter.software_version,
        serial_number=serial,
    )
    if hub is not None:
        info["via_device_id"] = hub.id
    else:
        # The sensor platform creates the hub before adding inverter entities;
        # retain the identifier fallback for isolated platform tests.
        info["via_device"] = hub_identifier(coordinator.entry)
    return info
