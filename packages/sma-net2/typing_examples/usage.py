"""Checked against source and each installed PEP-561 distribution; no I/O runs."""

from collections.abc import Sequence
from typing import assert_type

from sma_net2 import (
    MeasurementValue,
    SMAArchivePoint,
    SMAClassicClient,
    SMAClientDiagnostics,
    SMAClockSyncResult,
    SMAInverter,
)
from sma_net2.discovery import async_discover_sma_devices


async def check_public_api(client: SMAClassicClient) -> None:
    assert_type(client.diagnostics(), SMAClientDiagnostics)
    assert_type(client.diagnostics()["inverters"][0]["available_values"], list[str])
    async with client as opened:
        assert_type(opened, SMAClassicClient)
        inverters = await opened.async_query()
        assert_type(inverters, dict[str, SMAInverter])
        assert_type(inverters["1"].values.get("energy_total"), MeasurementValue)
        assert_type(inverters["1"].record_clock_difference, float | None)
        periods: Sequence[tuple[int, int]] = ((600, 900),)
        assert_type(
            await opened.async_read_archive(periods), dict[str, list[SMAArchivePoint]]
        )
        assert_type(await opened.async_sync_clock(3600, False), SMAClockSyncResult)
        await opened.async_start_session()
        assert_type(await opened.async_query_active(), dict[str, SMAInverter])
        assert_type(
            await opened.async_read_archive_active(periods),
            dict[str, list[SMAArchivePoint]],
        )
        assert_type(
            await opened.async_sync_clock_active(3600, False), SMAClockSyncResult
        )
        await opened.async_stop_session()
    assert_type(await async_discover_sma_devices(), dict[str, str])
