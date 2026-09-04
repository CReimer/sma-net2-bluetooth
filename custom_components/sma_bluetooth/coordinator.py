"""Data update coordinator for SMA Bluetooth."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
import logging
from typing import TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_CONNECTION_MODE,
    CONF_KNOWN_INVERTERS,
    CONF_LAST_DETECTED_NET_ID,
    CONF_LAST_DETECTED_SERIALS,
    CONF_NET_ID,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_SERIAL,
    DEFAULT_CONNECTION_MODE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EFFECTIVE_MODE_SINGLE,
    NETWORK_ROLE_DIRECT,
    NETWORK_ROLE_PARTICIPANT,
    NETWORK_ROLE_ROOT,
    RFCOMM_SESSION_ATTEMPTS,
    UPDATE_TIMEOUT,
)
from .daylight import daylight_schedule
from .gateway import SMADaylightError, async_get_adapter_gate
from .models import SMAInverter
from .ownership import (
    async_clear_netid_issues,
    async_note_netid_change,
    async_reconcile_ownership,
)
from .protocol import (
    SMAArchivePoint,
    SMAClassicClient,
    SMAClockSyncResult,
    SMAConfigurationError,
    SMANetworkModeError,
    SMAProtocolError,
)

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")


def serialize_known_inverters(
    inverters: dict[str, SMAInverter],
) -> list[dict[str, object]]:
    """Serialize identity and available value keys for stable night startup."""
    return [
        {
            "serial": inverter.serial,
            "susy_id": inverter.susy_id,
            "name": inverter.name,
            "model": inverter.model,
            "software_version": inverter.software_version,
            "bluetooth_address": inverter.bluetooth_address,
            "network_role": inverter.network_role,
            "value_keys": sorted(inverter.values),
        }
        for _, inverter in sorted(inverters.items())
    ]


def deserialize_known_inverters(raw: object) -> dict[str, SMAInverter]:
    """Restore entity identities without treating cached values as live."""
    if not isinstance(raw, list):
        return {}
    restored: dict[str, SMAInverter] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("serial"), str):
            continue
        serial = item["serial"]
        value_keys = item.get("value_keys", [])
        if not isinstance(value_keys, list):
            value_keys = []
        restored[serial] = SMAInverter(
            serial=serial,
            susy_id=item.get("susy_id")
            if isinstance(item.get("susy_id"), str)
            else None,
            name=item.get("name") if isinstance(item.get("name"), str) else None,
            model=item.get("model") if isinstance(item.get("model"), str) else None,
            software_version=(
                item.get("software_version")
                if isinstance(item.get("software_version"), str)
                else None
            ),
            bluetooth_address=(
                item.get("bluetooth_address")
                if isinstance(item.get("bluetooth_address"), str)
                else None
            ),
            network_role=(
                item.get("network_role")
                if isinstance(item.get("network_role"), str)
                else None
            ),
            values={str(key): None for key in value_keys if isinstance(key, str)},
        )
    return restored


class SMANetIDChangedError(SMAConfigurationError):
    """The detected physical topology no longer matches confirmed config."""


class SMABluetoothCoordinator(DataUpdateCoordinator[dict[str, SMAInverter]]):
    """Coordinate one logical SMA entry through the adapter-wide session gate."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
            always_update=True,
            config_entry=entry,
        )
        self.entry = entry
        self.address = entry.data["bt_address"]
        self.password = entry.data["password"]
        self.connection_mode = entry.data.get(
            CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE
        )
        self.configured_net_id = entry.data.get(CONF_NET_ID)
        self.selected_serial = entry.data.get(CONF_SELECTED_SERIAL)
        self.net_id = (
            self.configured_net_id if isinstance(self.configured_net_id, int) else None
        )
        self.effective_mode: str | None = None
        self.current_root: str | None = None
        self.daytime_interval = timedelta(
            seconds=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        self.sleeping = False
        self.adapter_gate = async_get_adapter_gate(hass)
        self._known_inverters = deserialize_known_inverters(
            entry.options.get(
                CONF_KNOWN_INVERTERS, entry.data.get(CONF_KNOWN_INVERTERS)
            )
        )
        self.owned_serials = set(self._known_inverters)
        self.archive_reconcile_task: asyncio.Task[None] | None = None
        self.archive_listener_remove: Callable[[], None] | None = None
        self.archive_last_attempt = None

    async def _async_update_data(self) -> dict[str, SMAInverter]:
        schedule = daylight_schedule(self.hass)
        if not schedule.active:
            self.sleeping = True
            self.update_interval = schedule.next_interval
            return self.data or self._known_inverters

        self.sleeping = False
        self.update_interval = self.daytime_interval
        try:
            data = await self.async_run_session(
                lambda client: client.async_query_active(), timeout=UPDATE_TIMEOUT
            )
        except SMADaylightError:
            schedule = daylight_schedule(self.hass)
            self.sleeping = True
            self.update_interval = schedule.next_interval
            return self.data or self._known_inverters
        except SMANetworkModeError as err:
            self.net_id = err.net_id
            async_note_netid_change(
                self.hass, self.entry, err.net_id, self._known_inverters
            )
            raise UpdateFailed(str(err)) from err
        except SMAProtocolError as err:
            raise UpdateFailed(str(err)) from err

        self._remember_inverters(data)
        self.owned_serials = async_reconcile_ownership(self.hass, self.entry, data)
        return data

    def is_daylight(self) -> bool:
        """Return whether a new scheduled SMA operation may communicate."""
        return daylight_schedule(self.hass).active

    @property
    def connection_open(self) -> bool:
        """Return whether this entry currently owns the adapter session."""
        return self.adapter_gate.active_address == self.address

    def _remember_inverters(self, data: dict[str, SMAInverter]) -> None:
        """Persist identity shape only when it changes."""
        self._known_inverters = data
        snapshot = serialize_known_inverters(data)
        if self.entry.options.get(CONF_KNOWN_INVERTERS) == snapshot:
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, CONF_KNOWN_INVERTERS: snapshot},
        )

    def _apply_session_metadata(self, client: SMAClassicClient) -> None:
        """Apply NetID/root facts without changing stable device hierarchy."""
        if client.net_id is None or client.effective_mode is None:
            raise SMAProtocolError("SMA session did not expose its NetID and mode")
        self.net_id = client.net_id
        self.effective_mode = client.effective_mode
        self.current_root = client.current_root

        if self.data:
            address_roles = {
                client.format_bluetooth_address(device.address): (
                    NETWORK_ROLE_DIRECT
                    if client.effective_mode == EFFECTIVE_MODE_SINGLE
                    else (
                        NETWORK_ROLE_ROOT
                        if device.address == client.root_address
                        else NETWORK_ROLE_PARTICIPANT
                    )
                )
                for device in client.devices
            }
            for inverter in self.data.values():
                inverter.network_role = address_roles.get(
                    inverter.bluetooth_address, inverter.network_role
                )

        detected_serials = {str(device.serial) for device in client.devices}
        if (
            isinstance(self.configured_net_id, int)
            and client.net_id != self.configured_net_id
        ):
            async_note_netid_change(
                self.hass, self.entry, client.net_id, detected_serials
            )
            raise SMANetIDChangedError(
                f"SMA NetID changed from {self.configured_net_id:X} "
                f"to {client.net_id:X}; follow the Home Assistant repair"
            )

        if self.selected_serial and client.effective_mode == EFFECTIVE_MODE_SINGLE:
            if detected_serials != {self.selected_serial}:
                raise SMAProtocolError(
                    "The directly connected SMA inverter no longer matches "
                    f"configured serial {self.selected_serial}"
                )

        data_updates: dict[str, object] = {}
        if not isinstance(self.configured_net_id, int):
            self.configured_net_id = client.net_id
            data_updates[CONF_NET_ID] = client.net_id
        if (
            self.selected_serial is None
            and client.effective_mode == EFFECTIVE_MODE_SINGLE
            and len(detected_serials) == 1
        ):
            self.selected_serial = next(iter(detected_serials))
            data_updates[CONF_SELECTED_SERIAL] = self.selected_serial
        if data_updates:
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, **data_updates},
            )
        detection_options = {
            **self.entry.options,
            CONF_LAST_DETECTED_NET_ID: client.net_id,
            CONF_LAST_DETECTED_SERIALS: sorted(detected_serials),
        }
        if any(
            self.entry.options.get(key) != detection_options[key]
            for key in (CONF_LAST_DETECTED_NET_ID, CONF_LAST_DETECTED_SERIALS)
        ):
            self.hass.config_entries.async_update_entry(
                self.entry, options=detection_options
            )
        async_clear_netid_issues(self.hass, self.entry.entry_id)

    async def async_run_session(
        self,
        operation: Callable[[SMAClassicClient], Awaitable[_T]],
        *,
        timeout: float | None = None,
        attempts: int = RFCOMM_SESSION_ATTEMPTS,
    ) -> _T:
        """Run one self-contained operation through the global adapter gate."""

        async def _validated_operation(client: SMAClassicClient) -> _T:
            self._apply_session_metadata(client)
            result = await operation(client)
            self._apply_session_metadata(client)
            return result

        return await self.adapter_gate.async_run(
            self.hass,
            self.address,
            self.password,
            self.connection_mode,
            _validated_operation,
            timeout=timeout,
            attempts=attempts,
        )

    async def async_disconnect(self) -> None:
        """No-op: every adapter-gated operation already disconnects itself."""

    async def async_enter_night(self) -> None:
        """Mark entities stale at sunset without opening or closing RFCOMM."""
        schedule = daylight_schedule(self.hass)
        if schedule.active:
            return
        self.sleeping = True
        self.update_interval = schedule.next_interval
        self.async_set_updated_data(self.data or self._known_inverters)

    async def async_read_archive(
        self, periods: list[int | tuple[int, int]]
    ) -> dict[str, list[SMAArchivePoint]]:
        """Read archive data through a dedicated adapter-wide session."""
        timeout = UPDATE_TIMEOUT + len(periods) * 15
        result = await self.async_run_session(
            lambda client: client.async_read_archive_active(periods), timeout=timeout
        )
        self.async_update_listeners()
        return result

    async def async_sync_clock(
        self, timezone_offset: int, dst_active: bool
    ) -> SMAClockSyncResult:
        """Synchronize the logical plant/direct-inverter clock once per entry."""
        result = await self.async_run_session(
            lambda client: client.async_sync_clock_active(timezone_offset, dst_active),
            timeout=UPDATE_TIMEOUT,
        )
        self.async_update_listeners()
        return result
