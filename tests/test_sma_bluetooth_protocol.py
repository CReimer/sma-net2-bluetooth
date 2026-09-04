"""Tests for SMA Bluetooth protocol and archive reconciliation helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorStateClass

import custom_components.sma_bluetooth as integration_module
from custom_components.sma_bluetooth import coordinator as coordinator_module
from custom_components.sma_bluetooth import config_flow as config_flow_module
from custom_components.sma_bluetooth import daylight as daylight_module
from custom_components.sma_bluetooth import gateway as gateway_module
from custom_components.sma_bluetooth import ownership as ownership_module
from custom_components.sma_bluetooth import protocol as protocol_module
from custom_components.sma_bluetooth.archive import (
    completed_day_periods,
    cumulative_statistic_sums,
    hourly_last_values,
    timestamp_series_complete,
)
from custom_components.sma_bluetooth.models import SMAInverter
from custom_components.sma_bluetooth.const import (
    CONF_CONNECTION_MODE,
    CONF_KNOWN_INVERTERS,
    CONF_LAST_DETECTED_NET_ID,
    CONF_NET_ID,
    CONNECTION_MODE_AUTO,
    CONNECTION_MODE_NETWORK,
    CONNECTION_MODE_SINGLE,
    EFFECTIVE_MODE_NETWORK,
    EFFECTIVE_MODE_SINGLE,
)
from custom_components.sma_bluetooth.device import (
    hub_identifier,
    inverter_device_info,
)
from custom_components.sma_bluetooth.gateway import (
    SMAAdapterGate,
    SMADaylightError,
)
from custom_components.sma_bluetooth.config_flow import SMABluetoothConfigFlow
from custom_components.sma_bluetooth.daylight import (
    DaylightSchedule,
    POLAR_NIGHT_RECHECK_INTERVAL,
    daylight_schedule,
)
from custom_components.sma_bluetooth.protocol import (
    L2_SIGNATURE,
    SMAArchivePoint,
    SMAClassicClient,
    SMAClockInfo,
    SMAProtocolError,
    SMATransportError,
    SMANetworkModeError,
    _Device,
    _escape,
    _fcs,
    _unescape,
)
from custom_components.sma_bluetooth.sensor import (
    DESCRIPTIONS,
    SMAInverterClockDifferenceSensor,
    SMAInverterRecordTimestampSensor,
    SMAPlantClockDifferenceSensor,
)


class SMAProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Exercise pure protocol parsing and framing."""

    def test_ppp_fcs_known_vector(self) -> None:
        self.assertEqual(_fcs(b"123456789"), 0x906E)

    def test_escape_roundtrip(self) -> None:
        source = b"\x11\x12\x13\x7d\x7e\x00"
        self.assertEqual(_unescape(_escape(source)), source)

    def test_l2_packet_has_valid_signature_and_checksum(self) -> None:
        client = SMAClassicClient("02:00:00:00:00:01", "0000")
        packet = client._l2_payload(9, 0xA0, 0, 0xFFFF, 0xFFFFFFFF, b"payload")
        decoded = _unescape(packet)
        self.assertEqual(struct.unpack_from("<I", decoded, 1)[0], L2_SIGNATURE)
        self.assertEqual(
            _fcs(decoded[1:-3]),
            struct.unpack_from("<H", decoded, len(decoded) - 3)[0],
        )

    def test_parse_energy_and_power_records(self) -> None:
        client = SMAClassicClient("02:00:00:00:00:01", "0000")
        inverter = SMAInverter(serial="1000000001", susy_id="131")
        device = _Device(b"\x01\x02\x03\x04\x05\x06", 1000000001, 131, inverter)

        records = bytearray()
        records.extend(struct.pack("<IIQ", 0x00260100, 0, 43_478_332))
        records.extend(struct.pack("<IIQ", 0x00262200, 0, 12_506))
        packet = bytearray(41)
        packet[5] = 17  # (17 - 9) * 4 / 2 = 16 bytes per record
        struct.pack_into("<II", packet, 33, 0, 1)
        packet.extend(records)
        packet.extend(b"\0\0\x7e")

        client._parse_records(device, bytes(packet))
        self.assertEqual(inverter.values["energy_total"], 43478.332)
        self.assertEqual(inverter.values["energy_today"], 12.506)

    def test_nan_measurement_remains_unknown(self) -> None:
        client = SMAClassicClient("02:00:00:00:00:01", "0000")
        inverter = SMAInverter(serial="1000000001", susy_id="131")
        device = _Device(b"\x01\x02\x03\x04\x05\x06", 1000000001, 131, inverter)
        record = bytearray(28)
        struct.pack_into("<I", record, 0, 0x40237700)
        struct.pack_into("<I", record, 16, 0x80000000)
        packet = bytearray(41)
        packet[5] = 16  # (16 - 9) * 4 = 28 bytes
        struct.pack_into("<II", packet, 33, 0, 0)
        packet.extend(record)
        packet.extend(b"\0\0\x7e")

        client._parse_records(device, bytes(packet))

        self.assertIsNone(inverter.values["temperature"])

    def test_clock_metadata_keeps_timezone_and_dst_separate(self) -> None:
        clock = SMAClockInfo(1_786_000_000, 1_785_900_000, 3600, True, 42)

        self.assertEqual(clock.timezone_offset, 3600)
        self.assertIs(clock.dst_active, True)
        self.assertEqual(clock.set_count, 42)

    async def test_clock_sync_keeps_existing_safety_limits(self) -> None:
        now = 1_786_000_000
        client = SMAClassicClient("02:00:00:00:00:01", "0000")
        client._session_active = True
        client._set_clock = AsyncMock()

        for difference, reason in (
            (60, "within_tolerance"),
            (3600, "unsafe_difference"),
        ):
            with self.subTest(difference=difference):
                client._read_clock = AsyncMock(
                    return_value=SMAClockInfo(now + difference, 0, 3600, True, 42)
                )
                with patch.object(protocol_module.time, "time", return_value=now):
                    result = await client.async_sync_clock_active(3600, True)

                self.assertEqual(result.reason, reason)
                self.assertFalse(result.adjusted)

        client._set_clock.assert_not_awaited()

    async def test_clock_sync_still_corrects_a_safe_difference(self) -> None:
        now = 1_786_000_000
        before = SMAClockInfo(now + 120, now - 90_000, 3600, True, 42)
        after = SMAClockInfo(now, now, 3600, True, 43)
        client = SMAClassicClient("02:00:00:00:00:01", "0000")
        client._session_active = True
        client._read_clock = AsyncMock(side_effect=(before, after))
        client._set_clock = AsyncMock()

        with (
            patch.object(protocol_module.time, "time", return_value=now),
            patch.object(protocol_module.asyncio, "sleep", AsyncMock()),
        ):
            result = await client.async_sync_clock_active(3600, True)

        self.assertTrue(result.adjusted)
        self.assertEqual(result.difference_seconds, 120)
        client._set_clock.assert_awaited_once_with(now, 3600, True, 43)

    async def test_auto_uses_direct_mode_for_netid_1(self) -> None:
        client = SMAClassicClient(
            "02:00:00:00:00:01", "0000", connection_mode=CONNECTION_MODE_AUTO
        )
        announcement = bytearray(23)
        announcement[19] = 4
        announcement[22] = 1
        client._send = AsyncMock()
        client._receive_packet = AsyncMock(
            return_value=(bytes(announcement), client.connection_address)
        )
        client._initialize_single = AsyncMock()
        client._initialize_network = AsyncMock()

        await client._initialize()

        self.assertEqual(client.net_id, 1)
        self.assertEqual(client.effective_mode, EFFECTIVE_MODE_SINGLE)
        client._initialize_single.assert_awaited_once_with(1)
        client._initialize_network.assert_not_awaited()

    async def test_auto_uses_full_network_for_netid_2_to_f(self) -> None:
        client = SMAClassicClient(
            "02:00:00:00:00:01", "0000", connection_mode=CONNECTION_MODE_AUTO
        )
        announcement = bytearray(23)
        announcement[19] = 4
        announcement[22] = 0x0F
        client._send = AsyncMock()
        client._receive_packet = AsyncMock(
            return_value=(bytes(announcement), client.connection_address)
        )
        client._initialize_single = AsyncMock()
        client._initialize_network = AsyncMock()

        await client._initialize()

        self.assertEqual(client.net_id, 0x0F)
        self.assertEqual(client.effective_mode, EFFECTIVE_MODE_NETWORK)
        client._initialize_network.assert_awaited_once_with(0x0F)
        client._initialize_single.assert_not_awaited()

    async def test_explicit_single_skips_multiple_inverter_search(self) -> None:
        client = SMAClassicClient(
            "02:00:00:00:00:01", "0000", connection_mode=CONNECTION_MODE_SINGLE
        )
        client._send = AsyncMock()
        direct_reply = bytearray(32)
        direct_reply[26:32] = b"\x01\x02\x03\x04\x05\x06"
        client._receive_packet = AsyncMock(
            return_value=(bytes(direct_reply), client.connection_address)
        )
        client._identify_devices = AsyncMock()
        client._build_network = AsyncMock()

        await client._initialize_single(2)

        self.assertEqual(len(client.devices), 1)
        self.assertEqual(client.devices[0].address, client.connection_address)
        client._identify_devices.assert_awaited_once()
        client._build_network.assert_not_awaited()

    async def test_full_network_rejects_netid_1_clearly(self) -> None:
        client = SMAClassicClient(
            "02:00:00:00:00:01", "0000", connection_mode=CONNECTION_MODE_NETWORK
        )

        with self.assertRaisesRegex(SMANetworkModeError, "NetID 2-F"):
            await client._initialize_network(1)

    async def test_unidentified_repeater_root_does_not_mark_an_inverter(self) -> None:
        client = SMAClassicClient("02:00:00:00:00:01", "0000")
        first = _Device(b"\x01\x02\x03\x04\x05\x06", 1, 131)
        second = _Device(b"\x06\x05\x04\x03\x02\x01", 2, 131)
        client.devices = [first, second]
        client._session_active = True
        client.effective_mode = EFFECTIVE_MODE_NETWORK
        client.root_address = b"\x03\x00\x00\x00\x00\x02"
        client.root_serial = None
        client._signals = {first.address: 25.0, second.address: 75.0}
        client._query = AsyncMock()

        result = await client.async_query_active()

        self.assertEqual(
            {inverter.network_role for inverter in result.values()},
            {"participant"},
        )
        self.assertEqual(client.current_root, "02:00:00:00:00:03")

    def test_decreasing_value_and_timestamp_remain_visible(self) -> None:
        client = SMAClassicClient("02:00:00:00:00:01", "0000")
        inverter = SMAInverter(serial="123", susy_id="131")
        device = _Device(b"\x01\x02\x03\x04\x05\x06", 123, 131, inverter)

        def packet(timestamp: int, total_wh: int) -> bytes:
            record = struct.pack("<IIQ", 0x00260100, timestamp, total_wh)
            data = bytearray(41)
            data[5] = 13
            struct.pack_into("<II", data, 33, 0, 0)
            data.extend(record)
            data.extend(b"\0\0\x7e")
            return bytes(data)

        with patch.object(protocol_module.time, "time", side_effect=(205.5, 105.5)):
            client._parse_records(device, packet(200, 50_000))
            client._parse_records(device, packet(100, 40_000))

        self.assertEqual(inverter.values["energy_total"], 40.0)
        self.assertEqual(inverter.record_timestamp, 100)
        self.assertEqual(inverter.record_received_at, 105.5)
        self.assertEqual(inverter.record_clock_difference, -5.5)

    async def test_archive_keeps_backwards_source_point_in_transfer_order(self) -> None:
        client = SMAClassicClient("02:00:00:00:00:01", "0000")
        device = _Device(b"\x01\x02\x03\x04\x05\x06", 123, 131)
        client._send = AsyncMock()

        async def receive(_command: int, _sender: bytes):
            packet = bytearray(68)
            struct.pack_into("<H", packet, 27, client.packet_id)
            struct.pack_into("<IQ", packet, 41, 1500, 50_000)
            struct.pack_into("<IQ", packet, 53, 1200, 40_000)
            struct.pack_into("<H", packet, 25, 0)
            return bytes(packet), device.address

        client._receive_packet = AsyncMock(side_effect=receive)

        points = await client._archive_day(device, 600, 1800)

        self.assertEqual([point.timestamp for point in points], [1500, 1200])
        self.assertEqual([point.total_energy_kwh for point in points], [50.0, 40.0])


class SMAArchiveTests(unittest.TestCase):
    """Exercise completeness and Recorder continuity rules."""

    def test_points_are_reduced_to_last_official_value_per_hour(self) -> None:
        start = 1_786_046_400
        points = [
            SMAArchivePoint(start + step * 300, 1000 + step / 1000, None)
            for step in range(288)
        ]

        hourly = hourly_last_values(points)

        self.assertEqual(len(hourly), 24)
        self.assertEqual(list(hourly.values())[0], 1000.011)
        self.assertEqual(list(hourly.values())[-1], 1000.287)

    def test_completed_day_periods_follow_local_dst_boundaries(self) -> None:
        local_today = datetime(2026, 10, 26, tzinfo=ZoneInfo("Europe/Berlin"))

        [(start, end)] = completed_day_periods(local_today, 1)

        self.assertEqual(end - start, 25 * 3600)

    def test_completed_archive_requires_every_five_minute_slot(self) -> None:
        start = 1_000_000
        end = start + 3600
        complete = set(range(start, end, 300))
        common_gap = complete - {start + 900}

        self.assertTrue(
            timestamp_series_complete(
                [complete, complete], start, end, require_all_slots=True
            )
        )
        self.assertFalse(
            timestamp_series_complete(
                [common_gap, common_gap], start, end, require_all_slots=True
            )
        )
        self.assertTrue(
            timestamp_series_complete(
                [common_gap, common_gap], start, end, require_all_slots=False
            )
        )
        self.assertFalse(
            timestamp_series_complete(
                [complete, common_gap], start, end, require_all_slots=False
            )
        )

    def test_statistic_sums_continue_across_range_and_meter_reset(self) -> None:
        sums = cumulative_statistic_sums(
            [102.0, 105.0, 1.0, 4.0], previous=(100.0, 10.0)
        )

        self.assertEqual(sums, [12.0, 15.0, 16.0, 19.0])

    def test_statistic_sums_can_anchor_at_following_history(self) -> None:
        sums = cumulative_statistic_sums([100.0, 103.0], following=(105.0, 50.0))

        self.assertEqual(sums, [45.0, 48.0])


class SMADaylightTests(unittest.TestCase):
    """Exercise the independent Home Assistant daylight schedule."""

    def test_daylight_allows_communication(self) -> None:
        with patch.object(daylight_module.sun, "is_up", return_value=True):
            schedule = daylight_schedule(
                SimpleNamespace(), datetime(2026, 8, 8, 12, tzinfo=UTC)
            )

        self.assertTrue(schedule.active)
        self.assertIsNone(schedule.next_interval)

    def test_night_sleeps_until_next_sunrise(self) -> None:
        now = datetime(2026, 8, 8, 22, tzinfo=UTC)
        sunrise = now + timedelta(hours=7, minutes=15)
        with (
            patch.object(daylight_module.sun, "is_up", return_value=False),
            patch.object(
                daylight_module.sun,
                "get_astral_event_next",
                return_value=sunrise,
            ),
        ):
            schedule = daylight_schedule(SimpleNamespace(), now)

        self.assertFalse(schedule.active)
        self.assertEqual(schedule.next_interval, sunrise - now)

    def test_polar_night_rechecks_without_communicating(self) -> None:
        with (
            patch.object(daylight_module.sun, "is_up", return_value=False),
            patch.object(
                daylight_module.sun,
                "get_astral_event_next",
                side_effect=ValueError,
            ),
        ):
            schedule = daylight_schedule(
                SimpleNamespace(), datetime(2026, 12, 21, 12, tzinfo=UTC)
            )

        self.assertFalse(schedule.active)
        self.assertEqual(schedule.next_interval, POLAR_NIGHT_RECHECK_INTERVAL)

    def test_known_inverters_restore_entities_without_stale_values(self) -> None:
        source = {
            "123": SMAInverter(
                serial="123",
                susy_id="131",
                name="Roof",
                model="Sunny Boy",
                software_version="1.2.3",
                values={"energy_total": 42.0, "ac_power_total": 900.0},
            )
        }

        restored = coordinator_module.deserialize_known_inverters(
            coordinator_module.serialize_known_inverters(source)
        )

        self.assertEqual(restored["123"].name, "Roof")
        self.assertEqual(restored["123"].model, "Sunny Boy")
        self.assertEqual(
            restored["123"].values,
            {"ac_power_total": None, "energy_total": None},
        )


class SMASensorSemanticsTests(unittest.IsolatedAsyncioTestCase):
    """Describe source counters without inventing monotonic behavior."""

    def test_operating_time_allows_source_rollbacks(self) -> None:
        operating_time = next(
            description
            for description in DESCRIPTIONS
            if description.key == "operation_time"
        )

        self.assertEqual(operating_time.state_class, SensorStateClass.TOTAL)

    def test_clock_sensor_exposes_recorded_difference_without_new_query(self) -> None:
        entity = SMAPlantClockDifferenceSensor.__new__(SMAPlantClockDifferenceSensor)
        entity._entry = SimpleNamespace(
            options={
                "last_clock_difference_seconds": -75,
                "last_clock_check_at": "2026-08-09T05:42:00+00:00",
                "last_clock_sync_at": "2026-08-09T05:42:00+00:00",
                "last_clock_result": "adjusted",
            }
        )

        self.assertEqual(entity.native_value, -75)
        self.assertEqual(
            entity.extra_state_attributes,
            {
                "checked_at": "2026-08-09T05:42:00+00:00",
                "last_synchronized_at": "2026-08-09T05:42:00+00:00",
                "result": "adjusted",
            },
        )

    def test_clock_sensor_is_attached_to_stable_hub_without_unique_id_change(
        self,
    ) -> None:
        entity = SMAPlantClockDifferenceSensor.__new__(SMAPlantClockDifferenceSensor)
        entity._entry = SimpleNamespace(
            entry_id="entry-1",
            title="Roof",
            data={"plant_name": "Roof"},
            options={},
        )
        entity._attr_unique_id = "sma_bluetooth_entry-1_plant_clock_difference"

        self.assertEqual(
            entity.unique_id, "sma_bluetooth_entry-1_plant_clock_difference"
        )
        self.assertEqual(
            entity.device_info["identifiers"], {hub_identifier(entity._entry)}
        )

    def test_root_change_does_not_change_inverter_via_device(self) -> None:
        entry = SimpleNamespace(
            entry_id="entry-1", title="Roof", data={"plant_name": "Roof"}
        )
        inverter = SMAInverter(serial="123", network_role="root_node")
        coordinator = SimpleNamespace(entry=entry, data={"123": inverter})

        before = inverter_device_info(coordinator, "123")
        inverter.network_role = "participant"
        after = inverter_device_info(coordinator, "123")

        self.assertEqual(before["via_device"], hub_identifier(entry))
        self.assertEqual(after["via_device"], hub_identifier(entry))
        self.assertEqual(before["identifiers"], after["identifiers"])

    def test_per_inverter_time_diagnostics_use_matching_receipt(self) -> None:
        inverter = SMAInverter(
            serial="123", record_timestamp=100, record_received_at=105.5
        )
        coordinator = SimpleNamespace(data={"123": inverter})
        timestamp = SMAInverterRecordTimestampSensor.__new__(
            SMAInverterRecordTimestampSensor
        )
        timestamp.coordinator = coordinator
        timestamp._serial = "123"
        difference = SMAInverterClockDifferenceSensor.__new__(
            SMAInverterClockDifferenceSensor
        )
        difference.coordinator = coordinator
        difference._serial = "123"

        self.assertEqual(timestamp.native_value.timestamp(), 100)
        self.assertEqual(difference.native_value, -5.5)

    async def test_coordinator_returns_decreasing_source_value_unchanged(self) -> None:
        coordinator = coordinator_module.SMABluetoothCoordinator.__new__(
            coordinator_module.SMABluetoothCoordinator
        )
        coordinator.hass = SimpleNamespace()
        coordinator.entry = SimpleNamespace()
        coordinator.daytime_interval = timedelta(seconds=60)
        coordinator.async_run_session = AsyncMock(
            return_value={
                "123": SMAInverter(serial="123", values={"operation_time": 99.5})
            }
        )
        coordinator._remember_inverters = MagicMock()

        with (
            patch.object(
                coordinator_module,
                "daylight_schedule",
                return_value=SimpleNamespace(active=True),
            ),
            patch.object(
                coordinator_module,
                "async_reconcile_ownership",
                return_value={"123"},
            ),
        ):
            data = await coordinator._async_update_data()

        self.assertEqual(data["123"].values["operation_time"], 99.5)


class SMAConfigMigrationOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """Protect config-entry migration and serial registry continuity."""

    async def test_v1_two_inverter_entry_migrates_to_network_mode(self) -> None:
        entry = SimpleNamespace(
            version=1,
            entry_id="legacy",
            data={
                "bt_address": "02:00:00:00:00:01",
                CONF_KNOWN_INVERTERS: [
                    {"serial": "1", "value_keys": ["energy_total"]},
                    {"serial": "2", "value_keys": ["energy_total"]},
                ],
            },
            options={},
        )
        update = MagicMock()
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=update)
        )

        migrated = await integration_module.async_migrate_entry(hass, entry)

        self.assertTrue(migrated)
        kwargs = update.call_args.kwargs
        self.assertEqual(kwargs["version"], 2)
        self.assertEqual(kwargs["data"][CONF_CONNECTION_MODE], CONNECTION_MODE_NETWORK)

    async def test_v1_single_inverter_entry_migrates_to_auto_mode(self) -> None:
        entry = SimpleNamespace(
            version=1,
            entry_id="legacy",
            data={CONF_KNOWN_INVERTERS: [{"serial": "1"}]},
            options={},
        )
        update = MagicMock()
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=update)
        )

        await integration_module.async_migrate_entry(hass, entry)

        self.assertEqual(
            update.call_args.kwargs["data"][CONF_CONNECTION_MODE],
            CONNECTION_MODE_AUTO,
        )

    def test_registry_owner_transfer_keeps_entity_id_and_unique_id(self) -> None:
        old = SimpleNamespace(
            entry_id="old",
            title="Old",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            data={CONF_KNOWN_INVERTERS: []},
            options={},
        )
        new = SimpleNamespace(
            entry_id="new",
            title="New",
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
            data={CONF_KNOWN_INVERTERS: [{"serial": "123"}]},
            options={},
        )
        entity = SimpleNamespace(
            entity_id="sensor.pv_ertrag_gesamt",
            platform="sma_bluetooth",
            unique_id="sma_bluetooth_123_energy_total",
            config_entry_id="old",
        )
        entity_registry = SimpleNamespace(
            entities={entity.entity_id: entity}, async_update_entity=MagicMock()
        )
        device = SimpleNamespace(id="device-123", config_entry_id="old")
        device_registry = SimpleNamespace(
            async_get_device=MagicMock(return_value=device),
            async_update_device=MagicMock(),
        )
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda _domain: [old, new])
        )

        with (
            patch.object(
                ownership_module.er, "async_get", return_value=entity_registry
            ),
            patch.object(
                ownership_module.dr, "async_get", return_value=device_registry
            ),
            patch.object(ownership_module, "_sync_overlap_issues"),
        ):
            owned = ownership_module.async_reconcile_ownership(hass, new, {"123"})

        self.assertEqual(owned, {"123"})
        entity_registry.async_update_entity.assert_called_once_with(
            "sensor.pv_ertrag_gesamt", config_entry_id="new"
        )
        self.assertEqual(entity.entity_id, "sensor.pv_ertrag_gesamt")
        self.assertEqual(entity.unique_id, "sma_bluetooth_123_energy_total")
        device_registry.async_update_device.assert_called_once_with(
            "device-123", new_config_entry_id="new"
        )

    def test_overlap_keeps_existing_owner_and_creates_repair_path(self) -> None:
        old = SimpleNamespace(
            entry_id="old",
            title="Old",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            data={CONF_KNOWN_INVERTERS: [{"serial": "123"}]},
            options={},
        )
        new = SimpleNamespace(
            entry_id="new",
            title="New",
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
            data={CONF_KNOWN_INVERTERS: [{"serial": "123"}]},
            options={},
        )
        entity = SimpleNamespace(
            entity_id="sensor.pv",
            platform="sma_bluetooth",
            unique_id="sma_bluetooth_123_energy_total",
            config_entry_id="old",
        )
        registry = SimpleNamespace(entities={"sensor.pv": entity})
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda _domain: [old, new])
        )

        with (
            patch.object(ownership_module.er, "async_get", return_value=registry),
            patch.object(ownership_module, "_move_serial_registries") as move,
            patch.object(ownership_module, "_sync_overlap_issues") as issues,
        ):
            owned = ownership_module.async_reconcile_ownership(hass, new, {"123"})

        self.assertEqual(owned, set())
        move.assert_not_called()
        issues.assert_called_once_with(hass)

    def test_netid_rebuild_creates_guided_directional_repair(self) -> None:
        entry = SimpleNamespace(
            entry_id="entry-1",
            title="Roof",
            data={
                CONF_NET_ID: 2,
                CONF_CONNECTION_MODE: CONNECTION_MODE_AUTO,
            },
            options={},
        )
        update = MagicMock()
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=update)
        )

        with patch.object(ownership_module.ir, "async_create_issue") as create:
            ownership_module.async_note_netid_change(hass, entry, 1, {"1"})

        self.assertEqual(
            update.call_args.kwargs["options"][CONF_LAST_DETECTED_NET_ID], 1
        )
        self.assertEqual(
            create.call_args.kwargs["translation_key"], "network_to_single"
        )
        self.assertTrue(create.call_args.kwargs["is_fixable"])

    def test_guided_entry_removal_transfers_registry_before_cleanup(self) -> None:
        departing = SimpleNamespace(
            entry_id="remove",
            title="Roof second",
            data={"plant_name": "Roof", "password": "0000"},
            options={
                CONF_LAST_DETECTED_NET_ID: 2,
                CONF_KNOWN_INVERTERS: [{"serial": "2"}],
            },
        )
        owner = SimpleNamespace(
            entry_id="keep",
            title="Roof",
            data={"plant_name": "Roof", "password": "0000"},
            options={
                CONF_LAST_DETECTED_NET_ID: 2,
                CONF_KNOWN_INVERTERS: [{"serial": "1"}],
            },
        )
        update = MagicMock()
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entries=lambda _domain: [owner],
                async_update_entry=update,
            )
        )

        with patch.object(ownership_module, "_move_serial_registries") as move:
            target = ownership_module.async_transfer_departing_entry(hass, departing)

        self.assertEqual(target, "keep")
        move.assert_called_once_with(hass, "2", "keep")
        snapshot = update.call_args.kwargs["options"][CONF_KNOWN_INVERTERS]
        self.assertEqual([item["serial"] for item in snapshot], ["1", "2"])


class SMAConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    """Validate selected-device probing and topology confirmation."""

    async def test_probe_never_assumes_other_visible_devices_share_network(
        self,
    ) -> None:
        flow = SMABluetoothConfigFlow()
        flow.hass = SimpleNamespace(data={})
        flow._discovered = {
            "02:00:00:00:00:01": "SMA first",
            "02:00:00:00:00:02": "SMA unrelated",
        }
        selected = "02:00:00:00:00:02"

        async def run(
            _hass: object,
            address: str,
            _password: str,
            _mode: str,
            operation,
            **_kwargs: object,
        ):
            self.assertEqual(address, selected)
            client = SimpleNamespace(
                net_id=1,
                effective_mode=EFFECTIVE_MODE_SINGLE,
                current_root="123",
                async_query_active=AsyncMock(
                    return_value={"123": SMAInverter(serial="123")}
                ),
            )
            return await operation(client)

        gate = SimpleNamespace(async_run=AsyncMock(side_effect=run))
        with patch.object(
            config_flow_module, "async_get_adapter_gate", return_value=gate
        ):
            probe = await flow._async_probe(
                {
                    "bt_address": selected,
                    "password": "0000",
                    "connection_mode": CONNECTION_MODE_AUTO,
                }
            )

        self.assertEqual(probe.net_id, 1)
        self.assertEqual(set(probe.inverters), {"123"})
        gate.async_run.assert_awaited_once()

    async def test_validation_blocks_serial_claimed_by_another_entry(self) -> None:
        existing = SimpleNamespace(
            entry_id="existing",
            data={CONF_KNOWN_INVERTERS: [{"serial": "123"}]},
            options={},
        )
        flow = SMABluetoothConfigFlow()
        flow.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda _domain: [existing])
        )
        flow._async_probe = AsyncMock(
            return_value=config_flow_module._ProbeResult(
                inverters={"123": SMAInverter(serial="123")},
                net_id=2,
                effective_mode=EFFECTIVE_MODE_NETWORK,
                current_root="123",
            )
        )

        error = await flow._async_validate(
            {
                "bt_address": "02:00:00:00:00:01",
                "password": "0000",
                "connection_mode": CONNECTION_MODE_AUTO,
                "plant_name": "Roof",
                "scan_interval": 60,
            },
            existing_entry=None,
        )

        self.assertEqual(error, "serial_overlap")

    def test_confirmation_shows_netid_mode_serials_and_root(self) -> None:
        flow = SMABluetoothConfigFlow()
        flow._pending_data = {CONF_CONNECTION_MODE: CONNECTION_MODE_AUTO}
        flow._pending_probe = config_flow_module._ProbeResult(
            inverters={
                "2": SMAInverter(serial="2"),
                "1": SMAInverter(serial="1"),
            },
            net_id=0x0F,
            effective_mode=EFFECTIVE_MODE_NETWORK,
            current_root="2",
        )

        self.assertEqual(
            flow._confirmation_placeholders(),
            {
                "net_id": "F",
                "configured_mode": "auto",
                "effective_mode": "network",
                "serials": "1, 2",
                "root": "2",
            },
        )


class SMASessionGateTests(unittest.IsolatedAsyncioTestCase):
    """Exercise RFCOMM retries without touching Bluetooth hardware."""

    async def test_transient_session_failures_are_retried_adapter_wide(self) -> None:
        gate = SMAAdapterGate()
        attempts = 0

        class FakeSession:
            async def __aenter__(self) -> FakeSession:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise OSError("Resource busy")
                return self

            async def async_start_session(self) -> None:
                return None

            async def async_stop_session(self) -> None:
                return None

            async def __aexit__(self, *_: object) -> None:
                return None

        with (
            patch.object(
                gateway_module,
                "SMAClassicClient",
                side_effect=lambda *_args, **_kwargs: FakeSession(),
            ),
            patch.object(
                gateway_module,
                "daylight_schedule",
                return_value=SimpleNamespace(active=True),
            ),
            patch.object(gateway_module.asyncio, "sleep", AsyncMock()) as sleep,
        ):
            result = await gate.async_run(
                SimpleNamespace(),
                "02:00:00:00:00:01",
                "0000",
                CONNECTION_MODE_AUTO,
                AsyncMock(return_value="connected"),
                attempts=3,
            )

        self.assertEqual(result, "connected")
        self.assertEqual(attempts, 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_two_failed_operations_power_cycle_adapter_and_retry(self) -> None:
        gate = SMAAdapterGate()
        attempts = 0

        class FakeSession:
            async def __aenter__(self) -> FakeSession:
                nonlocal attempts
                attempts += 1
                if attempts <= 2:
                    raise OSError("stuck RFCOMM")
                return self

            async def async_start_session(self) -> None:
                return None

            async def async_stop_session(self) -> None:
                return None

            async def __aexit__(self, *_: object) -> None:
                return None

        adapters = SimpleNamespace(
            default_adapter="hci0",
            adapters={"hci0": {"address": "02:00:00:00:00:04"}},
            refresh=AsyncMock(),
        )
        with (
            patch.object(
                gateway_module,
                "SMAClassicClient",
                side_effect=lambda *_args, **_kwargs: FakeSession(),
            ),
            patch.object(
                gateway_module,
                "daylight_schedule",
                return_value=SimpleNamespace(active=True),
            ),
            patch.object(gateway_module, "RFCOMM_RELEASE_DELAY", 0),
            patch.object(gateway_module, "get_adapters", return_value=adapters),
            patch.object(
                gateway_module, "recover_adapter", AsyncMock(return_value=True)
            ) as recover,
        ):
            with self.assertRaises(SMATransportError):
                await gate.async_run(
                    SimpleNamespace(),
                    "02:00:00:00:00:01",
                    "0000",
                    CONNECTION_MODE_AUTO,
                    AsyncMock(),
                    attempts=1,
                )
            result = await gate.async_run(
                SimpleNamespace(),
                "02:00:00:00:00:01",
                "0000",
                CONNECTION_MODE_AUTO,
                AsyncMock(return_value="recovered"),
                attempts=1,
            )

        self.assertEqual(result, "recovered")
        self.assertEqual(attempts, 3)
        adapters.refresh.assert_awaited_once()
        recover.assert_awaited_once_with(0, "02:00:00:00:00:04", gone_silent=False)
        self.assertEqual(gate.consecutive_transport_failures, 0)
        self.assertEqual(gate.recovery_count, 1)
        self.assertIsNotNone(gate.last_recovery_at)

    async def test_protocol_errors_do_not_reset_bluetooth_adapter(self) -> None:
        gate = SMAAdapterGate()

        class FakeSession:
            async def __aenter__(self) -> FakeSession:
                return self

            async def async_start_session(self) -> None:
                raise SMAProtocolError("invalid response")

            async def async_stop_session(self) -> None:
                return None

            async def __aexit__(self, *_: object) -> None:
                return None

        with (
            patch.object(
                gateway_module,
                "SMAClassicClient",
                side_effect=lambda *_args, **_kwargs: FakeSession(),
            ),
            patch.object(
                gateway_module,
                "daylight_schedule",
                return_value=SimpleNamespace(active=True),
            ),
            patch.object(gateway_module, "RFCOMM_RELEASE_DELAY", 0),
            patch.object(gateway_module, "recover_adapter", AsyncMock()) as recover,
        ):
            for _ in range(2):
                with self.assertRaisesRegex(SMAProtocolError, "invalid response"):
                    await gate.async_run(
                        SimpleNamespace(),
                        "02:00:00:00:00:01",
                        "0000",
                        CONNECTION_MODE_AUTO,
                        AsyncMock(),
                        attempts=1,
                    )

        recover.assert_not_awaited()
        self.assertEqual(gate.consecutive_transport_failures, 0)

    async def test_recovery_cooldown_prevents_repeated_power_cycles(self) -> None:
        gate = SMAAdapterGate()
        gate.consecutive_transport_failures = 1
        gate.last_recovery_attempt = gateway_module.monotonic()

        class FakeSession:
            async def __aenter__(self) -> FakeSession:
                raise OSError("stuck during cooldown")

            async def __aexit__(self, *_: object) -> None:
                return None

        with (
            patch.object(
                gateway_module,
                "SMAClassicClient",
                side_effect=lambda *_args, **_kwargs: FakeSession(),
            ),
            patch.object(
                gateway_module,
                "daylight_schedule",
                return_value=SimpleNamespace(active=True),
            ),
            patch.object(gateway_module, "RFCOMM_RELEASE_DELAY", 0),
            patch.object(gateway_module, "recover_adapter", AsyncMock()) as recover,
            patch.object(gateway_module.ir, "async_create_issue") as create_issue,
        ):
            with self.assertRaises(SMATransportError):
                await gate.async_run(
                    SimpleNamespace(),
                    "02:00:00:00:00:01",
                    "0000",
                    CONNECTION_MODE_AUTO,
                    AsyncMock(),
                    attempts=1,
                )

        recover.assert_not_awaited()
        create_issue.assert_called_once()

    async def test_failed_recovery_opens_and_success_clears_repair(self) -> None:
        gate = SMAAdapterGate()
        gate.consecutive_transport_failures = 1
        attempts = 0

        class FakeSession:
            async def __aenter__(self) -> FakeSession:
                nonlocal attempts
                attempts += 1
                if attempts <= 2:
                    raise OSError("still stuck")
                return self

            async def async_start_session(self) -> None:
                return None

            async def async_stop_session(self) -> None:
                return None

            async def __aexit__(self, *_: object) -> None:
                return None

        adapters = SimpleNamespace(
            default_adapter="hci0",
            adapters={"hci0": {"address": "02:00:00:00:00:04"}},
            refresh=AsyncMock(),
        )
        with (
            patch.object(
                gateway_module,
                "SMAClassicClient",
                side_effect=lambda *_args, **_kwargs: FakeSession(),
            ),
            patch.object(
                gateway_module,
                "daylight_schedule",
                return_value=SimpleNamespace(active=True),
            ),
            patch.object(gateway_module, "RFCOMM_RELEASE_DELAY", 0),
            patch.object(gateway_module, "get_adapters", return_value=adapters),
            patch.object(
                gateway_module, "recover_adapter", AsyncMock(return_value=True)
            ),
            patch.object(gateway_module.ir, "async_create_issue") as create_issue,
            patch.object(gateway_module.ir, "async_delete_issue") as delete_issue,
        ):
            with self.assertRaises(SMATransportError):
                await gate.async_run(
                    SimpleNamespace(),
                    "02:00:00:00:00:01",
                    "0000",
                    CONNECTION_MODE_AUTO,
                    AsyncMock(),
                    attempts=1,
                )
            self.assertTrue(gate.recovery_issue_open)
            create_issue.assert_called_once()

            result = await gate.async_run(
                SimpleNamespace(),
                "02:00:00:00:00:01",
                "0000",
                CONNECTION_MODE_AUTO,
                AsyncMock(return_value="ok"),
                attempts=1,
            )

        self.assertEqual(result, "ok")
        delete_issue.assert_called_once_with(
            ANY,
            "sma_bluetooth",
            "bluetooth_recovery_failed",
        )
        self.assertFalse(gate.recovery_issue_open)

    async def test_every_operation_gets_a_fresh_closed_session(self) -> None:
        gate = SMAAdapterGate()
        sessions: list[FakeSession] = []

        class FakeSession:
            def __init__(self) -> None:
                self.started = 0
                self.stopped = 0
                self.closed = 0
                sessions.append(self)

            async def __aenter__(self) -> FakeSession:
                return self

            async def async_start_session(self) -> None:
                self.started += 1

            async def async_stop_session(self) -> None:
                self.stopped += 1

            async def __aexit__(self, *_: object) -> None:
                self.closed += 1

        with (
            patch.object(
                gateway_module,
                "SMAClassicClient",
                side_effect=lambda *_args, **_kwargs: FakeSession(),
            ),
            patch.object(
                gateway_module,
                "daylight_schedule",
                return_value=SimpleNamespace(active=True),
            ),
            patch.object(gateway_module.asyncio, "sleep", AsyncMock()),
        ):
            first = await gate.async_run(
                SimpleNamespace(),
                "02:00:00:00:00:01",
                "0000",
                CONNECTION_MODE_AUTO,
                AsyncMock(return_value=1),
            )
            second = await gate.async_run(
                SimpleNamespace(),
                "02:00:00:00:00:02",
                "0000",
                CONNECTION_MODE_SINGLE,
                AsyncMock(return_value=2),
            )

        self.assertEqual((first, second), (1, 2))
        self.assertEqual(len(sessions), 2)
        self.assertTrue(
            all(
                (session.started, session.stopped, session.closed) == (1, 1, 1)
                for session in sessions
            )
        )

    async def test_two_entries_never_overlap_rfcomm_sessions(self) -> None:
        gate = SMAAdapterGate()
        active = 0
        maximum_active = 0

        class FakeSession:
            async def __aenter__(self) -> FakeSession:
                return self

            async def async_start_session(self) -> None:
                nonlocal active, maximum_active
                active += 1
                maximum_active = max(maximum_active, active)

            async def async_stop_session(self) -> None:
                nonlocal active
                active -= 1

            async def __aexit__(self, *_: object) -> None:
                return None

        async def operation(_client: object) -> str:
            await asyncio.sleep(0)
            return "ok"

        with (
            patch.object(
                gateway_module,
                "SMAClassicClient",
                side_effect=lambda *_args, **_kwargs: FakeSession(),
            ),
            patch.object(
                gateway_module,
                "daylight_schedule",
                return_value=SimpleNamespace(active=True),
            ),
            patch.object(gateway_module, "RFCOMM_RELEASE_DELAY", 0),
        ):
            results = await asyncio.gather(
                gate.async_run(
                    SimpleNamespace(),
                    "02:00:00:00:00:01",
                    "0000",
                    CONNECTION_MODE_SINGLE,
                    operation,
                ),
                gate.async_run(
                    SimpleNamespace(),
                    "02:00:00:00:00:02",
                    "0000",
                    CONNECTION_MODE_SINGLE,
                    operation,
                ),
            )

        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(maximum_active, 1)
        self.assertEqual(active, 0)

    async def test_sunset_while_waiting_for_lock_opens_no_session(self) -> None:
        gate = SMAAdapterGate()
        created = MagicMock()
        schedules = iter((True, False))

        await gate.lock.acquire()
        with (
            patch.object(
                gateway_module,
                "daylight_schedule",
                side_effect=lambda _hass: SimpleNamespace(active=next(schedules)),
            ),
            patch.object(gateway_module, "SMAClassicClient", created),
        ):
            task = asyncio.create_task(
                gate.async_run(
                    SimpleNamespace(),
                    "02:00:00:00:00:01",
                    "0000",
                    CONNECTION_MODE_SINGLE,
                    AsyncMock(),
                )
            )
            await asyncio.sleep(0)
            gate.lock.release()
            with self.assertRaises(SMADaylightError):
                await task

        created.assert_not_called()

    async def test_night_update_performs_no_transport_operation(self) -> None:
        coordinator = coordinator_module.SMABluetoothCoordinator.__new__(
            coordinator_module.SMABluetoothCoordinator
        )
        coordinator.hass = SimpleNamespace()
        coordinator.data = {
            "serial": SMAInverter(serial="serial", values={"power": 10})
        }
        coordinator._known_inverters = {}
        coordinator.async_run_session = AsyncMock()
        sleep_interval = timedelta(hours=8)

        with patch.object(
            coordinator_module,
            "daylight_schedule",
            return_value=DaylightSchedule(False, sleep_interval),
        ):
            data = await coordinator._async_update_data()

        self.assertIs(data, coordinator.data)
        self.assertTrue(coordinator.sleeping)
        self.assertEqual(coordinator.update_interval, sleep_interval)
        coordinator.async_run_session.assert_not_awaited()

    async def test_signal_strength_is_read_per_inverter(self) -> None:
        client = SMAClassicClient("02:00:00:00:00:01", "0000")
        first = _Device(
            b"\x01\x02\x03\x04\x05\x06",
            1000000001,
            131,
            SMAInverter(serial="1000000001"),
        )
        second = _Device(
            b"\x06\x05\x04\x03\x02\x01",
            1000000002,
            131,
            SMAInverter(serial="1000000002"),
        )
        client.devices = [first, second]
        client._initialize = AsyncMock()
        client._logoff = AsyncMock()
        client._logon = AsyncMock()
        client._query = AsyncMock()
        client._signal_strength = AsyncMock(side_effect=[25.0, 75.0])

        result = await client.async_query()

        self.assertEqual(result["1000000001"].values["bt_signal"], 25.0)
        self.assertEqual(result["1000000002"].values["bt_signal"], 75.0)

    async def test_archive_import_requires_every_total_energy_entity(self) -> None:
        coordinator = SimpleNamespace(
            data={
                "1000000001": SMAInverter(serial="1000000001"),
                "1000000002": SMAInverter(serial="1000000002"),
            },
            async_read_archive=AsyncMock(return_value={}),
        )
        registry = SimpleNamespace(
            async_get_entity_id=lambda _domain, _platform, unique_id: (
                "sensor.pv_ertrag_gesamt_wr2" if "1000000001" in unique_id else None
            )
        )

        with (
            patch.object(integration_module.er, "async_get", return_value=registry),
            self.assertRaisesRegex(
                SMAProtocolError, "Missing total-energy entities.*1000000002"
            ),
        ):
            await integration_module._async_import_periods(
                SimpleNamespace(),
                coordinator,
                [(1_000_000, 1_003_600)],
                require_complete=False,
            )

    async def test_daily_reconcile_keeps_guarded_clock_adjustment(self) -> None:
        local_today = datetime(2026, 8, 9, tzinfo=ZoneInfo("Europe/Berlin"))
        coordinator = SimpleNamespace(
            data={
                "1": SMAInverter(serial="1", values={"energy_today": 0.0}),
                "2": SMAInverter(serial="2", values={"energy_today": 0.0}),
            },
            async_sync_clock=AsyncMock(
                return_value=SimpleNamespace(
                    adjusted=False,
                    reason="within_tolerance",
                    difference_seconds=0,
                )
            ),
            async_update_listeners=MagicMock(),
        )
        entry = SimpleNamespace(options={})
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=MagicMock())
        )

        with (
            patch.object(integration_module, "_local_today", return_value=local_today),
            patch.object(
                integration_module.dt_util,
                "now",
                return_value=datetime(2026, 8, 9, 12, tzinfo=ZoneInfo("Europe/Berlin")),
            ),
            patch.object(integration_module.asyncio, "sleep", AsyncMock()),
            patch.object(
                integration_module,
                "_async_import_periods",
                AsyncMock(return_value={"sensor.one": 24, "sensor.two": 24}),
            ),
        ):
            await integration_module._async_reconcile_previous_day(
                hass, entry, coordinator
            )

        coordinator.async_sync_clock.assert_awaited_once_with(3600, True)


if __name__ == "__main__":
    unittest.main()
