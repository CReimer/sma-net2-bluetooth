"""Coordinator metadata guards, entity state and diagnostic contracts."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from homeassistant.helpers.update_coordinator import UpdateFailed
from custom_components.sma_bluetooth import (
    coordinator as c,
    sensor as s,
    event as e,
    diagnostics,
    device,
)
from custom_components.sma_bluetooth.const import (
    CONF_NET_ID,
    CONF_SELECTED_SERIAL,
    DOMAIN,
    EFFECTIVE_MODE_SINGLE,
    NETWORK_ROLE_DIRECT,
    NETWORK_ROLE_PARTICIPANT,
    NETWORK_ROLE_ROOT,
)
from custom_components.sma_bluetooth.models import SMAInverter
from custom_components.sma_bluetooth.protocol import (
    SMAProtocolError,
    SMANetworkModeError,
)
from custom_components.sma_bluetooth.gateway import SMADaylightError


def make_coordinator():
    entry = NS(
        entry_id="entry",
        title="Plant",
        data={"bt_address": "AA", "password": "p"},
        options={},
    )

    def update(entry, **changes):
        for key, value in changes.items():
            setattr(entry, key, value)

    hass = NS(data={}, config_entries=NS(async_update_entry=Mock(side_effect=update)))
    with patch.object(c.DataUpdateCoordinator, "__init__", return_value=None):
        coordinator = c.SMABluetoothCoordinator(hass, entry)
    coordinator.hass = hass
    coordinator.config_entry = entry
    coordinator.data = {
        "1": SMAInverter(
            serial="1",
            values={"ac_power_total": 100, "status": "OK", "relay_status": "Open"},
        )
    }
    coordinator.owned_serials = {"1"}
    coordinator.last_update_success = True
    coordinator.update_interval = timedelta(seconds=60)
    return coordinator


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def test_deserialization_rejects_invalid_shapes(self):
        self.assertEqual(c.deserialize_known_inverters("bad"), {})
        self.assertEqual(c.deserialize_known_inverters([None, {}, {"serial": 1}]), {})
        restored = c.deserialize_known_inverters([{"serial": "1", "value_keys": "bad"}])
        self.assertEqual(restored["1"].values, {})

    async def test_update_day_night_and_failures(self):
        co = make_coordinator()
        day, night = (
            NS(active=True, next_interval=timedelta(seconds=60)),
            NS(active=False, next_interval=timedelta(hours=5)),
        )
        for failure in (
            SMADaylightError("sunset"),
            SMANetworkModeError(1),
            SMAProtocolError("wire"),
        ):
            with (
                patch.object(c, "daylight_schedule", return_value=day),
                patch.object(co, "async_run_session", AsyncMock(side_effect=failure)),
                patch.object(c, "async_note_netid_change") as note,
            ):
                if isinstance(failure, SMADaylightError):
                    self.assertEqual(await co._async_update_data(), co.data)
                    self.assertTrue(co.sleeping)
                else:
                    with self.assertRaises(UpdateFailed):
                        await co._async_update_data()
                    self.assertEqual(
                        note.call_count, int(isinstance(failure, SMANetworkModeError))
                    )
        client = NS(async_query_active=AsyncMock(return_value=co.data))

        async def run(operation, **kwargs):
            return await operation(client)

        with (
            patch.object(c, "daylight_schedule", return_value=day),
            patch.object(co, "async_run_session", run),
            patch.object(c, "async_reconcile_ownership", return_value={"1"}),
        ):
            self.assertEqual(await co._async_update_data(), co.data)
            self.assertFalse(co.sleeping)
            self.assertTrue(co.is_daylight())
        with (
            patch.object(c, "daylight_schedule", return_value=night),
            patch.object(co, "async_set_updated_data") as set_data,
        ):
            await co.async_enter_night()
            set_data.assert_called_once_with(co.data)
            self.assertFalse(co.is_daylight())
        with (
            patch.object(c, "daylight_schedule", return_value=day),
            patch.object(co, "async_set_updated_data") as set_data,
        ):
            await co.async_enter_night()
            set_data.assert_not_called()
        co._remember_inverters(co.data)
        self.assertEqual(co.hass.config_entries.async_update_entry.call_count, 1)
        co.adapter_gate.active_address = co.address
        self.assertTrue(co.connection_open)
        co.adapter_gate.active_address = None
        self.assertFalse(co.connection_open)

    def test_session_metadata_identity_and_topology_guards(self):
        co = make_coordinator()
        client = NS(net_id=None, effective_mode=None)
        with self.assertRaises(SMAProtocolError):
            co._apply_session_metadata(client)
        client = NS(
            net_id=1,
            effective_mode=EFFECTIVE_MODE_SINGLE,
            current_root="1",
            root_address=b"a",
            devices=[NS(serial=1, address=b"a")],
            format_bluetooth_address=lambda address: address.decode(),
        )
        co.data["1"].bluetooth_address = "a"
        with (
            patch.object(c, "async_clear_netid_issues") as clear,
            patch.object(c, "async_note_netid_change") as note,
        ):
            co._apply_session_metadata(client)
            self.assertEqual(co.entry.data[CONF_NET_ID], 1)
            self.assertEqual(co.entry.data[CONF_SELECTED_SERIAL], "1")
            self.assertEqual(co.data["1"].network_role, NETWORK_ROLE_DIRECT)
            count = co.hass.config_entries.async_update_entry.call_count
            co._apply_session_metadata(client)
            self.assertEqual(
                co.hass.config_entries.async_update_entry.call_count, count
            )
            client.devices = [NS(serial=2, address=b"b")]
            with self.assertRaisesRegex(SMAProtocolError, "serial"):
                co._apply_session_metadata(client)
            client.net_id = 2
            with self.assertRaises(c.SMANetIDChangedError):
                co._apply_session_metadata(client)
            note.assert_called_once()
            co.configured_net_id = 2
            co.selected_serial = None
            client.effective_mode = "network"
            client.devices.append(NS(serial=1, address=b"a"))
            co._apply_session_metadata(client)
            self.assertEqual(co.data["1"].network_role, NETWORK_ROLE_ROOT)
            client.root_address = b"b"
            co._apply_session_metadata(client)
            self.assertEqual(co.data["1"].network_role, NETWORK_ROLE_PARTICIPANT)
            co.data = {}
            co._apply_session_metadata(client)
            self.assertGreater(clear.call_count, 1)

    async def test_session_validates_before_and_after_and_wrappers_notify(self):
        co = make_coordinator()
        client = NS(
            async_read_archive_active=AsyncMock(return_value={"1": []}),
            async_sync_clock_active=AsyncMock(return_value="clock"),
        )

        async def gate(hass, address, password, mode, operation, **kwargs):
            return await operation(client)

        with (
            patch.object(co.adapter_gate, "async_run", gate),
            patch.object(co, "_apply_session_metadata") as metadata,
            patch.object(co, "async_update_listeners") as notify,
        ):
            self.assertEqual(await co.async_read_archive([(0, 300)]), {"1": []})
            self.assertEqual(await co.async_sync_clock(3600, True), "clock")
            client.async_sync_clock_active.assert_awaited_once_with(3600, True)
            self.assertEqual(metadata.call_count, 4)
            self.assertEqual(notify.call_count, 2)
            await co.async_disconnect()


class PlatformTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_only_exposes_owned_inverters_and_state(self):
        co = make_coordinator()
        # No hass on the stand-in uses the stable identifier fallback in DeviceInfo.
        hass = co.hass
        del co.hass
        co.data["other"] = SMAInverter(serial="other")
        hass.data = {DOMAIN: {"entry": co}}
        entities = []
        await s.async_setup_entry(hass, co.entry, entities.extend)
        self.assertEqual(len(entities), 10)
        self.assertFalse(any("other" in entity.unique_id for entity in entities))
        power = next(
            entity
            for entity in entities
            if entity.unique_id.endswith("_ac_power_total")
        )
        self.assertEqual(power.native_value, 100)
        self.assertTrue(power.available)
        self.assertIn("via_device", power.device_info)
        co.sleeping = True
        self.assertFalse(power.available)
        for entity in entities:
            self.assertIn("identifiers", entity.device_info)
        mode, netid, root, clock = entities[:4]
        self.assertEqual(mode.native_value, "auto")
        co.effective_mode = "single"
        self.assertEqual(mode.native_value, "single")
        self.assertEqual(mode.extra_state_attributes, {"configured_mode": "auto"})
        self.assertIsNone(netid.native_value)
        self.assertIsNone(netid.extra_state_attributes)
        co.net_id = 10
        self.assertEqual(netid.extra_state_attributes, {"hex": "A"})
        co.current_root = "1"
        self.assertEqual(root.native_value, "1")
        for value in (None, True, "5", 5, 1.5):
            co.entry.options["last_clock_difference_seconds"] = value
            expected = isinstance(value, (int, float)) and not isinstance(value, bool)
            self.assertEqual(clock.available, expected)
            self.assertEqual(clock.native_value, value if expected else None)
        self.assertIn("checked_at", clock.extra_state_attributes)
        timestamp = s.SMAInverterRecordTimestampSensor(co, "1")
        self.assertIsNone(timestamp.native_value)
        self.assertIsNone(timestamp.extra_state_attributes)
        co.data["1"].record_timestamp = 300
        co.data["1"].record_received_at = 310
        self.assertEqual(timestamp.native_value, datetime.fromtimestamp(300, UTC))
        self.assertEqual(
            timestamp.extra_state_attributes["received_at"],
            datetime.fromtimestamp(310, UTC).isoformat(),
        )
        co.data.clear()
        self.assertIsNone(power.native_value)
        self.assertIsNone(timestamp.native_value)
        self.assertIsNone(timestamp.extra_state_attributes)
        self.assertIsNone(s.SMAInverterRoleSensor(co, "1").native_value)
        self.assertIsNone(s.SMAInverterClockDifferenceSensor(co, "1").native_value)

    async def test_events_only_fire_on_meaningful_transitions(self):
        co = make_coordinator()
        del co.hass
        entities = []
        await e.async_setup_entry(
            NS(data={DOMAIN: {"entry": co}}), co.entry, entities.extend
        )
        entity = entities[0]
        self.assertIn("via_device", entity.device_info)
        self.assertTrue(entity.available)
        co.sleeping = True
        self.assertFalse(entity.available)
        with (
            patch.object(entity, "async_write_ha_state"),
            patch.object(
                entity, "_trigger_event", wraps=entity._trigger_event
            ) as trigger,
        ):
            for status, relay, expected in [
                ("OK", "Open", None),
                ("Warning", "Open", "warning"),
                ("Fault", "Open", "fault"),
                ("Error", "Open", "fault"),
                ("OK", "Closed", "grid_connected"),
                ("OK", "Open", "grid_disconnected"),
                ("OK", "Unknown", None),
            ]:
                trigger.reset_mock()
                co.data["1"].values.update(status=status, relay_status=relay)
                entity._handle_coordinator_update()
                if expected:
                    self.assertEqual(trigger.call_args.args[0], expected)
                else:
                    trigger.assert_not_called()
            co.data.clear()
            entity._handle_coordinator_update()

    async def test_diagnostics_redaction_and_hub_creation(self):
        co = make_coordinator()
        co.hass.data[DOMAIN]["entry"] = co
        with patch.object(co, "is_daylight", return_value=True):
            for recovered in (None, datetime(2026, 1, 1, tzinfo=UTC)):
                co.adapter_gate.last_recovery_at = recovered
                result = await diagnostics.async_get_config_entry_diagnostics(
                    co.hass, co.entry
                )
                self.assertNotEqual(result["config_entry"]["password"], "p")
                self.assertEqual(result["inverters"]["1"]["entity_owner"], True)
                self.assertEqual(
                    result["connection"]["last_adapter_recovery_at"],
                    recovered.isoformat() if recovered else None,
                )
                co.update_interval = None
        registry = Mock()
        with patch.object(device.dr, "async_get", return_value=registry):
            device.async_ensure_hub_device(co.hass, co.entry)
        self.assertEqual(
            registry.async_get_or_create.call_args.kwargs["config_entry_id"], "entry"
        )
