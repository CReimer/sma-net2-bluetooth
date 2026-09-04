"""Config and reconfigure flows for SMA Bluetooth."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_BT_ADDRESS,
    CONF_CONNECTION_MODE,
    CONF_KNOWN_INVERTERS,
    CONF_NET_ID,
    CONF_PLANT_NAME,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_SERIAL,
    CONNECTION_MODES,
    DEFAULT_CONNECTION_MODE,
    DEFAULT_PLANT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EFFECTIVE_MODE_SINGLE,
    MIN_SCAN_INTERVAL,
    UPDATE_TIMEOUT,
)
from .coordinator import serialize_known_inverters
from .discovery import SMADiscoveryError, async_discover_sma_devices
from .gateway import SMADaylightError, async_get_adapter_gate
from .models import SMAInverter
from .ownership import entries_claiming_serials
from .protocol import (
    SMAAuthenticationError,
    SMAClassicClient,
    SMANetworkModeError,
    SMAProtocolError,
)

_BT_ADDRESS_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


@dataclass(slots=True)
class _ProbeResult:
    inverters: dict[str, SMAInverter]
    net_id: int
    effective_mode: str
    current_root: str


class SMABluetoothConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle SMA Bluetooth configuration and reconfiguration."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize one discovery/configuration flow."""
        super().__init__()
        self._discovered: dict[str, str] = {}
        self._pending_data: dict[str, Any] | None = None
        self._pending_probe: _ProbeResult | None = None
        self._reconfigure = False

    async def _async_discover(self) -> bool:
        try:
            self._discovered = await async_discover_sma_devices()
        except SMADiscoveryError:
            return False
        return True

    async def _async_probe(self, data: dict[str, Any]) -> _ProbeResult:
        gate = async_get_adapter_gate(self.hass)

        async def _query(client: SMAClassicClient) -> _ProbeResult:
            inverters = await client.async_query_active()
            if client.net_id is None or client.effective_mode is None:
                raise SMAProtocolError("SMA did not report NetID/mode")
            return _ProbeResult(
                inverters=inverters,
                net_id=client.net_id,
                effective_mode=client.effective_mode,
                current_root=client.current_root or data[CONF_BT_ADDRESS],
            )

        return await gate.async_run(
            self.hass,
            data[CONF_BT_ADDRESS],
            data[CONF_PASSWORD],
            data[CONF_CONNECTION_MODE],
            _query,
            timeout=UPDATE_TIMEOUT,
        )

    def _address_selector(self, default: str | None) -> tuple[Any, str | None]:
        options = dict(self._discovered)
        if default and default not in options:
            options[default] = "Configured SMA device"
        if not options:
            return str, default
        return (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=address, label=f"{name} ({address})"
                        )
                        for address, name in options.items()
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            default or next(iter(options)),
        )

    def _schema(self, defaults: dict[str, Any], *, reconfigure: bool) -> vol.Schema:
        address_field, address_default = self._address_selector(
            defaults.get(CONF_BT_ADDRESS)
        )
        address_key = (
            vol.Required(CONF_BT_ADDRESS, default=address_default)
            if address_default
            else vol.Required(CONF_BT_ADDRESS)
        )
        password_key: vol.Marker = (
            vol.Optional(CONF_PASSWORD, default="")
            if reconfigure
            else vol.Required(CONF_PASSWORD)
        )
        return vol.Schema(
            {
                address_key: address_field,
                password_key: str,
                vol.Required(
                    CONF_CONNECTION_MODE,
                    default=defaults.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(CONNECTION_MODES),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="connection_mode",
                    )
                ),
                vol.Required(
                    CONF_PLANT_NAME,
                    default=defaults.get(CONF_PLANT_NAME, DEFAULT_PLANT_NAME),
                ): str,
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL)),
            }
        )

    async def _async_validate(
        self,
        user_input: dict[str, Any],
        *,
        existing_entry: config_entries.ConfigEntry | None,
    ) -> str | None:
        address = str(user_input[CONF_BT_ADDRESS]).upper()
        user_input[CONF_BT_ADDRESS] = address
        if not _BT_ADDRESS_RE.fullmatch(address):
            return "invalid_address"

        if existing_entry is not None and not user_input.get(CONF_PASSWORD):
            user_input[CONF_PASSWORD] = existing_entry.data[CONF_PASSWORD]

        try:
            probe = await self._async_probe(user_input)
        except SMADaylightError:
            return "nighttime"
        except SMANetworkModeError:
            return "network_requires_netid_2_f"
        except SMAAuthenticationError:
            return "invalid_auth"
        except SMAProtocolError:
            return "cannot_connect"

        claims = entries_claiming_serials(
            self.hass,
            probe.inverters,
            exclude_entry_id=(
                existing_entry.entry_id if existing_entry is not None else None
            ),
        )
        if any(claims.values()):
            return "serial_overlap"

        pending = dict(user_input)
        pending[CONF_NET_ID] = probe.net_id
        pending[CONF_KNOWN_INVERTERS] = serialize_known_inverters(probe.inverters)
        if probe.effective_mode == EFFECTIVE_MODE_SINGLE:
            pending[CONF_SELECTED_SERIAL] = next(iter(probe.inverters))
        else:
            pending.pop(CONF_SELECTED_SERIAL, None)
        self._pending_data = pending
        self._pending_probe = probe
        return None

    def _confirmation_placeholders(self) -> dict[str, str]:
        assert self._pending_data is not None
        assert self._pending_probe is not None
        return {
            "net_id": f"{self._pending_probe.net_id:X}",
            "configured_mode": self._pending_data[CONF_CONNECTION_MODE],
            "effective_mode": self._pending_probe.effective_mode,
            "serials": ", ".join(sorted(self._pending_probe.inverters)),
            "root": self._pending_probe.current_root,
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select one visible device and validate exactly that SMA system."""
        errors: dict[str, str] = {}
        if user_input is None and not await self._async_discover():
            errors["base"] = "discovery_failed"
        elif user_input is not None:
            if error := await self._async_validate(user_input, existing_entry=None):
                errors["base" if error != "invalid_address" else CONF_BT_ADDRESS] = (
                    error
                )
            else:
                return self.async_show_form(
                    step_id="confirm",
                    data_schema=vol.Schema({}),
                    description_placeholders=self._confirmation_placeholders(),
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self._schema({}, reconfigure=False),
            errors=errors,
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Create the confirmed entry after showing detected topology facts."""
        if self._pending_data is None or self._pending_probe is None:
            return self.async_abort(reason="cannot_connect")
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders=self._confirmation_placeholders(),
            )

        serial = min(self._pending_probe.inverters)
        unique_id = f"{self._pending_probe.effective_mode}:{serial}"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self._pending_data[CONF_PLANT_NAME],
            data=self._pending_data,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-detect and explicitly confirm a changed SMA topology."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is None:
            await self._async_discover()
        else:
            if error := await self._async_validate(user_input, existing_entry=entry):
                errors["base" if error != "invalid_address" else CONF_BT_ADDRESS] = (
                    error
                )
            else:
                self._reconfigure = True
                return self.async_show_form(
                    step_id="reconfigure_confirm",
                    data_schema=vol.Schema({}),
                    description_placeholders=self._confirmation_placeholders(),
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._schema(dict(entry.data), reconfigure=True),
            errors=errors,
        )

    async def async_step_reconfigure_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Apply a confirmed topology while retaining registry identities."""
        if self._pending_data is None or self._pending_probe is None:
            return self.async_abort(reason="cannot_connect")
        if user_input is None:
            return self.async_show_form(
                step_id="reconfigure_confirm",
                data_schema=vol.Schema({}),
                description_placeholders=self._confirmation_placeholders(),
            )
        entry = self._get_reconfigure_entry()
        snapshot = self._pending_data[CONF_KNOWN_INVERTERS]
        serial = min(self._pending_probe.inverters)
        unique_id = f"{self._pending_probe.effective_mode}:{serial}"
        return self.async_update_reload_and_abort(
            entry,
            unique_id=unique_id,
            title=self._pending_data[CONF_PLANT_NAME],
            data=self._pending_data,
            options={**entry.options, CONF_KNOWN_INVERTERS: snapshot},
        )
