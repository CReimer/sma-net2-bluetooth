"""Exercise configuration through Home Assistant's real flow manager."""

import unittest
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries, loader
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.sma_bluetooth import config_flow as cf
from custom_components.sma_bluetooth.const import DOMAIN
from sma_net2 import SMAInverter
from sma_net2.protocol import SMAAuthenticationError


class ConfigFlowManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        loader.async_setup(self.hass)
        self.hass.config_entries = config_entries.ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        self.patches = [
            patch.object(
                config_entries,
                "_async_get_flow_handler",
                AsyncMock(return_value=cf.SMABluetoothConfigFlow),
            ),
            patch.object(
                config_entries,
                "_support_single_config_entry_only",
                AsyncMock(return_value=False),
            ),
            patch.object(
                self.hass.config_entries, "async_setup", AsyncMock(return_value=True)
            ),
            patch.object(
                self.hass.config_entries, "async_reload", AsyncMock(return_value=True)
            ),
            patch.object(cf, "async_discover_sma_devices", AsyncMock(return_value={})),
        ]
        for item in self.patches:
            item.start()
        self.input = {
            "bt_address": "aa:bb:cc:dd:ee:ff",
            "password": "p",
            "connection_mode": "auto",
            "plant_name": "Roof",
            "scan_interval": 120,
        }
        self.probe = cf._ProbeResult({"1": SMAInverter(serial="1")}, 1, "single", "1")

    async def asyncTearDown(self):
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def start(self, context=None):
        return await self.hass.config_entries.flow.async_init(
            DOMAIN, context=context or {"source": "user"}
        )

    async def submit(self, result, data):
        return await self.hass.config_entries.flow.async_configure(
            result["flow_id"], data
        )

    async def test_user_recovers_from_auth_error_and_creates_single_entry(self):
        with patch.object(
            cf.SMABluetoothConfigFlow,
            "_async_probe",
            AsyncMock(side_effect=[SMAAuthenticationError("bad password"), self.probe]),
        ):
            result = await self.start()
            self.assertEqual(result["step_id"], "user")
            result = await self.submit(result, self.input)
            self.assertEqual(result["errors"], {"base": "invalid_auth"})
            result = await self.submit(result, self.input)
            self.assertEqual(result["step_id"], "confirm")
            result = await self.submit(result, {})
        self.assertIs(result["type"], FlowResultType.CREATE_ENTRY)
        entry = result["result"]
        self.assertEqual(entry.unique_id, "single:1")
        self.assertEqual(entry.data["bt_address"], "AA:BB:CC:DD:EE:FF")
        self.assertNotIn("scan_interval", entry.data)
        self.assertNotIn("plant_name", entry.data)
        self.assertEqual(entry.options["scan_interval"], 120)
        self.assertEqual(entry.options["known_inverters"][0]["serial"], "1")
        with patch.object(
            cf.SMABluetoothConfigFlow,
            "_async_probe",
            AsyncMock(return_value=self.probe),
        ):
            duplicate = await self.start()
            duplicate = await self.submit(duplicate, self.input)
        self.assertEqual(duplicate["errors"], {"base": "serial_overlap"})
        self.assertEqual(len(self.hass.config_entries.async_entries(DOMAIN)), 1)

    async def create_entry(self, probe=None):
        with patch.object(
            cf.SMABluetoothConfigFlow,
            "_async_probe",
            AsyncMock(return_value=probe or self.probe),
        ):
            result = await self.start()
            result = await self.submit(result, self.input)
            result = await self.submit(result, {})
        self.assertIs(result["type"], FlowResultType.CREATE_ENTRY)
        return result["result"]

    async def test_network_reconfigure_recovers_and_retains_entry_identity(self):
        network = cf._ProbeResult(
            {"1": SMAInverter(serial="1"), "2": SMAInverter(serial="2")},
            2,
            "network",
            "2",
        )
        entry = await self.create_entry(network)
        original_id = entry.entry_id
        result = await self.start({"source": "reconfigure", "entry_id": entry.entry_id})
        self.assertEqual(result["step_id"], "reconfigure")
        with patch.object(
            cf.SMABluetoothConfigFlow,
            "_async_probe",
            AsyncMock(side_effect=[cf.SMAProtocolError("offline"), network]),
        ) as probe:
            result = await self.submit(
                result,
                {**self.input, "bt_address": "AA:BB:CC:DD:EE:FF", "password": ""},
            )
            self.assertEqual(result["errors"], {"base": "cannot_connect"})
            result = await self.submit(
                result,
                {
                    **self.input,
                    "bt_address": "AA:BB:CC:DD:EE:FF",
                    "password": "",
                    "scan_interval": 180,
                },
            )
            self.assertEqual(result["step_id"], "reconfigure_confirm")
            self.assertEqual(probe.call_args.args[0]["password"], "p")
            result = await self.submit(result, {})
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(entry.entry_id, original_id)
        self.assertEqual(entry.unique_id, "network:1")
        self.assertEqual(entry.options["scan_interval"], 180)
        self.assertEqual(entry.data["password"], "p")
        self.assertNotIn("selected_serial", entry.data)
        self.assertEqual(len(self.hass.config_entries.async_entries(DOMAIN)), 1)

    async def test_two_confirmations_cannot_create_overlapping_entries(self):
        # Different network minima produce different unique IDs but share serial 2.
        left = cf._ProbeResult(
            {"1": SMAInverter(serial="1"), "2": SMAInverter(serial="2")},
            2,
            "network",
            "1",
        )
        right = cf._ProbeResult(
            {"2": SMAInverter(serial="2"), "3": SMAInverter(serial="3")},
            2,
            "network",
            "2",
        )
        with patch.object(
            cf.SMABluetoothConfigFlow,
            "_async_probe",
            AsyncMock(side_effect=[left, right]),
        ):
            first, second = await self.start(), await self.start()
            first = await self.submit(first, self.input)
            second = await self.submit(second, self.input)
        self.assertEqual(first["step_id"], "confirm")
        self.assertEqual(second["step_id"], "confirm")
        first = await self.submit(first, {})
        self.assertIs(first["type"], FlowResultType.CREATE_ENTRY)
        second = await self.submit(second, {})
        self.assertEqual(second["reason"], "already_configured")
        self.assertEqual(len(self.hass.config_entries.async_entries(DOMAIN)), 1)

    async def test_unique_id_also_blocks_legacy_entry_without_identity_cache(self):
        entry = await self.create_entry()
        self.hass.config_entries.async_update_entry(entry, options={})
        with patch.object(
            cf.SMABluetoothConfigFlow,
            "_async_probe",
            AsyncMock(return_value=self.probe),
        ):
            result = await self.start()
            result = await self.submit(result, self.input)
            self.assertEqual(result["step_id"], "confirm")
            result = await self.submit(result, {})
        self.assertEqual(result["reason"], "already_configured")
        self.assertEqual(len(self.hass.config_entries.async_entries(DOMAIN)), 1)

    async def test_reconfigure_confirmation_rechecks_overlapping_serials(self):
        entry = await self.create_entry()
        changed = cf._ProbeResult({"2": SMAInverter(serial="2")}, 1, "single", "2")
        with patch.object(
            cf.SMABluetoothConfigFlow, "_async_probe", AsyncMock(return_value=changed)
        ):
            result = await self.start(
                {"source": "reconfigure", "entry_id": entry.entry_id}
            )
            result = await self.submit(
                result, {**self.input, "bt_address": "AA:BB:CC:DD:EE:FF"}
            )
            self.assertEqual(result["step_id"], "reconfigure_confirm")
            await self.create_entry(changed)
            result = await self.submit(result, {})
        self.assertEqual(result["reason"], "already_configured")
        self.assertEqual(entry.unique_id, "single:1")

    async def test_reconfigure_blocks_unique_id_of_legacy_entry_without_cache(self):
        entry = await self.create_entry()
        other_probe = cf._ProbeResult({"2": SMAInverter(serial="2")}, 1, "single", "2")
        other = await self.create_entry(other_probe)
        self.hass.config_entries.async_update_entry(other, options={})
        with patch.object(
            cf.SMABluetoothConfigFlow,
            "_async_probe",
            AsyncMock(return_value=other_probe),
        ):
            result = await self.start(
                {"source": "reconfigure", "entry_id": entry.entry_id}
            )
            result = await self.submit(
                result, {**self.input, "bt_address": "AA:BB:CC:DD:EE:FF"}
            )
            self.assertEqual(result["step_id"], "reconfigure_confirm")
            result = await self.submit(result, {})
        self.assertEqual(result["reason"], "already_configured")
        self.assertEqual(entry.unique_id, "single:1")
