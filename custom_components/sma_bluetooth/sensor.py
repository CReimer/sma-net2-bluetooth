"""Sensor platform for SMA Bluetooth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SMABluetoothCoordinator
from .device import hub_device_info, inverter_device_info


@dataclass(frozen=True, kw_only=True)
class SMASensorDescription(SensorEntityDescription):
    """Describe one SMA value."""

    suffix: str


DESCRIPTIONS: tuple[SMASensorDescription, ...] = (
    SMASensorDescription(
        key="ac_power_total",
        name="Power",
        suffix="power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SMASensorDescription(
        key="energy_today",
        name="Energy today",
        suffix="energy_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SMASensorDescription(
        key="energy_total",
        name="Energy total",
        suffix="energy_total",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SMASensorDescription(
        key="temperature",
        name="Temperature",
        suffix="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SMASensorDescription(key="status", name="Status", suffix="status"),
    SMASensorDescription(
        key="relay_status", name="Relay status", suffix="relay_status"
    ),
    SMASensorDescription(
        key="bt_signal",
        name="Bluetooth signal",
        suffix="bluetooth_signal",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SMASensorDescription(
        key="frequency",
        name="Grid frequency",
        suffix="frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SMASensorDescription(
        key="operation_time",
        name="Operating time",
        suffix="operating_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL,
    ),
    SMASensorDescription(
        key="feed_in_time",
        name="Feed-in time",
        suffix="feed_in_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SMASensorDescription(
        key="dc_power_total",
        name="DC power total",
        suffix="dc_power_total",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    *tuple(
        SMASensorDescription(
            key=f"dc_{kind}_{channel}",
            name=f"DC {kind} MPPT {channel}",
            suffix=f"dc_{kind}_{channel}",
            device_class=device_class,
            native_unit_of_measurement=unit,
            state_class=SensorStateClass.MEASUREMENT,
        )
        for channel in (1, 2)
        for kind, device_class, unit in (
            ("power", SensorDeviceClass.POWER, UnitOfPower.WATT),
            ("voltage", SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
            ("current", SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
        )
    ),
    *tuple(
        SMASensorDescription(
            key=f"ac_{kind}_{phase}",
            name=f"AC {kind} phase {phase}",
            suffix=f"ac_{kind}_{phase}",
            device_class=device_class,
            native_unit_of_measurement=unit,
            state_class=SensorStateClass.MEASUREMENT,
        )
        for phase in (1, 2, 3)
        for kind, device_class, unit in (
            ("power", SensorDeviceClass.POWER, UnitOfPower.WATT),
            ("voltage", SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
            ("current", SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
        )
    ),
)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up all discovered inverter sensors."""
    coordinator: SMABluetoothCoordinator = hass.data[DOMAIN][entry.entry_id]
    serials = sorted(set(coordinator.data) & coordinator.owned_serials)
    inverter_entities = [
        SMASensor(coordinator, serial, description)
        for serial in serials
        for inverter in (coordinator.data[serial],)
        for description in DESCRIPTIONS
        if description.key in inverter.values
    ]
    async_add_entities(
        [
            SMAConnectionModeSensor(coordinator, entry),
            SMANetIDSensor(coordinator, entry),
            SMACurrentRootSensor(coordinator, entry),
            SMAPlantClockDifferenceSensor(coordinator, entry),
            *inverter_entities,
            *(SMAInverterRoleSensor(coordinator, serial) for serial in serials),
            *(
                SMAInverterRecordTimestampSensor(coordinator, serial)
                for serial in serials
            ),
            *(
                SMAInverterClockDifferenceSensor(coordinator, serial)
                for serial in serials
            ),
        ]
    )


class SMASensor(CoordinatorEntity[SMABluetoothCoordinator], SensorEntity):
    """A value read from an SMA Bluetooth Classic inverter."""

    entity_description: SMASensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SMABluetoothCoordinator,
        serial: str,
        description: SMASensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._serial = serial
        self._attr_unique_id = f"sma_bluetooth_{serial}_{description.key}"
        self._attr_suggested_object_id = f"sma_{serial}_{description.suffix}"

    @property
    def available(self) -> bool:
        """Expose cached identities but no stale values while sleeping."""
        return super().available and not self.coordinator.sleeping

    @property
    def device_info(self) -> DeviceInfo:
        return inverter_device_info(self.coordinator, self._serial)

    @property
    def native_value(self) -> Any:
        inverter = self.coordinator.data.get(self._serial)
        return inverter.values.get(self.entity_description.key) if inverter else None


class SMAPlantClockDifferenceSensor(
    CoordinatorEntity[SMABluetoothCoordinator], SensorEntity
):
    """Expose the signed difference between the SMA plant and HA clocks."""

    _attr_has_entity_name = True
    _attr_name = "Plant clock difference at check"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_object_id = "sma_pv_plant_clock_difference"

    def __init__(
        self, coordinator: SMABluetoothCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"sma_bluetooth_{entry.entry_id}_plant_clock_difference"

    @property
    def available(self) -> bool:
        """Keep the latest explicitly timestamped observation visible at night."""
        value = self._entry.options.get("last_clock_difference_seconds")
        return (
            super().available
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        )

    @property
    def native_value(self) -> int | float | None:
        value = self._entry.options.get("last_clock_difference_seconds")
        return (
            value
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Attach the legacy clock sensor to the new stable hub."""
        return hub_device_info(self._entry)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "checked_at": self._entry.options.get("last_clock_check_at"),
            "last_synchronized_at": self._entry.options.get("last_clock_sync_at"),
            "result": self._entry.options.get("last_clock_result"),
        }


class SMAHubDiagnosticSensor(CoordinatorEntity[SMABluetoothCoordinator], SensorEntity):
    """Base for diagnostics attached to the logical SMA-Net2 hub."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: SMABluetoothCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        return hub_device_info(self._entry)


class SMAConnectionModeSensor(SMAHubDiagnosticSensor):
    """Expose the effective and configured connection modes."""

    _attr_name = "Connection mode"

    def __init__(
        self, coordinator: SMABluetoothCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"sma_bluetooth_{entry.entry_id}_connection_mode"

    @property
    def native_value(self) -> str:
        return self.coordinator.effective_mode or self.coordinator.connection_mode

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {"configured_mode": self.coordinator.connection_mode}


class SMANetIDSensor(SMAHubDiagnosticSensor):
    """Expose the NetID detected in the most recent SMA session."""

    _attr_name = "NetID"

    def __init__(
        self, coordinator: SMABluetoothCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"sma_bluetooth_{entry.entry_id}_net_id"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.net_id

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        if self.coordinator.net_id is None:
            return None
        return {"hex": f"{self.coordinator.net_id:X}"}


class SMACurrentRootSensor(SMAHubDiagnosticSensor):
    """Expose the current root serial or unidentified root Bluetooth address."""

    _attr_name = "Current root node"

    def __init__(
        self, coordinator: SMABluetoothCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"sma_bluetooth_{entry.entry_id}_root_node"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.current_root


class SMAInverterDiagnosticSensor(
    CoordinatorEntity[SMABluetoothCoordinator], SensorEntity
):
    """Base for diagnostics attached to a stable inverter device."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SMABluetoothCoordinator, serial: str) -> None:
        super().__init__(coordinator)
        self._serial = serial

    @property
    def device_info(self) -> DeviceInfo:
        return inverter_device_info(self.coordinator, self._serial)

    def _inverter(self):
        return self.coordinator.data.get(self._serial)


class SMAInverterRoleSensor(SMAInverterDiagnosticSensor):
    """Expose direct/root/participant without affecting via-device hierarchy."""

    _attr_name = "Network role"

    def __init__(self, coordinator: SMABluetoothCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"sma_bluetooth_{serial}_network_role"

    @property
    def native_value(self) -> str | None:
        inverter = self._inverter()
        return inverter.network_role if inverter is not None else None


class SMAInverterRecordTimestampSensor(SMAInverterDiagnosticSensor):
    """Expose the last SMA record timestamp in transfer order."""

    _attr_name = "Record timestamp"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: SMABluetoothCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"sma_bluetooth_{serial}_record_timestamp"

    @property
    def native_value(self) -> datetime | None:
        inverter = self._inverter()
        if inverter is None or inverter.record_timestamp is None:
            return None
        return datetime.fromtimestamp(inverter.record_timestamp, UTC)

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        inverter = self._inverter()
        if inverter is None or inverter.record_received_at is None:
            return None
        return {
            "received_at": datetime.fromtimestamp(
                inverter.record_received_at, UTC
            ).isoformat()
        }


class SMAInverterClockDifferenceSensor(SMAInverterDiagnosticSensor):
    """Expose raw SMA record time minus its matching receive time."""

    _attr_name = "Record clock difference"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: SMABluetoothCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"sma_bluetooth_{serial}_record_clock_difference"

    @property
    def native_value(self) -> float | None:
        inverter = self._inverter()
        return inverter.record_clock_difference if inverter is not None else None
