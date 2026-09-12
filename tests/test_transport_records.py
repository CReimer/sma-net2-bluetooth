"""Binary frame validation, fragmented transport and measurement decoding."""

import asyncio
import struct
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from custom_components.sma_bluetooth import protocol as p
from custom_components.sma_bluetooth.models import SMAInverter


def record_packet(code, value=1000, size=28, attribute=None):
    record = bytearray(size)
    struct.pack_into("<II", record, 0, code, 300)
    if size == 16:
        struct.pack_into("<Q", record, 8, value)
    elif size >= 20:
        struct.pack_into("<I", record, 16, value)
    if attribute is not None:
        struct.pack_into("<I", record, 8, attribute)
    packet = bytearray(41)
    packet[5] = 9 + size // 4
    struct.pack_into("<II", packet, 33, 0, 0)
    return bytes(packet + record + b"\0\0\x7e")


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.device = p._Device(b"a" * 6, 1, 1, SMAInverter(serial="1"))

    def test_measurement_scaling_and_missing_sentinels(self):
        cases = [
            (p.LRI_ENERGY_TOTAL, "energy_total", 16, 1),
            (p.LRI_ENERGY_TODAY, "energy_today", 16, 1),
            (p.LRI_OPERATION_TIME, "operation_time", 16, 1000 / 3600),
            (p.LRI_FEED_IN_TIME, "feed_in_time", 16, 1000 / 3600),
            (p.LRI_AC_POWER_TOTAL, "ac_power_total", 28, 1000),
            (p.LRI_DC_POWER | 1, "dc_power_1", 28, 1000),
            (p.LRI_DC_VOLTAGE | 1, "dc_voltage_1", 28, 10),
            (p.LRI_DC_CURRENT | 1, "dc_current_1", 28, 1),
            (p.LRI_AC_POWER_1, "ac_power_1", 28, 1000),
            (p.LRI_AC_VOLTAGE_1, "ac_voltage_1", 28, 10),
            (p.LRI_AC_CURRENT_1, "ac_current_1", 28, 1),
            (p.LRI_AC_CURRENT_ALT_2, "ac_current_2", 28, 1),
            (p.LRI_FREQUENCY, "frequency", 28, 10),
            (p.LRI_TEMPERATURE, "temperature", 28, 10),
        ]
        for code, key, size, expected in cases:
            with self.subTest(key=key):
                for value in (1000, (1 << (64 if size == 16 else 32)) - 1):
                    p.SMAClassicClient._parse_records(
                        self.device, record_packet(code, value, size)
                    )
                    self.assertEqual(
                        self.device.inverter.values[key],
                        expected if value == 1000 else None,
                    )
        self.assertEqual(self.device.inverter.record_timestamp, 300)

    def test_attributes_name_class_model_and_version(self):
        for code, key in [
            (p.LRI_OPERATION_HEALTH, "status"),
            (p.LRI_RELAY_STATUS, "relay_status"),
        ]:
            for attribute, expected in [
                (0x010001C7, "Warning"),
                (0x01000063, "99"),
                (0x00FFFFFE, None),
                (0, None),
            ]:
                p.SMAClassicClient._parse_records(
                    self.device, record_packet(code, attribute=attribute)
                )
                self.assertEqual(self.device.inverter.values[key], expected)
        p.SMAClassicClient._parse_records(
            self.device, record_packet(p.LRI_DEVICE_CLASS, attribute=0x01001F41)
        )
        self.assertEqual(self.device.inverter.model, "Solar Inverters")
        p.SMAClassicClient._parse_records(
            self.device, record_packet(p.LRI_DEVICE_MODEL, attribute=0x01002371)
        )
        self.assertEqual(self.device.inverter.model, "SB 3000HF-30")
        p.SMAClassicClient._parse_records(
            self.device, record_packet(p.LRI_DEVICE_CLASS, attribute=0x01001F41)
        )
        self.assertEqual(self.device.inverter.model, "SB 3000HF-30")
        p.SMAClassicClient._parse_records(
            self.device, record_packet(p.LRI_DEVICE_MODEL, attribute=0x01000001)
        )
        self.assertEqual(self.device.inverter.model, "SMA model 1")
        packet = bytearray(record_packet(p.LRI_DEVICE_NAME))
        packet[49:54] = b"Roof\0"
        p.SMAClassicClient._parse_records(self.device, packet)
        self.assertEqual(self.device.inverter.name, "Roof")
        packet = bytearray(record_packet(p.LRI_SOFTWARE_VERSION))
        struct.pack_into("<I", packet, 65, 0x12340504)
        p.SMAClassicClient._parse_records(self.device, packet)
        self.assertEqual(self.device.inverter.software_version, "12.34.05.R")
        self.assertEqual(p._version(255), "00.00.00.?")

    def test_malformed_records_do_not_create_values(self):
        p.SMAClassicClient._parse_records(p._Device(b"a"), b"")
        p.SMAClassicClient._parse_records(self.device, b"")
        packet = bytearray(record_packet(0, size=12))
        struct.pack_into("<II", packet, 33, 2, 0)
        p.SMAClassicClient._parse_records(self.device, packet)
        struct.pack_into("<II", packet, 33, 0, 0)
        packet[5] = 10
        p.SMAClassicClient._parse_records(self.device, packet)
        p.SMAClassicClient._parse_records(self.device, record_packet(0, size=12))
        p.SMAClassicClient._parse_records(self.device, record_packet(0)[:-10])
        p.SMAClassicClient._parse_records(
            self.device, record_packet(p.LRI_SOFTWARE_VERSION, size=12)
        )
        self.assertEqual(self.device.inverter.values, {})
        with self.assertRaises(p.SMAProtocolError):
            p._unescape(b"\x7d")
        with self.assertRaises(ValueError):
            p.SMAClassicClient("AA:BB:CC:DD:EE:FF", "p", connection_mode="invalid")


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # setup-python builds may omit Bluetooth constants. The socket itself is
        # mocked, so the transport tests must not depend on host BlueZ support.
        for name, value in (("AF_BLUETOOTH", 31), ("BTPROTO_RFCOMM", 3)):
            replacement = patch.object(p.socket, name, value, create=True)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = p.SMAClassicClient("AA:BB:CC:DD:EE:FF", "p")

    async def test_socket_connect_receive_send_and_close(self):
        loop = asyncio.get_running_loop()
        sock = Mock()
        with (
            patch.object(p.socket, "socket", return_value=sock),
            patch.object(loop, "sock_connect", AsyncMock()),
            patch.object(
                loop, "sock_recv", AsyncMock(side_effect=[b"a", b"bc"])
            ),
            patch.object(loop, "sock_sendall", AsyncMock()) as send,
        ):
            async with self.client as client:
                self.assertIs(client, self.client)
                self.assertEqual(await client._recv_exact(3), b"abc")
                await client._send(b"packet")
                send.assert_awaited_once_with(sock, b"packet")
            self.assertIsNone(self.client.sock)
            sock.close.assert_called_once()
        await self.client.__aexit__()
        for operation in (self.client._recv_exact(1), self.client._send(b"packet")):
            with self.assertRaises(p.SMAProtocolError):
                await operation
        self.client.root_address = b""
        self.assertIsNone(self.client.current_root)
        self.client.root_serial = "1"
        self.assertEqual(self.client.current_root, "1")

    async def test_socket_failure_and_receive_timeout(self):
        loop = asyncio.get_running_loop()
        sock = Mock()
        for error in (OSError("connect"), TimeoutError("connect")):
            with (
                patch.object(p.socket, "socket", return_value=sock),
                patch.object(loop, "sock_connect", AsyncMock(side_effect=error)),
            ):
                with self.assertRaises(p.SMATransportError):
                    await self.client.__aenter__()
                self.assertIsNone(self.client.sock)
        for result in (b"", TimeoutError()):
            self.client.sock = sock
            with patch.object(
                loop,
                "sock_recv",
                AsyncMock(
                    return_value=result if isinstance(result, bytes) else None,
                    side_effect=result if isinstance(result, Exception) else None,
                ),
            ):
                with self.assertRaises(p.SMATransportError):
                    await self.client._recv_exact(1)
        with patch.object(p, "socket", NS()):
            with self.assertRaisesRegex(p.SMAProtocolError, "no Bluetooth"):
                await self.client.__aenter__()

    def frame(self, payload, command=1, source=b"a" * 6):
        frame = bytearray(self.client._l1(command, b"b" * 6, payload))
        frame[4:10] = source
        return bytes(frame)

    async def receive(self, frames, command=1, sender=None):
        chunks = [chunk for frame in frames for chunk in (frame[:18], frame[18:])]
        with patch.object(self.client, "_recv_exact", AsyncMock(side_effect=chunks)):
            return await self.client._receive_packet(command, sender)

    async def test_frame_filters_and_fragment_reassembly(self):
        payload = self.client._l2_payload(9, 0xA0, 0, 1, 1, b"payload")
        result, source = await self.receive(
            [self.frame(b"ignored", source=b"z" * 6), self.frame(payload)],
            sender=b"a" * 6,
        )
        self.assertEqual(result, p._unescape(payload))
        self.assertEqual(source, b"a" * 6)
        result, _ = await self.receive(
            [
                self.frame(payload[:10], 8),
                self.frame(b"other", 8, b"z" * 6),
                self.frame(payload[10:15], 8),
                self.frame(payload[15:]),
            ]
        )
        self.assertEqual(result, p._unescape(payload))
        result, _ = await self.receive(
            [self.frame(payload[:10]), self.frame(payload[10:])]
        )
        self.assertEqual(result, p._unescape(payload))
        frame = self.frame(b"plain")
        result, _ = await self.receive([self.frame(b"ignored", 8), frame])
        self.assertEqual(result, frame)
        result, _ = await self.receive([frame], command=None)
        self.assertEqual(result, frame)

    async def test_invalid_marker_header_signature_and_checksum(self):
        valid = bytearray(self.frame(b"plain"))
        for offset, value in [(0, 0), (1, 1), (3, 0)]:
            frame = bytearray(valid)
            frame[offset] = value
            with self.assertRaises(p.SMAProtocolError):
                await self.receive([frame])
        for payload in (b"\x7e\0\x7e", b"\x7e" + b"\0" * 8 + b"\x7e"):
            with self.assertRaisesRegex(p.SMAProtocolError, "signature"):
                await self.receive([self.frame(payload)])
        payload = bytearray(self.client._l2_payload(9, 0xA0, 0, 1, 1, b"payload"))
        payload[-2] ^= 1
        with self.assertRaisesRegex(p.SMAProtocolError, "checksum"):
            await self.receive([self.frame(payload)])
