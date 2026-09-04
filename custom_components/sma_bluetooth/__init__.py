"""SMA Bluetooth integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models.statistics import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_sunset
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .archive import (
    completed_day_periods,
    cumulative_statistic_sums,
    hourly_last_values,
    timestamp_series_complete,
)
from .const import (
    CONF_CONNECTION_MODE,
    CONF_KNOWN_INVERTERS,
    CONF_SELECTED_SERIAL,
    CONNECTION_MODE_AUTO,
    CONNECTION_MODE_NETWORK,
    DOMAIN,
    MAX_ARCHIVE_DAYS,
    PLATFORMS,
)
from .coordinator import SMABluetoothCoordinator, deserialize_known_inverters
from .device import async_ensure_hub_device
from .ownership import (
    async_reconcile_ownership,
    async_refresh_overlap_issues,
    async_transfer_departing_entry,
)
from .protocol import SMAProtocolError

SERVICE_IMPORT_ARCHIVE = "import_archive"
SERVICE_GET_ARCHIVE = "get_archive"
CONF_DAYS = "days"
CONF_CONFIG_ENTRY_ID = "config_entry_id"
CONF_START = "start"
CONF_END = "end"
ARCHIVE_RETRY_INTERVAL = timedelta(minutes=30)
STATISTICS_ANCHOR_LOOKBACK = timedelta(days=MAX_ARCHIVE_DAYS + 1)
_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _local_today() -> datetime:
    """Return local midnight in Home Assistant's configured timezone."""
    return dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)


def _managed_serials(coordinator: SMABluetoothCoordinator) -> set[str]:
    """Return serials whose entities are owned by this entry."""
    if hasattr(coordinator, "owned_serials"):
        return set(coordinator.owned_serials)
    return set(coordinator.data)


async def _async_import_periods(
    hass: HomeAssistant,
    coordinator: SMABluetoothCoordinator,
    periods: list[tuple[int, int]],
    *,
    require_complete: bool,
) -> dict[str, int]:
    """Import official archive readings as supported hourly Recorder statistics."""
    archive = await coordinator.async_read_archive(periods)

    if require_complete:
        expected_timestamps = {
            timestamp for start, end in periods for timestamp in range(start, end, 300)
        }
        incomplete = {
            serial: len({point.timestamp for point in archive.get(serial, [])})
            for serial in _managed_serials(coordinator)
            if {point.timestamp for point in archive.get(serial, [])}
            != expected_timestamps
        }
        if incomplete:
            details = ", ".join(
                f"{serial}: {actual}/{len(expected_timestamps)}"
                for serial, actual in incomplete.items()
            )
            raise SMAProtocolError(f"Incomplete SMA archive ({details})")

    imported: dict[str, int] = {}
    registry = er.async_get(hass)
    energy_entity_ids: dict[str, str] = {}
    missing_entities: list[str] = []
    for serial in _managed_serials(coordinator):
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"sma_bluetooth_{serial}_energy_total"
        )
        if entity_id is None:
            missing_entities.append(serial)
        else:
            energy_entity_ids[serial] = entity_id
    if missing_entities:
        raise SMAProtocolError(
            "Missing total-energy entities for SMA inverter(s): "
            + ", ".join(sorted(missing_entities))
        )

    current_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    hourly_by_entity: dict[str, list[tuple[datetime, float]]] = {}
    for serial, points in archive.items():
        entity_id = energy_entity_ids.get(serial)
        if entity_id is None:
            continue
        hourly = [
            (hour, value)
            for hour, value in sorted(hourly_last_values(points).items())
            if hour < current_hour
        ]
        if not hourly:
            continue
        hourly_by_entity[entity_id] = hourly

    if not hourly_by_entity:
        return imported

    first_hour = min(hourly[0][0] for hourly in hourly_by_entity.values())
    end_hour = max(hourly[-1][0] for hourly in hourly_by_entity.values()) + timedelta(
        hours=1
    )
    statistic_ids = set(hourly_by_entity)
    recorder = get_instance(hass)
    before, after = await asyncio.gather(
        recorder.async_add_executor_job(
            statistics_during_period,
            hass,
            first_hour - STATISTICS_ANCHOR_LOOKBACK,
            first_hour,
            statistic_ids,
            "hour",
            None,
            {"state", "sum"},
        ),
        recorder.async_add_executor_job(
            statistics_during_period,
            hass,
            end_hour,
            end_hour + STATISTICS_ANCHOR_LOOKBACK,
            statistic_ids,
            "hour",
            None,
            {"state", "sum"},
        ),
    )

    def anchor(rows: list[dict[str, Any]], *, last: bool) -> tuple[float, float] | None:
        usable = [
            row
            for row in rows
            if row.get("state") is not None and row.get("sum") is not None
        ]
        if not usable:
            return None
        row = usable[-1] if last else usable[0]
        return float(row["state"]), float(row["sum"])

    for entity_id, hourly in hourly_by_entity.items():
        states = [value for _, value in hourly]
        sums = cumulative_statistic_sums(
            states,
            anchor(before.get(entity_id, []), last=True),
            anchor(after.get(entity_id, []), last=False),
        )
        statistics = [
            StatisticData(start=hour, state=value, sum=sum_value)
            for (hour, value), sum_value in zip(hourly, sums, strict=True)
        ]
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        async_import_statistics(hass, metadata, statistics)
        imported[entity_id] = len(statistics)
    await recorder.async_block_till_done()
    return imported


async def _async_reconcile_previous_day(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: SMABluetoothCoordinator
) -> None:
    """Reconcile the previous completed day after the first successful poll."""
    local_today = _local_today()
    target_date = (local_today - timedelta(days=1)).date().isoformat()
    if entry.options.get("last_archive_reconcile_date") == target_date:
        return

    try:
        local_now = dt_util.now()
        dst_delta = local_now.dst() or timedelta()
        utc_offset = local_now.utcoffset() or timedelta()
        standard_offset = int((utc_offset - dst_delta).total_seconds())
        clock_result = await coordinator.async_sync_clock(
            standard_offset, bool(dst_delta)
        )
        if clock_result.reason == "unsafe_difference":
            _LOGGER.error(
                "SMA plant clock differs by %s seconds; refused automatic adjustment",
                clock_result.difference_seconds,
            )
        elif clock_result.adjusted:
            _LOGGER.info(
                "Synchronized SMA plant clock (previous difference: %s seconds)",
                clock_result.difference_seconds,
            )
        clock_checked_at = dt_util.utcnow()
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                "last_clock_check_at": clock_checked_at.isoformat(),
                "last_clock_check_date": local_today.date().isoformat(),
                "last_clock_difference_seconds": clock_result.difference_seconds,
                "last_clock_result": clock_result.reason,
                "last_clock_sync_at": (
                    clock_checked_at.isoformat()
                    if clock_result.adjusted
                    else entry.options.get("last_clock_sync_at")
                ),
            },
        )
        coordinator.async_update_listeners()

        imported = await _async_import_periods(
            hass,
            coordinator,
            completed_day_periods(local_today, 1),
            require_complete=True,
        )
        if len(imported) != len(_managed_serials(coordinator)):
            raise HomeAssistantError("Not every SMA total-energy entity was imported")
    except (HomeAssistantError, SMAProtocolError) as err:
        _LOGGER.warning("Could not reconcile SMA archive for %s: %s", target_date, err)
        return

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "last_archive_reconcile_date": target_date,
            "last_archive_reconcile_at": dt_util.utcnow().isoformat(),
        },
    )
    _LOGGER.info(
        "Reconciled SMA archive for %s: %s",
        target_date,
        ", ".join(f"{entity_id}={count}" for entity_id, count in imported.items()),
    )


def _schedule_archive_reconcile(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: SMABluetoothCoordinator
) -> None:
    """Schedule one archive reconciliation without overlapping Bluetooth work."""
    if not coordinator.is_daylight():
        return
    target_date = (_local_today() - timedelta(days=1)).date().isoformat()
    if entry.options.get("last_archive_reconcile_date") == target_date:
        return
    task = coordinator.archive_reconcile_task
    if task is not None and not task.done():
        return
    last_attempt = getattr(coordinator, "archive_last_attempt", None)
    now = dt_util.utcnow()
    if last_attempt is not None and now - last_attempt < ARCHIVE_RETRY_INTERVAL:
        return
    coordinator.archive_last_attempt = now
    coordinator.archive_reconcile_task = hass.async_create_task(
        _async_reconcile_previous_day(hass, entry, coordinator),
        f"{DOMAIN} archive reconciliation",
    )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the explicitly invoked archive import action."""

    async def async_import_archive(call: ServiceCall) -> dict[str, Any]:
        days = call.data[CONF_DAYS]
        requested_entry = call.data.get(CONF_CONFIG_ENTRY_ID)
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.entry_id in hass.data.get(DOMAIN, {})
            and (requested_entry is None or entry.entry_id == requested_entry)
        ]
        if not entries:
            raise ServiceValidationError("No loaded SMA Bluetooth entry found")

        local_today = _local_today()
        imported: dict[str, int] = {}

        for entry in entries:
            coordinator: SMABluetoothCoordinator = hass.data[DOMAIN][entry.entry_id]
            imported.update(
                await _async_import_periods(
                    hass,
                    coordinator,
                    completed_day_periods(local_today, days),
                    require_complete=True,
                )
            )

            hass.config_entries.async_update_entry(
                entry,
                options={
                    **entry.options,
                    "last_archive_import": dt_util.utcnow().isoformat(),
                    "last_archive_import_days": days,
                },
            )
        return {"imported_hourly_statistics": imported}

    async def async_get_archive(call: ServiceCall) -> dict[str, Any]:
        """Return unmodified official five-minute points for a bounded range."""
        start: datetime = call.data[CONF_START]
        end: datetime = call.data[CONF_END]
        if start.tzinfo is None or start.utcoffset() is None:
            raise ServiceValidationError("Archive start must include a timezone")
        if end.tzinfo is None or end.utcoffset() is None:
            raise ServiceValidationError("Archive end must include a timezone")
        start_timestamp = int(start.timestamp())
        end_timestamp = int(end.timestamp())
        if end_timestamp <= start_timestamp:
            raise ServiceValidationError("Archive end must be after start")
        if start_timestamp % 300 or end_timestamp % 300:
            raise ServiceValidationError(
                "Archive start and end must be aligned to five-minute boundaries"
            )
        if end_timestamp - start_timestamp > MAX_ARCHIVE_DAYS * 86400 + 3600:
            raise ServiceValidationError(
                f"Archive range cannot exceed {MAX_ARCHIVE_DAYS} days"
            )

        requested_entry = call.data.get(CONF_CONFIG_ENTRY_ID)
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.entry_id in hass.data.get(DOMAIN, {})
            and (requested_entry is None or entry.entry_id == requested_entry)
        ]
        if not entries:
            raise ServiceValidationError("No loaded SMA Bluetooth entry found")

        periods: list[tuple[int, int]] = []
        cursor = start_timestamp
        while cursor < end_timestamp:
            period_end = min(cursor + 86400, end_timestamp)
            periods.append((cursor, period_end))
            cursor = period_end

        response: dict[str, Any] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "plants": {},
        }
        for entry in entries:
            coordinator: SMABluetoothCoordinator = hass.data[DOMAIN][entry.entry_id]
            try:
                archive = await coordinator.async_read_archive(periods)
            except SMAProtocolError as err:
                raise ServiceValidationError(
                    f"Could not read SMA archive: {err}"
                ) from err
            inverter_response: dict[str, Any] = {}
            timestamp_sets: list[set[int]] = []
            for serial in coordinator.data:
                points = sorted(
                    {
                        point.timestamp: point
                        for point in archive.get(serial, [])
                        if start_timestamp <= point.timestamp < end_timestamp
                    }.values(),
                    key=lambda point: point.timestamp,
                )
                timestamp_sets.append({point.timestamp for point in points})
                inverter_response[serial] = {
                    "count": len(points),
                    "points": [
                        {
                            "timestamp": point.timestamp,
                            "total_energy_kwh": point.total_energy_kwh,
                            "power_w": point.power_w,
                        }
                        for point in points
                    ],
                }
            if not inverter_response or any(
                timestamps != timestamp_sets[0] for timestamps in timestamp_sets[1:]
            ):
                raise ServiceValidationError(
                    "SMA inverters returned different archive timestamps"
                )
            require_all_slots = end_timestamp <= int(_local_today().timestamp())
            complete = timestamp_series_complete(
                timestamp_sets,
                start_timestamp,
                end_timestamp,
                require_all_slots=require_all_slots,
            )
            response["plants"][entry.entry_id] = {
                "requested_slots": (end_timestamp - start_timestamp) // 300,
                "received_slots": len(timestamp_sets[0]),
                "complete": complete,
                "inverters": inverter_response,
            }
        return response

    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_ARCHIVE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_IMPORT_ARCHIVE,
            async_import_archive,
            schema=vol.Schema(
                {
                    vol.Required(CONF_DAYS, default=3): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=MAX_ARCHIVE_DAYS)
                    ),
                    vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_GET_ARCHIVE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_ARCHIVE,
            async_get_archive,
            schema=vol.Schema(
                {
                    vol.Required(CONF_START): cv.datetime,
                    vol.Required(CONF_END): cv.datetime,
                    vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
                }
            ),
            supports_response=SupportsResponse.ONLY,
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SMA Bluetooth from a config entry."""
    coordinator = SMABluetoothCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    coordinator.owned_serials = async_reconcile_ownership(
        hass, entry, coordinator.data or coordinator._known_inverters
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    async_ensure_hub_device(hass, entry)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_disconnect()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    coordinator.archive_listener_remove = coordinator.async_add_listener(
        lambda: _schedule_archive_reconcile(hass, entry, coordinator)
    )

    @callback
    def _close_at_sunset() -> None:
        entry.async_create_background_task(
            hass,
            coordinator.async_enter_night(),
            f"{DOMAIN} sunset disconnect",
        )

    entry.async_on_unload(async_track_sunset(hass, _close_at_sunset))
    _schedule_archive_reconcile(hass, entry, coordinator)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate legacy address-based entries to explicit topology schema v2."""
    if entry.version > 2:
        return False
    if entry.version == 2:
        return True

    known = deserialize_known_inverters(
        entry.options.get(CONF_KNOWN_INVERTERS, entry.data.get(CONF_KNOWN_INVERTERS))
    )
    connection_mode = (
        CONNECTION_MODE_NETWORK if len(known) > 1 else CONNECTION_MODE_AUTO
    )
    migrated_data = {**entry.data, CONF_CONNECTION_MODE: connection_mode}
    if len(known) == 1:
        migrated_data[CONF_SELECTED_SERIAL] = next(iter(known))
    hass.config_entries.async_update_entry(
        entry,
        data=migrated_data,
        version=2,
    )
    _LOGGER.info(
        "Migrated SMA entry %s to schema v2 using %s mode for %s known inverter(s)",
        entry.entry_id,
        connection_mode,
        len(known),
    )
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Preserve inverter registry rows during a guided entry consolidation."""
    owner_entry_id = async_transfer_departing_entry(hass, entry)
    if owner_entry_id is not None:
        _LOGGER.info(
            "Transferred SMA inverter registries from removed entry %s to %s",
            entry.entry_id,
            owner_entry_id,
        )
    async_refresh_overlap_issues(hass)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: SMABluetoothCoordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.archive_listener_remove is not None:
        coordinator.archive_listener_remove()
        coordinator.archive_listener_remove = None
    if (
        coordinator.archive_reconcile_task is not None
        and not coordinator.archive_reconcile_task.done()
    ):
        coordinator.archive_reconcile_task.cancel()
        try:
            await coordinator.archive_reconcile_task
        except asyncio.CancelledError:
            pass
    await coordinator.async_disconnect()
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
