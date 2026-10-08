"""Dynamic devices, privacy, safe removal and frontend localization contracts."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from sma_net2 import SMAInverter

from custom_components import sma_bluetooth as integration
from custom_components.sma_bluetooth import device, diagnostics, event, sensor
from tests.test_coordinator_platforms import make_coordinator

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/sma_bluetooth"


class GoldContracts(unittest.IsolatedAsyncioTestCase):
    async def test_new_owned_devices_and_measurements_added_without_reload(self):
        for platform in (sensor, event):
            co = make_coordinator()
            entities = []
            await platform.async_setup_entry(co.hass, co.entry, entities.extend)
            listener = co.async_add_listener.call_args.args[0]
            co.entry.async_on_unload.assert_called_once_with(
                co.async_add_listener.return_value
            )
            initial_ids = {entity.unique_id for entity in entities}
            co.data["2"] = SMAInverter(serial="2", values={"energy_total": 42})
            listener()  # Not owned by this entry: never create duplicates.
            self.assertEqual({entity.unique_id for entity in entities}, initial_ids)
            co.owned_serials.add("2")
            co.data["1"].values["energy_total"] = 100
            listener()
            ids = [entity.unique_id for entity in entities]
            self.assertIn(
                f"sma_bluetooth_2_{'energy_total' if platform is sensor else 'events'}",
                ids,
            )
            if platform is sensor:
                self.assertIn("sma_bluetooth_1_energy_total", ids)
            listener()
            self.assertEqual([entity.unique_id for entity in entities], ids)
            self.assertEqual(len(ids), len(set(ids)))
            co.data.pop("2")
            listener()  # Missing devices remain unavailable, keeping history intact.
            self.assertEqual([entity.unique_id for entity in entities], ids)
            co.data["2"] = SMAInverter(serial="2")
            listener()
            self.assertEqual([entity.unique_id for entity in entities], ids)

    async def test_only_explicitly_absent_inverters_can_be_removed(self):
        co = make_coordinator()
        for ids, allowed in [
            ({("sma_bluetooth", "1")}, False),
            ({device.hub_identifier(co.entry)}, False),
            ({("sma_bluetooth", "2")}, True),
            ({("other", "1")}, True),
        ]:
            row = NS(identifiers=ids)
            self.assertEqual(
                await integration.async_remove_config_entry_device(
                    co.hass, co.entry, row
                ),
                allowed,
            )
        co._known_inverters = {"1": co.data["1"], "2": SMAInverter(serial="2")}
        co.owned_serials.add("2")
        co.entry.options["known_inverters"] = [{"serial": "1"}, {"serial": "2"}]
        removed = NS(identifiers={("sma_bluetooth", "2")})
        self.assertTrue(
            await integration.async_remove_config_entry_device(
                co.hass, co.entry, removed
            )
        )
        self.assertNotIn("2", co._known_inverters)
        self.assertNotIn("2", co.owned_serials)
        self.assertEqual(
            [row["serial"] for row in co.entry.options["known_inverters"]], ["1"]
        )
        missing = NS(identifiers={("sma_bluetooth", "2")})
        co.last_update_success = False
        self.assertFalse(
            await integration.async_remove_config_entry_device(
                co.hass, co.entry, missing
            )
        )
        co.last_update_success = True
        co.sleeping = True
        self.assertFalse(
            await integration.async_remove_config_entry_device(
                co.hass, co.entry, missing
            )
        )
        del co.entry.runtime_data
        self.assertFalse(
            await integration.async_remove_config_entry_device(
                co.hass, co.entry, missing
            )
        )
        self.assertEqual(
            device.inverter_device_info(co, "missing")["identifiers"],
            {("sma_bluetooth", "missing")},
        )

    async def test_diagnostics_redact_credentials_and_physical_identifiers(self):
        co = make_coordinator()
        co.entry.data.update(
            bt_address="AA:BB:CC:DD:EE:FF",
            password="SECRET",
            selected_serial="PRIVATE-SERIAL",
            plant_name="PRIVATE-NAME",
        )
        co.current_root = "PRIVATE-ROOT"
        co.adapter_gate.active_address = "PRIVATE-ACTIVE"
        co.data = {
            "PRIVATE-SERIAL": SMAInverter(
                serial="PRIVATE-SERIAL", bluetooth_address="PRIVATE-BT"
            )
        }
        co.update_interval = None
        with patch.object(co, "is_daylight", return_value=True):
            result = await diagnostics.async_get_config_entry_diagnostics(
                co.hass, co.entry
            )
        serialized = json.dumps(result)
        for private in [
            "SECRET",
            "AA:BB:CC:DD:EE:FF",
            "PRIVATE-SERIAL",
            "PRIVATE-NAME",
            "PRIVATE-ROOT",
            "PRIVATE-ACTIVE",
            "PRIVATE-BT",
        ]:
            self.assertNotIn(private, serialized)
        self.assertIn("inverter_1", result["inverters"])

    def test_all_entities_have_localized_names_and_icons_or_device_classes(self):
        co = make_coordinator()
        entities = [
            sensor.SMASensor(co, "1", description)
            for description in sensor.DESCRIPTIONS
        ]
        entities += [
            cls(co, co.entry)
            for cls in [
                sensor.SMAConnectionModeSensor,
                sensor.SMANetIDSensor,
                sensor.SMACurrentRootSensor,
                sensor.SMAPlantClockDifferenceSensor,
            ]
        ]
        entities += [
            cls(co, "1")
            for cls in [
                sensor.SMAInverterRoleSensor,
                sensor.SMAInverterRecordTimestampSensor,
                sensor.SMAInverterClockDifferenceSensor,
                event.SMAInverterEvent,
            ]
        ]
        icons = json.loads((COMPONENT / "icons.json").read_text())["entity"]
        for language in ("en", "de"):
            translations = json.loads(
                (COMPONENT / f"translations/{language}.json").read_text()
            )
            for entity in entities:
                platform = (
                    "event" if isinstance(entity, event.SMAInverterEvent) else "sensor"
                )
                key = entity.translation_key
                self.assertTrue(translations["entity"][platform][key]["name"])
                self.assertTrue(entity.device_class or key in icons[platform])
                if entity.entity_category is not None:
                    self.assertFalse(entity.entity_registry_enabled_default)
        # Percent signal must not claim a dBm device class.
        signal = next(e for e in entities if e.translation_key == "bt_signal")
        self.assertIsNone(signal.device_class)
        self.assertFalse(signal.entity_registry_enabled_default)

    def test_user_visible_exception_keys_are_translatable(self):
        import ast

        keys = set()
        for path in COMPONENT.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(node, ast.Raise)
                    and isinstance(node.exc, ast.Call)
                    and isinstance(node.exc.func, ast.Name)
                    and node.exc.func.id
                    in {
                        "HomeAssistantError",
                        "ServiceValidationError",
                        "ConfigEntryError",
                        "ConfigEntryAuthFailed",
                        "UpdateFailed",
                    }
                ):
                    kwargs = {kw.arg: kw.value for kw in node.exc.keywords}
                    self.assertIn("translation_domain", kwargs, (path, node.lineno))
                    keys.add(ast.literal_eval(kwargs["translation_key"]))
        self.assertTrue(keys)
        for language in ("en", "de"):
            translations = json.loads(
                (COMPONENT / f"translations/{language}.json").read_text()
            )["exceptions"]
            self.assertFalse(keys - translations.keys())
