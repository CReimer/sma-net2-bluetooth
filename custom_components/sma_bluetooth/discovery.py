"""Bluetooth Classic discovery through the local BlueZ D-Bus API."""

from __future__ import annotations

import asyncio

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus


class SMADiscoveryError(RuntimeError):
    """BlueZ discovery could not be completed."""


async def async_discover_sma_devices(timeout: float = 8) -> dict[str, str]:
    """Return Bluetooth address to display name for nearby SMA devices."""
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as err:
        raise SMADiscoveryError(f"Unable to connect to BlueZ D-Bus: {err}") from err

    try:
        root_intro = await bus.introspect("org.bluez", "/")
        root = bus.get_proxy_object("org.bluez", "/", root_intro)
        manager = root.get_interface("org.freedesktop.DBus.ObjectManager")
        objects = await manager.call_get_managed_objects()
        adapter_paths = [
            path
            for path, interfaces in objects.items()
            if "org.bluez.Adapter1" in interfaces
        ]
        if not adapter_paths:
            raise SMADiscoveryError("No local BlueZ adapter found")

        adapters = []
        for path in adapter_paths:
            intro = await bus.introspect("org.bluez", path)
            obj = bus.get_proxy_object("org.bluez", path, intro)
            adapter = obj.get_interface("org.bluez.Adapter1")
            await adapter.call_set_discovery_filter(
                {"Transport": Variant("s", "bredr")}
            )
            await adapter.call_start_discovery()
            adapters.append(adapter)

        try:
            await asyncio.sleep(timeout)
        finally:
            for adapter in adapters:
                try:
                    await adapter.call_stop_discovery()
                except Exception:
                    pass

        objects = await manager.call_get_managed_objects()
        found: dict[str, str] = {}
        for interfaces in objects.values():
            properties = interfaces.get("org.bluez.Device1")
            if not properties:
                continue
            address = properties.get("Address")
            name = properties.get("Name") or properties.get("Alias")
            if address and name and str(name.value).upper().startswith("SMA"):
                found[str(address.value).upper()] = str(name.value)
        return dict(sorted(found.items()))
    except SMADiscoveryError:
        raise
    except Exception as err:
        raise SMADiscoveryError(f"BlueZ discovery failed: {err}") from err
    finally:
        bus.disconnect()
