"""Shared entity identity and coordinator lifecycle for SMA Bluetooth."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import SMABluetoothCoordinator
from .device import inverter_device_info


class SMAEntity(CoordinatorEntity[SMABluetoothCoordinator]):
    """Subscribe via CoordinatorEntity when added and unsubscribe when removed."""

    _attr_has_entity_name = True


class SMAInverterEntity(SMAEntity):
    """Attach inverter entities to their stable serial-number device."""

    def __init__(self, coordinator: SMABluetoothCoordinator, serial: str) -> None:
        super().__init__(coordinator)
        self._serial = serial

    @property
    def device_info(self) -> DeviceInfo:
        return inverter_device_info(self.coordinator, self._serial)
