"""Failure, identity and coverage policy contracts required for Silver."""

import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from sma_net2 import SMAAuthenticationError, SMAProtocolError, SMATransportError

from custom_components import sma_bluetooth as integration
from custom_components.sma_bluetooth import archive, event, sensor
from tests.test_coordinator_platforms import make_coordinator
from tools.run_tests import check_summary


class SilverContracts(unittest.TestCase):
    def test_all_inverter_entities_unavailable_when_inverter_disappears(self):
        co = make_coordinator()
        entities = [
            sensor.SMASensor(co, "1", sensor.DESCRIPTIONS[0]),
            sensor.SMAInverterRoleSensor(co, "1"),
            sensor.SMAInverterRecordTimestampSensor(co, "1"),
            sensor.SMAInverterClockDifferenceSensor(co, "1"),
            event.SMAInverterEvent(co, "1"),
        ]
        self.assertTrue(all(entity.available for entity in entities))
        data = co.data
        co.data = {}
        self.assertTrue(all(not entity.available for entity in entities))
        co.data = data
        self.assertTrue(all(entity.available for entity in entities))
        co.last_update_success = False
        self.assertTrue(all(not entity.available for entity in entities))
        self.assertEqual(sensor.PARALLEL_UPDATES, 0)
        self.assertEqual(event.PARALLEL_UPDATES, 0)

    def test_invalid_or_empty_archive_input_cannot_create_statistics(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            archive.completed_day_periods(
                datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None), 1
            )
        self.assertFalse(
            archive.timestamp_series_complete(
                [{0}, {300}], 0, 600, require_all_slots=False
            )
        )
        for timestamp_sets in ([], [set()]):
            self.assertFalse(
                archive.timestamp_series_complete(
                    timestamp_sets, 0, 600, require_all_slots=False
                )
            )
        self.assertEqual(archive.cumulative_statistic_sums([], previous=(10, 5)), [])

    def test_coverage_gate_rejects_low_module_or_branch_coverage(self):
        def summary(lines, branches, count=100):
            return {
                "covered_lines": lines,
                "num_statements": count,
                "covered_branches": branches,
                "num_branches": count,
            }

        with redirect_stdout(StringIO()):
            self.assertFalse(check_summary("module", summary(95, 100)))
            self.assertFalse(check_summary("module", summary(100, 95)))
            self.assertFalse(check_summary("module", summary(95999, 100000, 100000)))
            self.assertTrue(check_summary("module", summary(96, 96)))
            self.assertTrue(check_summary("no branches", summary(0, 0, 0)))
            self.assertFalse(
                check_summary("config flow", summary(100, 99), minimum=100)
            )


class ArchiveActionFailures(unittest.IsolatedAsyncioTestCase):
    async def test_import_runtime_failures_raise_home_assistant_error_without_success_metadata(
        self,
    ):
        co = make_coordinator()
        hass, entry = co.hass, co.entry
        hass.config_entries.async_entries = Mock(return_value=[entry])
        callbacks = {}
        hass.services = NS(
            has_service=Mock(return_value=False),
            async_register=lambda domain, name, handler, **kw: callbacks.update(
                {name: handler}
            ),
        )
        await integration.async_setup(hass, {})
        for error in (
            SMAProtocolError("incomplete"),
            SMAAuthenticationError("auth"),
            SMATransportError("offline"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(
                    integration, "_async_import_periods", AsyncMock(side_effect=error)
                ),
                self.assertRaises(HomeAssistantError) as caught,
            ):
                await callbacks[integration.SERVICE_IMPORT_ARCHIVE](
                    NS(data={"days": 1})
                )
            self.assertNotIsInstance(caught.exception, ServiceValidationError)
            self.assertIs(caught.exception.__cause__, error)
            self.assertNotIn("last_archive_import", entry.options)
            hass.config_entries.async_update_entry.assert_not_called()
