"""Exercise configuration through Home Assistant's real flow manager."""

import unittest
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries, loader
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from sma_net2 import SMAInverter
from sma_net2.protocol import SMAAuthenticationError

from custom_components.sma_bluetooth import config_flow as cf
from custom_components.sma_bluetooth.const import DOMAIN


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

    async def test_reauth_retries_errors_and_updates_only_the_password(self):
        entry = await self.create_entry()
        original_data, original_options = dict(entry.data), dict(entry.options)
        original_id, original_unique_id = entry.entry_id, entry.unique_id
        result = await self.start({"source": "reauth", "entry_id": entry.entry_id})
        self.assertEqual(result["step_id"], "reauth_confirm")
        errors = [
            (cf.SMADaylightError("night"), "nighttime"),
            (cf.SMANetworkModeError(1), "network_requires_netid_2_f"),
            (cf.SMAAuthenticationError("bad password"), "invalid_auth"),
            (cf.SMAProtocolError("offline"), "cannot_connect"),
        ]
        for error, reason in errors:
            with (
                self.subTest(reason=reason),
                patch.object(
                    cf.SMABluetoothConfigFlow,
                    "_async_probe",
                    AsyncMock(side_effect=error),
                ),
            ):
                result = await self.submit(result, {"password": "new"})
                self.assertEqual(result["errors"], {"base": reason})
                self.assertEqual(dict(entry.data), original_data)
                self.assertEqual(dict(entry.options), original_options)
        with patch.object(
            cf.SMABluetoothConfigFlow,
            "_async_probe",
            AsyncMock(return_value=self.probe),
        ) as probe:
            result = await self.submit(result, {"password": "new"})
        self.assertEqual(result["reason"], "reauth_successful")
        self.assertEqual(dict(entry.data), {**original_data, "password": "new"})
        self.assertEqual(dict(entry.options), original_options)
        self.assertEqual(entry.entry_id, original_id)
        self.assertEqual(entry.unique_id, original_unique_id)
        self.assertEqual(
            probe.call_args.args[0]["bt_address"], original_data["bt_address"]
        )
        self.assertEqual(probe.call_args.args[0]["password"], "new")
        await self.hass.async_block_till_done()
        self.hass.config_entries.async_reload.assert_awaited_once_with(original_id)
        self.assertEqual(len(self.hass.config_entries.async_entries(DOMAIN)), 1)

    async def test_reauth_rejects_another_device_and_changed_netid(self):
        entry = await self.create_entry()
        original_data, original_options = dict(entry.data), dict(entry.options)
        for probe, reason in [
            (
                cf._ProbeResult({"2": SMAInverter(serial="2")}, 1, "single", "2"),
                "wrong_device",
            ),
            (
                cf._ProbeResult({"1": SMAInverter(serial="1")}, 2, "single", "1"),
                "topology_changed",
            ),
        ]:
            with (
                self.subTest(reason=reason),
                patch.object(
                    cf.SMABluetoothConfigFlow,
                    "_async_probe",
                    AsyncMock(return_value=probe),
                ),
            ):
                result = await self.start(
                    {"source": "reauth", "entry_id": entry.entry_id}
                )
                result = await self.submit(result, {"password": "new"})
            self.assertEqual(result["reason"], reason)
            self.assertEqual(dict(entry.data), original_data)
            self.assertEqual(dict(entry.options), original_options)
        self.hass.config_entries.async_reload.assert_not_awaited()

    async def test_update_failure_logs_once_recovers_and_starts_reauth(self):
        from datetime import timedelta
        from types import SimpleNamespace

        from custom_components.sma_bluetooth import coordinator as c
        from custom_components.sma_bluetooth import sensor as s

        entry = await self.create_entry()
        co = c.SMABluetoothCoordinator(self.hass, entry)
        co.data = self.probe.inverters
        co.owned_serials = {"1"}
        sensor = s.SMASensor(co, "1", s.DESCRIPTIONS[0])
        with (
            patch.object(
                c, "daylight_schedule", return_value=SimpleNamespace(active=True)
            ) as schedule,
            patch.object(c, "async_reconcile_ownership", return_value={"1"}),
            patch.object(
                co,
                "async_run_session",
                AsyncMock(
                    side_effect=[
                        cf.SMAProtocolError("offline"),
                        cf.SMAProtocolError("offline"),
                        self.probe.inverters,
                    ]
                ),
            ) as session,
            self.assertLogs(c._LOGGER, level="INFO") as logs,
        ):
            await co.async_refresh()
            self.assertFalse(sensor.available)
            await co.async_refresh()
            schedule.return_value = SimpleNamespace(
                active=False, next_interval=timedelta(hours=6)
            )
            await co.async_enter_night()
            self.assertFalse(co.last_update_success)
            await co.async_refresh()
            self.assertFalse(co.last_update_success)
            self.assertFalse(sensor.available)
            self.assertEqual(session.await_count, 2)
            schedule.return_value = SimpleNamespace(active=True)
            await co.async_refresh()
            self.assertTrue(sensor.available)
            self.assertEqual(session.await_count, 3)
        self.assertEqual(len(logs.records), 2)
        self.assertIn("offline", logs.records[0].getMessage())
        self.assertIn("recovered", logs.records[1].getMessage())
        with (
            patch.object(
                c, "daylight_schedule", return_value=SimpleNamespace(active=True)
            ),
            patch.object(
                co,
                "async_run_session",
                AsyncMock(side_effect=cf.SMAAuthenticationError("bad password")),
            ),
            self.assertLogs(c._LOGGER, level="ERROR"),
        ):
            await co.async_refresh()
        self.assertFalse(sensor.available)
        await self.hass.async_block_till_done()
        progress = self.hass.config_entries.flow.async_progress()
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0]["context"]["source"], "reauth")
        self.assertEqual(progress[0]["context"]["entry_id"], entry.entry_id)
        result = await self.hass.config_entries.flow.async_configure(
            progress[0]["flow_id"]
        )
        self.assertEqual(result["step_id"], "reauth_confirm")

    async def test_password_dialog_translations_load_in_home_assistant(self):
        import json
        from pathlib import Path

        from homeassistant.helpers import translation

        directory = Path(cf.__file__).parent
        integration = loader.Integration(
            self.hass,
            "custom_components.sma_bluetooth",
            directory,
            json.loads((directory / "manifest.json").read_text()),
            {path.name for path in directory.iterdir()},
        )
        with patch.object(
            translation,
            "async_get_integrations",
            AsyncMock(return_value={DOMAIN: integration}),
        ):
            for language in ("en", "de"):
                with self.subTest(language=language):
                    strings = await translation.async_get_translations(
                        self.hass, language, "config", {DOMAIN}
                    )
                    prefix = f"component.{DOMAIN}.config."
                    for key in (
                        "step.reauth_confirm.title",
                        "step.reauth_confirm.description",
                        "step.reauth_confirm.data.password",
                        "step.reauth_confirm.data_description.password",
                        "error.invalid_auth",
                        "error.cannot_connect",
                        "error.nighttime",
                        "abort.reauth_successful",
                        "abort.wrong_device",
                        "abort.topology_changed",
                    ):
                        self.assertTrue(strings[prefix + key])
                        self.assertNotIn("[%key", strings[prefix + key])

                    entities = await translation.async_get_translations(
                        self.hass, language, "entity", {DOMAIN}
                    )
                    for key in (
                        "sensor.ac_power_total.name",
                        "sensor.connection_mode.name",
                        "event.events.name",
                        "event.events.state_attributes.event_type.state.fault",
                    ):
                        self.assertTrue(entities[f"component.{DOMAIN}.entity.{key}"])
                    exceptions = await translation.async_get_translations(
                        self.hass, language, "exceptions", {DOMAIN}
                    )
                    for key in (
                        "archive_read",
                        "invalid_auth",
                        "poll_failed",
                        "archive_alignment",
                    ):
                        self.assertTrue(
                            exceptions[f"component.{DOMAIN}.exceptions.{key}.message"]
                        )
