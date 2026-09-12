"""Discovery cleanup and configuration error/confirmation contracts."""

import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from dbus_fast import Variant
from custom_components.sma_bluetooth import config_flow as cf, discovery as d, repairs
from custom_components.sma_bluetooth.const import (
    CONF_BT_ADDRESS,
    CONF_CONNECTION_MODE,
    CONF_PLANT_NAME,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_SERIAL,
    EFFECTIVE_MODE_SINGLE,
)
from custom_components.sma_bluetooth.models import SMAInverter
from custom_components.sma_bluetooth.protocol import (
    SMAAuthenticationError,
    SMAProtocolError,
    SMANetworkModeError,
)
from custom_components.sma_bluetooth.gateway import SMADaylightError


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flow = cf.SMABluetoothConfigFlow()
        self.flow.hass = NS()
        self.data = {
            CONF_BT_ADDRESS: "aa:bb:cc:dd:ee:ff",
            "password": "p",
            CONF_CONNECTION_MODE: "auto",
            CONF_PLANT_NAME: "Plant",
            CONF_SCAN_INTERVAL: 60,
        }
        self.probe = cf._ProbeResult(
            {"1": SMAInverter(serial="1")},
            1,
            EFFECTIVE_MODE_SINGLE,
            "AA:BB:CC:DD:EE:FF",
        )

    async def test_discovery_form_and_address_selector(self):
        with patch.object(
            cf,
            "async_discover_sma_devices",
            AsyncMock(side_effect=d.SMADiscoveryError("dbus")),
        ):
            self.assertEqual(
                (await self.flow.async_step_user())["errors"],
                {"base": "discovery_failed"},
            )
        with patch.object(
            cf,
            "async_discover_sma_devices",
            AsyncMock(return_value={"AA:BB:CC:DD:EE:FF": "SMA"}),
        ):
            self.assertEqual((await self.flow.async_step_user())["errors"], {})
        field, default = self.flow._address_selector("11:22:33:44:55:66")
        self.assertEqual(default, "11:22:33:44:55:66")
        self.assertEqual(field(default), default)

    async def test_validation_errors_and_password_reuse(self):
        result = await self.flow.async_step_user({**self.data, CONF_BT_ADDRESS: "bad"})
        self.assertEqual(result["errors"], {CONF_BT_ADDRESS: "invalid_address"})
        for error, expected in [
            (SMADaylightError("night"), "nighttime"),
            (SMANetworkModeError(1), "network_requires_netid_2_f"),
            (SMAAuthenticationError("auth"), "invalid_auth"),
            (SMAProtocolError("wire"), "cannot_connect"),
        ]:
            with patch.object(self.flow, "_async_probe", AsyncMock(side_effect=error)):
                self.assertEqual(
                    (await self.flow.async_step_user(dict(self.data)))["errors"],
                    {"base": expected},
                )
        entry = NS(entry_id="entry", data=self.data, options={"keep": True})
        with (
            patch.object(self.flow, "_get_reconfigure_entry", return_value=entry),
            patch.object(self.flow, "_async_discover", AsyncMock(return_value=True)),
            patch.object(self.flow, "_async_probe", AsyncMock(return_value=self.probe)),
            patch.object(cf, "entries_claiming_serials", return_value={}),
        ):
            self.assertEqual(
                (await self.flow.async_step_reconfigure())["step_id"], "reconfigure"
            )
            self.assertEqual(
                (
                    await self.flow.async_step_reconfigure(
                        {**self.data, CONF_BT_ADDRESS: "bad"}
                    )
                )["errors"],
                {CONF_BT_ADDRESS: "invalid_address"},
            )
            with patch.object(
                self.flow,
                "_async_probe",
                AsyncMock(side_effect=SMAProtocolError("wire")),
            ):
                self.assertEqual(
                    (await self.flow.async_step_reconfigure(dict(self.data)))["errors"],
                    {"base": "cannot_connect"},
                )
            result = await self.flow.async_step_reconfigure(
                {**self.data, "password": ""}
            )
            self.assertEqual(result["step_id"], "reconfigure_confirm")
            self.assertEqual(self.flow._pending_data["password"], "p")
            self.assertEqual(
                (await self.flow.async_step_reconfigure_confirm())["step_id"],
                "reconfigure_confirm",
            )
            with patch.object(
                self.flow,
                "async_update_reload_and_abort",
                return_value={"type": "abort"},
            ) as update:
                await self.flow.async_step_reconfigure_confirm({})
                self.assertEqual(update.call_args.kwargs["unique_id"], "single:1")
                self.assertTrue(update.call_args.kwargs["options"]["keep"])

    async def test_confirmation_single_network_and_missing_pending(self):
        self.assertEqual(
            (await self.flow.async_step_confirm())["reason"], "cannot_connect"
        )
        self.assertEqual(
            (await self.flow.async_step_reconfigure_confirm())["reason"],
            "cannot_connect",
        )
        for mode in (EFFECTIVE_MODE_SINGLE, "network"):
            self.probe.effective_mode = mode
            with (
                patch.object(
                    self.flow, "_async_probe", AsyncMock(return_value=self.probe)
                ),
                patch.object(cf, "entries_claiming_serials", return_value={}),
                patch.object(self.flow, "async_set_unique_id", AsyncMock()) as unique,
                patch.object(self.flow, "_abort_if_unique_id_configured"),
            ):
                self.assertEqual(
                    (await self.flow.async_step_user(dict(self.data)))["step_id"],
                    "confirm",
                )
                self.assertEqual(
                    (await self.flow.async_step_confirm())["step_id"], "confirm"
                )
                result = await self.flow.async_step_confirm({})
                unique.assert_awaited_once_with(f"{mode}:1")
                self.assertEqual(result["title"], "Plant")
                self.assertEqual(
                    CONF_SELECTED_SERIAL in result["data"],
                    mode == EFFECTIVE_MODE_SINGLE,
                )

    async def test_probe_requires_metadata(self):
        client = NS(
            async_query_active=AsyncMock(return_value={}),
            net_id=None,
            effective_mode=None,
        )

        async def run(hass, address, password, mode, operation, **kwargs):
            return await operation(client)

        with patch.object(cf, "async_get_adapter_gate", return_value=NS(async_run=run)):
            with self.assertRaises(SMAProtocolError):
                await self.flow._async_probe(self.data)

    async def test_repairs_stay_open_until_issue_resolved(self):
        flow = await repairs.async_create_fix_flow(
            NS(), "issue", {"owner": 12, "absent": None}
        )
        flow.hass = NS()
        result = await flow.async_step_init()
        self.assertEqual(result["description_placeholders"], {"owner": "12"})
        registry = Mock()
        with patch.object(repairs.ir, "async_get", return_value=registry):
            self.assertEqual(
                (await flow.async_step_init({}))["errors"], {"base": "still_present"}
            )
            registry.async_get_issue.return_value = None
            self.assertEqual((await flow.async_step_init({}))["data"], {})
        self.assertEqual(
            (await repairs.async_create_fix_flow(NS(), "issue", None))._placeholders, {}
        )


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_filters_devices_and_stops_adapters(self):
        adapter = NS(
            call_set_discovery_filter=AsyncMock(),
            call_start_discovery=AsyncMock(),
            call_stop_discovery=AsyncMock(side_effect=RuntimeError("already stopped")),
        )
        objects = {"/adapter": {"org.bluez.Adapter1": {}}}

        def device(address=None, name=None, alias=None):
            return {
                "org.bluez.Device1": {
                    k: Variant("s", v)
                    for k, v in [("Address", address), ("Name", name), ("Alias", alias)]
                    if v is not None
                }
            }

        found = {
            **objects,
            "/a": device("aa:bb", "SMA one"),
            "/b": device("cc:dd", alias="sma two"),
            "/c": device("ee:ff", "Other"),
            "/d": device(name="SMA missing address"),
            "/e": device("ff:ee"),
        }
        manager = NS(call_get_managed_objects=AsyncMock(side_effect=[objects, found]))
        bus = NS(
            introspect=AsyncMock(),
            get_proxy_object=Mock(
                side_effect=lambda name, path, intro: NS(
                    get_interface=Mock(return_value=manager if path == "/" else adapter)
                )
            ),
            disconnect=Mock(),
        )
        with (
            patch.object(
                d, "MessageBus", return_value=NS(connect=AsyncMock(return_value=bus))
            ),
            patch.object(d.asyncio, "sleep", AsyncMock()),
        ):
            self.assertEqual(
                await d.async_discover_sma_devices(),
                {"AA:BB": "SMA one", "CC:DD": "sma two"},
            )
        adapter.call_start_discovery.assert_awaited_once()
        adapter.call_stop_discovery.assert_awaited_once()
        bus.disconnect.assert_called_once()

    async def test_discovery_errors_and_cancellation_disconnect(self):
        with patch.object(
            d,
            "MessageBus",
            return_value=NS(connect=AsyncMock(side_effect=OSError("bus"))),
        ):
            with self.assertRaisesRegex(d.SMADiscoveryError, "Unable to connect"):
                await d.async_discover_sma_devices()
        for error in (None, RuntimeError("introspect"), asyncio.CancelledError()):
            manager = NS(call_get_managed_objects=AsyncMock(return_value={}))
            bus = NS(
                introspect=AsyncMock(side_effect=error),
                get_proxy_object=Mock(
                    return_value=NS(get_interface=Mock(return_value=manager))
                ),
                disconnect=Mock(),
            )
            with patch.object(
                d, "MessageBus", return_value=NS(connect=AsyncMock(return_value=bus))
            ):
                with self.assertRaises(
                    asyncio.CancelledError
                    if isinstance(error, asyncio.CancelledError)
                    else d.SMADiscoveryError
                ):
                    await d.async_discover_sma_devices()
            bus.disconnect.assert_called_once()
