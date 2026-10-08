"""SMA-Net2 framing, record ordering, topology and clock safety contracts."""

import struct
import unittest
from unittest.mock import AsyncMock, patch

from sma_net2 import protocol as protocol_module
from sma_net2.const import (
    CONNECTION_MODE_AUTO,
    CONNECTION_MODE_NETWORK,
    CONNECTION_MODE_SINGLE,
    EFFECTIVE_MODE_NETWORK,
    EFFECTIVE_MODE_SINGLE,
)
from sma_net2.models import SMAInverter
from sma_net2.protocol import (
    L2_SIGNATURE,
    SMAClassicClient,
    SMAClockInfo,
    SMANetworkModeError,
    _Device,
    _escape,
    _fcs,
    _unescape,
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
