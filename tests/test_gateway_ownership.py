"""Adapter recovery errors and ownership repair lifecycle."""

from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from custom_components.sma_bluetooth import gateway as g, ownership as o
from sma_net2 import protocol as p
from custom_components.sma_bluetooth.const import (
    CONF_CONNECTION_MODE,
    CONF_KNOWN_INVERTERS,
    CONF_LAST_DETECTED_NET_ID,
    CONF_NET_ID,
    DOMAIN,
    RFCOMM_RECOVERY_FAILURES,
)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_lookup_and_recovery_failure(self):
        gate = g.SMAAdapterGate()
        for name, details in [
            (None, {}),
            ("other", {}),
            ("hciX", {}),
            ("hci0", {}),
            ("hci0", {g.ADAPTER_ADDRESS: "AA"}),
        ]:
            adapters = NS(
                refresh=AsyncMock(), default_adapter=name, adapters={name: details}
            )
            with (
                patch.object(g, "get_adapters", return_value=adapters),
                patch.object(
                    g, "recover_adapter", AsyncMock(return_value=False)
                ) as recover,
            ):
                self.assertFalse(await gate._async_recover_adapter())
                self.assertEqual(recover.await_count, int(bool(details)))
        self.assertEqual(gate.recovery_count, 5)

    async def test_transport_errors_are_wrapped_and_fatal_errors_not_retried(self):
        gate = g.SMAAdapterGate()
        for timeout in (None, 10):
            for error in (TimeoutError(), OSError("wire")):
                with patch.object(
                    gate, "_async_run_once", AsyncMock(side_effect=error)
                ):
                    with self.assertRaises(p.SMATransportError):
                        await gate._async_execute_once(
                            "AA", "p", "auto", AsyncMock(), timeout
                        )
        for error in (
            p.SMAAuthenticationError("auth"),
            p.SMAConfigurationError("config"),
            g.SMADaylightError("night"),
        ):
            with (
                patch.object(gate, "_ensure_daylight"),
                patch.object(
                    gate, "_async_execute_once", AsyncMock(side_effect=error)
                ) as execute,
            ):
                with self.assertRaises(type(error)):
                    await gate.async_run(NS(), "AA", "p", "auto", AsyncMock())
                execute.assert_awaited_once()

    async def test_recovery_failures_preserve_error_categories(self):
        for error in (
            p.SMAAuthenticationError("auth"),
            g.SMADaylightError("night"),
            p.SMAProtocolError("protocol"),
            RuntimeError("dbus"),
        ):
            gate = g.SMAAdapterGate()
            gate.consecutive_transport_failures = RFCOMM_RECOVERY_FAILURES
            with (
                patch.object(gate, "_ensure_daylight"),
                patch.object(
                    gate,
                    "_async_execute_once",
                    AsyncMock(side_effect=p.SMATransportError("wire")),
                ),
                patch.object(
                    gate, "_async_recover_adapter", AsyncMock(side_effect=error)
                ),
                patch.object(gate, "_raise_recovery_issue") as issue,
            ):
                with self.assertRaises(
                    p.SMATransportError if type(error) is RuntimeError else type(error)
                ):
                    await gate.async_run(
                        NS(), "AA", "p", "auto", AsyncMock(), attempts=1
                    )
                self.assertEqual(issue.call_count, int(type(error) is RuntimeError))

    async def test_logoff_error_still_releases_gate(self):
        gate = g.SMAAdapterGate()
        client = NS(
            async_start_session=AsyncMock(),
            async_stop_session=AsyncMock(side_effect=OSError("logoff")),
        )
        context = AsyncMock()
        context.__aenter__.return_value = client
        # The gate retains the client itself, not the context return value.
        context.async_start_session = client.async_start_session
        context.async_stop_session = client.async_stop_session
        with patch.object(g, "SMAClassicClient", return_value=context):
            self.assertEqual(
                await gate._async_run_once(
                    "AA", "p", "auto", AsyncMock(return_value=42)
                ),
                42,
            )
        self.assertIsNone(gate.active_address)
        context.__aexit__.assert_awaited_once()
        hass = NS(data={})
        self.assertIs(g.async_get_adapter_gate(hass), g.async_get_adapter_gate(hass))


class OwnershipTests(unittest.TestCase):
    def entry(self, entry_id, serials):
        return NS(
            entry_id=entry_id,
            title=entry_id,
            options={},
            data={CONF_KNOWN_INVERTERS: [{"serial": serial} for serial in serials]},
        )

    def test_overlap_repairs_create_and_remove_only_resolved_issues(self):
        a, b = self.entry("a", ["1", "2"]), self.entry("b", ["1"])
        hass = NS(config_entries=NS(async_entries=Mock(return_value=[a, b])))
        registry = NS(
            issues={
                (DOMAIN, "serial_overlap_1"): None,
                (DOMAIN, "serial_overlap_old"): None,
                ("other", "serial_overlap_x"): None,
                (DOMAIN, "unrelated"): None,
            }
        )
        with (
            patch.object(o.ir, "async_get", return_value=registry),
            patch.object(o.ir, "async_create_issue") as create,
            patch.object(o.ir, "async_delete_issue") as delete,
        ):
            o.async_refresh_overlap_issues(hass)
            self.assertEqual(create.call_args.args[2], "serial_overlap_1")
            delete.assert_called_once_with(hass, DOMAIN, "serial_overlap_old")
        self.assertEqual(
            o.entries_claiming_serials(hass, ["1"], exclude_entry_id="a"), {"1": [b]}
        )
        a.data[CONF_KNOWN_INVERTERS] = "bad"
        self.assertEqual(o.entry_known_serials(a), set())

    def test_mode_and_netid_issue_lifecycle(self):
        a = self.entry("a", [])
        hass = NS(config_entries=NS(async_update_entry=Mock()))
        for mode, old, new, expected in [
            ("single", 1, 2, "netid_changed"),
            ("network", None, 1, "network_to_single"),
            ("auto", None, 2, "single_to_network"),
        ]:
            a.data.update({CONF_CONNECTION_MODE: mode, CONF_NET_ID: old})
            with patch.object(o.ir, "async_create_issue") as create:
                o.async_note_netid_change(hass, a, new, [])
                self.assertEqual(create.call_args.kwargs["translation_key"], expected)
        with patch.object(o.ir, "async_delete_issue") as delete:
            o.async_clear_netid_issues(hass, "a")
            self.assertEqual(delete.call_count, 3)

    def test_departing_entry_requires_unambiguous_matching_plant(self):
        a = self.entry("a", ["1"])
        hass = NS(config_entries=NS(async_entries=Mock(return_value=[])))
        self.assertIsNone(o.async_transfer_departing_entry(hass, a))
        a.options[CONF_LAST_DETECTED_NET_ID] = 2
        self.assertIsNone(o.async_transfer_departing_entry(hass, a))

    def test_registry_move_preserves_existing_owner_and_unrelated_entities(self):
        hass = NS()
        matching = NS(
            platform=DOMAIN,
            unique_id="sma_bluetooth_1_energy",
            config_entry_id="owner",
            entity_id="sensor.energy",
        )
        unrelated = NS(
            platform="other",
            unique_id="sma_bluetooth_1_energy",
            config_entry_id="old",
            entity_id="sensor.other",
        )
        registry = NS(
            entities={"energy": matching, "other": unrelated},
            async_update_entity=Mock(),
        )
        devices = NS(
            async_get_devices=Mock(
                return_value=[NS(id="dev", config_entry_id="owner")]
            ),
            async_update_device=Mock(),
        )
        with (
            patch.object(o.er, "async_get", return_value=registry),
            patch.object(o.dr, "async_get", return_value=devices),
        ):
            o._move_serial_registries(hass, "1", "owner")
        registry.async_update_entity.assert_not_called()
        devices.async_update_device.assert_not_called()

    def test_reconciliation_retains_registry_owner_and_claims_new_serial(self):
        a, b = self.entry("a", []), self.entry("b", ["1"])
        hass = NS(config_entries=NS(async_entries=Mock(return_value=[a, b])))
        registry = NS(
            entities={
                "energy": NS(
                    platform=DOMAIN,
                    unique_id="sma_bluetooth_1_energy",
                    config_entry_id="b",
                )
            }
        )
        with (
            patch.object(o.er, "async_get", return_value=registry),
            patch.object(o, "_move_serial_registries") as move,
            patch.object(o, "_sync_overlap_issues"),
        ):
            self.assertEqual(o.async_reconcile_ownership(hass, a, ["1", "2"]), {"2"})
        move.assert_called_once_with(hass, "2", "a")

    def test_transfer_ignores_invalid_identity_records(self):
        departing, owner = self.entry("old", ["1"]), self.entry("owner", ["2"])
        for entry in (departing, owner):
            entry.options[CONF_LAST_DETECTED_NET_ID] = 2
        departing.data[CONF_KNOWN_INVERTERS].append({"serial": None})
        hass = NS(
            config_entries=NS(
                async_entries=Mock(return_value=[owner]), async_update_entry=Mock()
            )
        )
        with patch.object(o, "_move_serial_registries") as move:
            self.assertEqual(o.async_transfer_departing_entry(hass, departing), "owner")
        move.assert_called_once_with(hass, "1", "owner")
        merged = hass.config_entries.async_update_entry.call_args.kwargs["options"][
            CONF_KNOWN_INVERTERS
        ]
        self.assertEqual([item["serial"] for item in merged], ["1", "2"])
