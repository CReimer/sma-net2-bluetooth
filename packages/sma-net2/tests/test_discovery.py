"""BlueZ discovery is tested without Home Assistant or system D-Bus access."""

import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from dbus_fast import Variant
from sma_net2 import discovery as d


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
        with (
            patch.object(
                d,
                "MessageBus",
                return_value=NS(connect=AsyncMock(side_effect=OSError("bus"))),
            ),
            self.assertRaisesRegex(d.SMADiscoveryError, "Unable to connect"),
        ):
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
            with (
                patch.object(
                    d,
                    "MessageBus",
                    return_value=NS(connect=AsyncMock(return_value=bus)),
                ),
                self.assertRaises(
                    asyncio.CancelledError
                    if isinstance(error, asyncio.CancelledError)
                    else d.SMADiscoveryError
                ),
            ):
                await d.async_discover_sma_devices()
            bus.disconnect.assert_called_once()
