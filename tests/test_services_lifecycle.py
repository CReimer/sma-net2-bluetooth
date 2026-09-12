"""Archive service validation, Recorder import and entry lifecycle contracts."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from homeassistant.exceptions import ServiceValidationError
from custom_components import sma_bluetooth as m
from custom_components.sma_bluetooth.protocol import SMAArchivePoint, SMAProtocolError
from tests.test_coordinator_platforms import make_coordinator


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.co = make_coordinator()
        self.hass = self.co.hass
        self.entry = self.co.entry
        self.hass.data[m.DOMAIN]["entry"] = self.co
        self.hass.config_entries.async_entries = Mock(
            return_value=[self.entry, NS(entry_id="unloaded")]
        )
        self.callbacks = {}

        def register(domain, service, handler, **kwargs):
            self.callbacks[service] = handler

        self.hass.services = NS(
            has_service=Mock(
                side_effect=lambda domain, service: service in self.callbacks
            ),
            async_register=Mock(side_effect=register),
        )
        self.assertTrue(await m.async_setup(self.hass, {}))
        self.assertTrue(await m.async_setup(self.hass, {}))
        self.assertEqual(self.hass.services.async_register.call_count, 2)
        self.start = datetime(2026, 1, 1, tzinfo=UTC)
        self.end = self.start + timedelta(minutes=5)

    async def test_archive_service_validates_ranges_and_entry(self):
        handler = self.callbacks[m.SERVICE_GET_ARCHIVE]
        cases = [
            (self.start.replace(tzinfo=None), self.end),
            (self.start, self.end.replace(tzinfo=None)),
            (self.end, self.start),
            (self.start + timedelta(seconds=1), self.end),
            (self.start, self.end + timedelta(days=m.MAX_ARCHIVE_DAYS + 1)),
        ]
        for start, end in cases:
            with (
                self.subTest(start=start, end=end),
                self.assertRaises(ServiceValidationError),
            ):
                await handler(NS(data={"start": start, "end": end}))
        with self.assertRaisesRegex(ServiceValidationError, "No loaded"):
            await handler(
                NS(
                    data={
                        "start": self.start,
                        "end": self.end,
                        "config_entry_id": "missing",
                    }
                )
            )
        with self.assertRaisesRegex(ServiceValidationError, "No loaded"):
            await self.callbacks[m.SERVICE_IMPORT_ARCHIVE](
                NS(data={"days": 1, "config_entry_id": "missing"})
            )

    async def test_archive_service_preserves_points_and_rejects_mismatch(self):
        timestamp = int(self.start.timestamp())
        self.co.async_read_archive = AsyncMock(
            return_value={
                "1": [
                    SMAArchivePoint(timestamp, 10, None),
                    SMAArchivePoint(timestamp, 11, 20),
                    SMAArchivePoint(timestamp - 300, 9, 1),
                ]
            }
        )
        handler = self.callbacks[m.SERVICE_GET_ARCHIVE]
        result = await handler(
            NS(data={"start": self.start, "end": self.end, "config_entry_id": "entry"})
        )
        plant = result["plants"]["entry"]
        self.assertTrue(plant["complete"])
        self.assertEqual(
            plant["inverters"]["1"]["points"],
            [{"timestamp": timestamp, "total_energy_kwh": 11, "power_w": 20}],
        )
        self.co.data["2"] = NS()
        with self.assertRaisesRegex(
            ServiceValidationError, "different archive timestamps"
        ):
            await handler(NS(data={"start": self.start, "end": self.end}))
        self.co.data.clear()
        with self.assertRaises(ServiceValidationError):
            await handler(NS(data={"start": self.start, "end": self.end}))
        self.co.async_read_archive.side_effect = SMAProtocolError("wire")
        with self.assertRaisesRegex(ServiceValidationError, "Could not read"):
            await handler(NS(data={"start": self.start, "end": self.end}))

    async def test_import_service_updates_metadata_after_success(self):
        with patch.object(
            m, "_async_import_periods", AsyncMock(return_value={"sensor.energy": 24})
        ) as importer:
            result = await self.callbacks[m.SERVICE_IMPORT_ARCHIVE](
                NS(data={"days": 1})
            )
            self.assertEqual(
                result, {"imported_hourly_statistics": {"sensor.energy": 24}}
            )
            self.assertTrue(importer.call_args.kwargs["require_complete"])
            self.assertEqual(self.entry.options["last_archive_import_days"], 1)

    async def test_import_rejects_incomplete_archive(self):
        self.co.async_read_archive = AsyncMock(return_value={"1": []})
        with self.assertRaisesRegex(SMAProtocolError, "Incomplete"):
            await m._async_import_periods(
                self.hass, self.co, [(0, 300)], require_complete=True
            )

    async def test_recorder_import_anchors_and_skips_unowned_and_current_hour(self):
        timestamp = int(self.start.timestamp())
        self.co.async_read_archive = AsyncMock(
            return_value={
                "1": [
                    SMAArchivePoint(timestamp, 10, None),
                    SMAArchivePoint(timestamp + 3600, 11, 10),
                ],
                "unowned": [SMAArchivePoint(timestamp, 99, None)],
            }
        )
        registry = NS(async_get_entity_id=Mock(return_value="sensor.energy"))
        recorder = NS(
            async_add_executor_job=AsyncMock(
                side_effect=[
                    {
                        "sensor.energy": [
                            {"state": None, "sum": 0},
                            {"state": 9, "sum": 2},
                        ]
                    },
                    {"sensor.energy": [{"state": 12, "sum": 5}]},
                ]
            ),
            async_block_till_done=AsyncMock(),
        )
        with (
            patch.object(m.er, "async_get", return_value=registry),
            patch.object(m, "get_instance", return_value=recorder),
            patch.object(m, "async_import_statistics") as importer,
        ):
            result = await m._async_import_periods(
                self.hass,
                self.co,
                [(timestamp, timestamp + 7200)],
                require_complete=False,
            )
            self.assertEqual(result, {"sensor.energy": 2})
            rows = importer.call_args.args[2]
            self.assertEqual([row["sum"] for row in rows], [3, 4])
            self.assertEqual(importer.call_args.args[1]["unit_of_measurement"], "kWh")
            recorder.async_block_till_done.assert_awaited_once()
            recorder.async_add_executor_job.side_effect = [{}, {}]
            await m._async_import_periods(
                self.hass, self.co, [], require_complete=False
            )
            self.assertEqual([row["sum"] for row in importer.call_args.args[2]], [0, 1])
            self.co.async_read_archive.return_value = {"1": [], "unowned": []}
            self.assertEqual(
                await m._async_import_periods(
                    self.hass, self.co, [], require_complete=False
                ),
                {},
            )
        self.assertEqual(m._managed_serials(NS(data={"fallback": None})), {"fallback"})


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconcile_already_done_unsafe_adjusted_and_failed(self):
        co = make_coordinator()
        co.async_sync_clock = AsyncMock()
        co.async_update_listeners = Mock()
        target = (m._local_today() - timedelta(days=1)).date().isoformat()
        co.entry.options["last_archive_reconcile_date"] = target
        await m._async_reconcile_previous_day(co.hass, co.entry, co)
        co.async_sync_clock.assert_not_awaited()
        for reason, adjusted, imported in [
            ("unsafe_difference", False, {"sensor": 24}),
            ("adjusted", True, {"sensor": 24}),
            ("ok", False, {}),
        ]:
            co.entry.options.clear()
            co.async_sync_clock.return_value = NS(
                reason=reason, adjusted=adjusted, difference_seconds=10
            )
            with patch.object(
                m, "_async_import_periods", AsyncMock(return_value=imported)
            ):
                await m._async_reconcile_previous_day(co.hass, co.entry, co)
            self.assertEqual(
                co.entry.options.get("last_archive_reconcile_date"),
                target if imported else None,
            )
            self.assertEqual(
                co.entry.options["last_clock_sync_at"] is not None, adjusted
            )

    async def test_schedule_guards_and_task_creation(self):
        co = make_coordinator()
        co.is_daylight = Mock(return_value=False)
        co.hass.async_create_task = Mock()
        m._schedule_archive_reconcile(co.hass, co.entry, co)
        co.is_daylight.return_value = True
        target = (m._local_today() - timedelta(days=1)).date().isoformat()
        co.entry.options["last_archive_reconcile_date"] = target
        m._schedule_archive_reconcile(co.hass, co.entry, co)
        co.entry.options.clear()
        co.archive_reconcile_task = NS(done=lambda: False)
        m._schedule_archive_reconcile(co.hass, co.entry, co)
        co.archive_reconcile_task = None
        co.archive_last_attempt = datetime.now(UTC)
        m._schedule_archive_reconcile(co.hass, co.entry, co)
        co.hass.async_create_task.assert_not_called()
        co.archive_last_attempt -= timedelta(hours=1)
        with patch.object(m, "_async_reconcile_previous_day", AsyncMock()) as reconcile:
            co.hass.async_create_task = lambda coro, name: asyncio.create_task(coro)
            m._schedule_archive_reconcile(co.hass, co.entry, co)
            await co.archive_reconcile_task
            reconcile.assert_awaited_once()

    async def test_setup_sunset_unload_and_rollback(self):
        co = make_coordinator()
        co.async_config_entry_first_refresh = AsyncMock()
        co.async_disconnect = AsyncMock()
        co.async_enter_night = AsyncMock()
        co.async_add_listener = Mock(return_value=Mock())
        co.entry.async_on_unload = Mock()
        tasks = []

        def background(hass, coro, name):
            task = asyncio.create_task(coro)
            tasks.append(task)
            return task

        co.entry.async_create_background_task = background
        entries = co.hass.config_entries
        entries.async_forward_entry_setups = AsyncMock()
        entries.async_unload_platforms = AsyncMock(return_value=False)
        with (
            patch.object(m, "SMABluetoothCoordinator", return_value=co),
            patch.object(m, "async_reconcile_ownership", return_value={"1"}),
            patch.object(m, "async_ensure_hub_device"),
            patch.object(m, "async_track_sunset") as sunset,
            patch.object(m, "_schedule_archive_reconcile") as schedule,
        ):
            self.assertTrue(await m.async_setup_entry(co.hass, co.entry))
            co.async_add_listener.call_args.args[0]()
            self.assertEqual(schedule.call_count, 2)
            sunset.call_args.args[1]()
            await asyncio.gather(*tasks)
            co.async_enter_night.assert_awaited_once()

            async def pending():
                await asyncio.Event().wait()

            co.archive_reconcile_task = asyncio.create_task(pending())
            await asyncio.sleep(0)
            self.assertFalse(await m.async_unload_entry(co.hass, co.entry))
            self.assertTrue(co.archive_reconcile_task.cancelled())
            self.assertIn("entry", co.hass.data[m.DOMAIN])
            entries.async_unload_platforms.return_value = True
            self.assertTrue(await m.async_unload_entry(co.hass, co.entry))
            self.assertNotIn("entry", co.hass.data[m.DOMAIN])
            entries.async_forward_entry_setups.side_effect = RuntimeError("platform")
            with self.assertRaises(RuntimeError):
                await m.async_setup_entry(co.hass, co.entry)
            self.assertNotIn("entry", co.hass.data[m.DOMAIN])

    async def test_migration_version_guards_and_removal(self):
        co = make_coordinator()
        for version, expected in [(3, False), (2, True)]:
            co.entry.version = version
            self.assertEqual(await m.async_migrate_entry(co.hass, co.entry), expected)
        for owner in (None, "new"):
            with (
                patch.object(m, "async_transfer_departing_entry", return_value=owner),
                patch.object(m, "async_refresh_overlap_issues") as refresh,
            ):
                await m.async_remove_entry(co.hass, co.entry)
                refresh.assert_called_once_with(co.hass)
