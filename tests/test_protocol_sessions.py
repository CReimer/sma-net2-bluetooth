"""Authentication, topology discovery, query retry and clock contracts."""

import struct
import unittest
from unittest.mock import AsyncMock, patch
from custom_components.sma_bluetooth import protocol as p


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = p.SMAClassicClient("AA:BB:CC:DD:EE:FF", "p")
        self.device = p._Device(self.client.connection_address, 1, 125)
        self.client.devices = [self.device]
        self.client._send = AsyncMock()

    def reply(self, status=0, remaining=0, size=65, request_id=None):
        packet = bytearray(size)
        struct.pack_into(
            "<HHH",
            packet,
            23,
            status,
            remaining,
            self.client.packet_id if request_id is None else request_id,
        )
        return packet

    async def test_identification_ignores_unrelated_replies(self):
        def reply(kind):
            packet = self.reply(request_id=0 if kind == "wrong_id" else None)
            struct.pack_into("<HI", packet, 55, 125, 12345)
            return bytes(
                packet[:40] if kind == "short" else packet
            ), b"z" * 6 if kind == "unknown" else self.device.address

        kinds = iter(["wrong_id", "unknown", "short", "valid"])

        async def receive(*args):
            return reply(next(kinds))

        with patch.object(self.client, "_receive_packet", receive):
            await self.client._identify_devices()
        self.assertEqual(self.device.serial, 12345)
        self.assertEqual(self.client.root_serial, "12345")
        self.assertEqual(self.device.inverter.susy_id, "125")
        self.client.devices = []
        with self.assertRaisesRegex(p.SMAProtocolError, "No SMA inverter"):
            await self.client._identify_devices()

    async def test_initialize_single_and_network_topologies(self):
        for length in (20, 32):
            reply = bytearray(length)
            if length == 32:
                reply[26:32] = b"l" * 6
            with (
                patch.object(
                    self.client,
                    "_receive_packet",
                    AsyncMock(return_value=(reply, b"a")),
                ),
                patch.object(self.client, "_identify_devices", AsyncMock()) as identify,
            ):
                await self.client._initialize_single(1)
                identify.assert_awaited_once()
                self.assertEqual(
                    self.client.devices[0].address, self.client.connection_address
                )
        self.assertEqual(self.client.local_address, b"l" * 6)
        for count in (0, 1, 2):
            root = bytearray(31)
            root[18:24] = b"r" * 6
            root[24] = 2
            root[25:31] = b"l" * 6
            topology = (
                b"\0" * 18
                + b"a" * 6
                + b"\0\0"
                + b"".join(bytes([i]) * 6 + b"\x01\x01" for i in range(count))
            )
            with (
                patch.object(
                    self.client,
                    "_receive_packet",
                    AsyncMock(side_effect=[(root, b"a"), (topology, b"r")]),
                ),
                patch.object(self.client, "_build_network", AsyncMock()) as build,
                patch.object(self.client, "_identify_devices", AsyncMock()) as identify,
            ):
                if count:
                    await self.client._initialize_network(2)
                    identify.assert_awaited_once()
                else:
                    with self.assertRaisesRegex(p.SMAProtocolError, "No inverter"):
                        await self.client._initialize_network(2)
                self.assertEqual(build.await_count, int(count == 1))
        self.assertEqual(self.client.root_address, b"r" * 6)
        for reply in (b"", b"\0" * 23):
            with patch.object(
                self.client, "_receive_packet", AsyncMock(return_value=(reply, b"a"))
            ):
                with self.assertRaisesRegex(p.SMAProtocolError, "firmware"):
                    await self.client._initialize()

    async def test_network_build_adds_only_new_addresses(self):
        def packet(kind, payload=b""):
            result = bytearray(18)
            struct.pack_into("<H", result, 16, kind)
            return bytes(result) + payload, self.client.root_address

        topology = self.device.address + b"\x01\x01" + b"n" * 6 + b"\x01\x01"
        replies = [packet(4)] * 3 + [
            packet(0x1001),
            packet(5, topology),
            packet(0x99),
            packet(6),
        ]
        with patch.object(
            self.client, "_receive_packet", AsyncMock(side_effect=replies)
        ):
            await self.client._build_network(2)
        self.assertEqual(
            [d.address for d in self.client.devices], [self.device.address, b"n" * 6]
        )
        with patch.object(
            self.client, "_receive_packet", AsyncMock(return_value=packet(0x99))
        ):
            await self.client._build_network(2)

    async def test_logon_validates_correlation_and_password(self):
        async def receive(*args):
            packet = self.reply(status=next(statuses))
            struct.pack_into("<I", packet, 41, 100)
            struct.pack_into("<HI", packet, 15, 125, 42)
            return packet, self.device.address

        for status, error in [
            (0, None),
            (0x100, p.SMAAuthenticationError),
            (2, p.SMAProtocolError),
        ]:
            statuses = iter([status])
            with (
                patch.object(p.time, "time", return_value=100),
                patch.object(self.client, "_receive_packet", receive),
            ):
                if error:
                    with self.assertRaises(error):
                        await self.client._logon()
                else:
                    await self.client._logon()
                    self.assertEqual(self.device.serial, 42)
        count = 0

        async def stale_then_valid(*args):
            nonlocal count
            count += 1
            packet = self.reply(request_id=0 if count == 1 else None)
            struct.pack_into("<I", packet, 41, 99 if count == 2 else 100)
            return packet, b"unknown"

        with (
            patch.object(p.time, "time", return_value=100),
            patch.object(self.client, "_receive_packet", stale_then_valid),
        ):
            await self.client._logon()
        self.assertEqual(count, 3)

    async def test_query_status_retry_and_multi_packet(self):
        for status in (0, 21, 2):

            async def receive(*args):
                return self.reply(status), self.device.address

            with (
                patch.object(self.client, "_receive_packet", receive),
                patch.object(self.client, "_parse_records") as parse,
            ):
                if status == 2:
                    with self.assertRaisesRegex(p.SMAProtocolError, "Query"):
                        await self.client._query(self.device, 1, 2, 3)
                else:
                    await self.client._query(self.device, 1, 2, 3)
                    self.assertEqual(parse.call_count, int(status == 0))
        for failures in (1, 3):
            calls = 0

            async def receive(*args):
                nonlocal calls
                calls += 1
                if calls <= failures:
                    raise p.SMATransportError("Timeout receiving")
                return self.reply(21), self.device.address

            with patch.object(self.client, "_receive_packet", receive):
                if failures == 3:
                    with self.assertRaises(p.SMAProtocolError):
                        await self.client._query(self.device, 1, 2, 3)
                else:
                    await self.client._query(self.device, 1, 2, 3)
                self.assertEqual(calls, min(3, failures + 1))
        count = 0

        async def receive(*args):
            nonlocal count
            count += 1
            return self.reply(
                remaining=1 if count == 2 else 0, request_id=0 if count == 1 else None
            ), self.device.address

        with (
            patch.object(self.client, "_receive_packet", receive),
            patch.object(self.client, "_parse_records") as parse,
        ):
            await self.client._query(self.device, 1, 2, 3)
            self.assertEqual(parse.call_count, 2)

    async def test_signal_clock_read_and_set_payload(self):
        for reply, expected in [(b"", None), (b"\0" * 22 + b"\xff", 100)]:
            with patch.object(
                self.client, "_receive_packet", AsyncMock(return_value=(reply, b"a"))
            ):
                self.assertEqual(
                    await self.client._signal_strength(self.device), expected
                )
        packet = self.reply()
        struct.pack_into("<IIIIII", packet, 41, 0x00236D00, 100, 90, 0, 3601, 2)
        with patch.object(
            self.client,
            "_receive_packet",
            AsyncMock(side_effect=[(b"", b"a"), (b"\0" * 65, b"a"), (packet, b"a")]),
        ):
            clock = await self.client._read_clock()
        self.assertEqual(clock, p.SMAClockInfo(100, 90, 3600, True, 2))
        await self.client._set_clock(200, 3600, True, 3)
        payload = p._unescape(self.client._send.call_args.args[0][18:])
        self.assertIn(struct.pack("<IIII", 200, 200, 200, 3601), payload)

    async def test_session_wrappers_cleanup_and_authentication_guards(self):
        for operation in (
            self.client.async_query_active(),
            self.client.async_read_archive_active([]),
            self.client.async_sync_clock_active(0, False),
        ):
            with self.assertRaisesRegex(p.SMAProtocolError, "not authenticated"):
                await operation
        with (
            patch.object(self.client, "_initialize", AsyncMock()) as initialize,
            patch.object(self.client, "_logoff", AsyncMock()) as logoff,
            patch.object(self.client, "_signal_strength", AsyncMock(return_value=50)),
            patch.object(self.client, "_logon", AsyncMock()),
        ):
            await self.client.async_start_session()
            await self.client.async_start_session()
            initialize.assert_awaited_once()
            self.assertTrue(self.client._session_active)
            await self.client.async_stop_session()
            await self.client.async_stop_session()
            self.assertEqual(logoff.await_count, 2)
        for name, args, active in [
            ("async_query", (), "async_query_active"),
            ("async_read_archive", ([],), "async_read_archive_active"),
            ("async_sync_clock", (3600, False), "async_sync_clock_active"),
        ]:
            for error in (None, p.SMAProtocolError("failed")):
                with (
                    patch.object(self.client, "async_start_session", AsyncMock()),
                    patch.object(
                        self.client, "async_stop_session", AsyncMock()
                    ) as stop,
                    patch.object(
                        self.client,
                        active,
                        AsyncMock(return_value="result", side_effect=error),
                    ),
                ):
                    if error:
                        with self.assertRaises(p.SMAProtocolError):
                            await getattr(self.client, name)(*args)
                    else:
                        self.assertEqual(
                            await getattr(self.client, name)(*args), "result"
                        )
                    stop.assert_awaited_once()
        self.client._session_active = True
        with patch.object(
            self.client, "_archive_day", AsyncMock(return_value=[])
        ) as archive:
            self.assertEqual(
                await self.client.async_read_archive_active([600, (900, 1200)]),
                {"1": []},
            )
            self.assertEqual(
                [call.args[1:] for call in archive.call_args_list],
                [(600, 87000), (900, 1200)],
            )

    async def test_clock_verification_failure(self):
        self.client._session_active = True
        before = p.SMAClockInfo(10100, 0, 0, False, 1)
        with (
            patch.object(self.client, "_read_clock", AsyncMock(return_value=before)),
            patch.object(self.client, "_set_clock", AsyncMock()),
            patch.object(p.time, "time", return_value=10000),
            patch.object(p.asyncio, "sleep", AsyncMock()),
        ):
            with self.assertRaisesRegex(p.SMAProtocolError, "verification"):
                await self.client.async_sync_clock_active(0, False)
