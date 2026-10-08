"""Resource cleanup and source fidelity at public API boundaries."""

import asyncio
import struct
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from sma_net2 import discovery as d
from sma_net2 import protocol as p
from sma_net2.models import SMAInverter


class QualityContracts(unittest.IsolatedAsyncioTestCase):
    async def test_partial_discovery_failure_and_cancellation_stop_started_adapters(
        self,
    ):
        for failure_at in ("filter", "start", "wait"):
            for error in (
                RuntimeError("adapter unavailable"),
                asyncio.CancelledError(),
            ):
                adapters = [
                    NS(
                        call_set_discovery_filter=AsyncMock(),
                        call_start_discovery=AsyncMock(),
                        call_stop_discovery=AsyncMock(),
                    )
                    for _ in range(2)
                ]
                if failure_at != "wait":
                    getattr(
                        adapters[1],
                        f"call_{'set_discovery_filter' if failure_at == 'filter' else 'start_discovery'}",
                    ).side_effect = error
                objects = {f"/adapter{i}": {"org.bluez.Adapter1": {}} for i in range(2)}
                manager = NS(call_get_managed_objects=AsyncMock(return_value=objects))
                bus = NS(
                    introspect=AsyncMock(),
                    disconnect=Mock(),
                    get_proxy_object=Mock(
                        side_effect=lambda name, path, intro, manager=manager, adapters=adapters: (
                            NS(
                                get_interface=Mock(
                                    return_value=manager
                                    if path == "/"
                                    else adapters[int(path[-1])]
                                )
                            )
                        )
                    ),
                )
                with (
                    patch.object(
                        d,
                        "MessageBus",
                        return_value=NS(connect=AsyncMock(return_value=bus)),
                    ),
                    patch.object(
                        d.asyncio,
                        "sleep",
                        AsyncMock(side_effect=error if failure_at == "wait" else None),
                    ),
                    self.assertRaises(
                        asyncio.CancelledError
                        if isinstance(error, asyncio.CancelledError)
                        else d.SMADiscoveryError
                    ),
                ):
                    await d.async_discover_sma_devices()
                adapters[0].call_stop_discovery.assert_awaited_once()
                self.assertEqual(
                    adapters[1].call_stop_discovery.await_count,
                    int(failure_at == "wait"),
                )
                bus.disconnect.assert_called_once()

    def test_record_time_difference_requires_both_observations(self):
        inverter = SMAInverter(serial="1")
        self.assertIsNone(inverter.record_clock_difference)
        inverter.record_timestamp = 300
        self.assertIsNone(inverter.record_clock_difference)
        inverter.record_received_at = 310.5
        self.assertEqual(inverter.record_clock_difference, -10.5)

    async def test_archive_ignores_stale_invalid_and_truncated_records(self):
        client = p.SMAClassicClient("AA:BB:CC:DD:EE:FF", "secret")
        device = p._Device(client.connection_address, 1, 125)
        client._send = AsyncMock()
        replies = 0

        async def receive(*args):
            nonlocal replies
            replies += 1
            packet = bytearray(41)
            struct.pack_into("<H", packet, 27, client.packet_id if replies > 1 else 0)
            struct.pack_into("<H", packet, 25, int(replies == 2))
            for timestamp, total in [
                (300, 1000),
                (300, 1100),
                (301, 1200),
                (600, 0xFFFFFFFFFFFFFFFF),
                (900, 0x8000000000000000),
                (1200, 2000),
                (1800, 3000),
            ]:
                packet.extend(struct.pack("<IQ", timestamp, total))
            packet.extend(b"truncated\0\0\x7e")
            return bytes(packet), device.address

        client._receive_packet = receive
        points = await client._archive_day(device, 600, 1500)
        self.assertEqual(replies, 3)
        self.assertEqual([point.timestamp for point in points], [1200, 1200])
        self.assertEqual([point.total_energy_kwh for point in points], [2, 2])

    async def test_clock_recent_adjustment_prevents_write(self):
        client = p.SMAClassicClient("AA:BB:CC:DD:EE:FF", "secret")
        client._session_active = True
        with (
            patch.object(p.time, "time", return_value=10000),
            patch.object(
                client,
                "_read_clock",
                AsyncMock(return_value=p.SMAClockInfo(10100, 9999, 0, False, 1)),
            ),
            patch.object(client, "_set_clock", AsyncMock()) as write,
        ):
            result = await client.async_sync_clock_active(0, False)
        self.assertEqual(result.reason, "recently_adjusted")
        self.assertFalse(result.adjusted)
        write.assert_not_awaited()

    async def test_live_query_roles_and_multiple_mppt_aggregation(self):
        client = p.SMAClassicClient("AA:BB:CC:DD:EE:FF", "secret")
        client._session_active = True
        client.devices = [
            p._Device(client.connection_address, 1, 125),
            p._Device(b"x" * 6, 2, 125),
        ]
        client._signals = {device.address: 50 for device in client.devices}
        client.root_address = client.connection_address

        async def query(device, *args):
            device.inverter.values.update(
                dc_power_1=10, dc_power_2=20, dc_power_3=None, dc_power_total=999
            )

        for mode, roles in [
            ("single", ["direct", "direct"]),
            ("network", ["root_node", "participant"]),
        ]:
            client.effective_mode = mode
            with patch.object(client, "_query", query):
                result = await client.async_query_active()
            self.assertEqual([i.network_role for i in result.values()], roles)
            self.assertEqual(
                [i.values["dc_power_total"] for i in result.values()], [30, 30]
            )
            self.assertEqual([i.values["bt_signal"] for i in result.values()], [50, 50])

    async def test_cancelled_connect_closes_socket_and_transport_errors_are_typed(self):
        client = p.SMAClassicClient("AA:BB:CC:DD:EE:FF", "secret")
        loop = asyncio.get_running_loop()
        sock = Mock()
        with (
            patch.object(p.socket, "AF_BLUETOOTH", 31, create=True),
            patch.object(p.socket, "BTPROTO_RFCOMM", 3, create=True),
            patch.object(p.socket, "socket", return_value=sock),
            patch.object(
                loop, "sock_connect", AsyncMock(side_effect=asyncio.CancelledError())
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await client.__aenter__()
        sock.close.assert_called_once()
        self.assertIsNone(client.sock)
        client.sock = sock
        for operation, method in [
            (client._recv_exact, "sock_recv"),
            (client._send, "sock_sendall"),
        ]:
            for error in (OSError("broken pipe"), TimeoutError()):
                with (
                    patch.object(loop, method, AsyncMock(side_effect=error)),
                    self.assertRaises(p.SMATransportError) as failure,
                ):
                    await operation(1 if method == "sock_recv" else b"data")
                self.assertIs(failure.exception.__cause__, error)
        # Public diagnostics work both before and after discovery, and never reveal secrets.
        import json

        client.devices = [
            p._Device(
                b"x" * 6,
                1,
                125,
                SMAInverter(
                    serial="PRIVATE", name="PRIVATE-NAME", values={"energy_total": 1}
                ),
            ),
            p._Device(b"y" * 6),
        ]
        result = client.diagnostics()
        self.assertEqual(result["inverter_count"], 2)
        self.assertEqual(result["inverters"][0]["available_values"], ["energy_total"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("AA:BB", json.dumps(result))

    async def test_explicit_network_mode_and_anonymous_logoff_payload(self):
        client = p.SMAClassicClient(
            "AA:BB:CC:DD:EE:FF", "secret", connection_mode="network"
        )
        client._send = AsyncMock()
        announcement = bytearray(23)
        announcement[19] = 4
        announcement[22] = 2
        with (
            patch.object(
                client,
                "_receive_packet",
                AsyncMock(return_value=(announcement, client.connection_address)),
            ),
            patch.object(client, "_initialize_network", AsyncMock()) as initialize,
        ):
            await client._initialize()
        initialize.assert_awaited_once_with(2)
        self.assertEqual(client.effective_mode, "network")
        await client._logoff()
        payload = p._unescape(client._send.call_args.args[0][18:])
        self.assertIn(struct.pack("<II", 0xFFFD010E, 0xFFFFFFFF), payload)

    def test_reserved_checksum_retries_with_new_packet_id(self):
        client = p.SMAClassicClient("AA:BB:CC:DD:EE:FF", "secret")
        client.packet_id = 0x7FFE
        with patch.object(p, "_fcs", side_effect=[0x7E00, 0x2234]) as checksum:
            payload = client._l2_payload(9, 0xA0, 0, 1, 1, b"data")
        self.assertEqual(checksum.call_count, 2)
        self.assertEqual(client.packet_id, 1)
        self.assertTrue(payload.endswith(b"\x34\x22\x7e"))

    async def test_socket_allocation_failure_is_a_transport_error(self):
        client = p.SMAClassicClient("AA:BB:CC:DD:EE:FF", "secret")
        with (
            patch.object(p.socket, "AF_BLUETOOTH", 31, create=True),
            patch.object(p.socket, "BTPROTO_RFCOMM", 3, create=True),
            patch.object(
                p.socket, "socket", side_effect=OSError("adapter unavailable")
            ),
            self.assertRaises(p.SMATransportError),
        ):
            await client.__aenter__()
        self.assertIsNone(client.sock)

    async def test_pending_socket_io_keeps_event_loop_responsive(self):
        client = p.SMAClassicClient("AA:BB:CC:DD:EE:FF", "secret")
        left, right = p.socket.socketpair()
        left.setblocking(False)
        right.setblocking(False)
        client.sock = left
        task = asyncio.create_task(client._recv_exact(3))
        try:
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            await asyncio.get_running_loop().sock_sendall(right, b"abc")
            self.assertEqual(await asyncio.wait_for(task, 1), b"abc")
            await client._send(b"def")
            self.assertEqual(
                await asyncio.get_running_loop().sock_recv(right, 3), b"def"
            )
        finally:
            task.cancel()
            await client.__aexit__()
            right.close()
