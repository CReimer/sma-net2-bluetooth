"""Helpers for reconciling SMA archive data with Home Assistant statistics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .protocol import SMAArchivePoint


def completed_day_periods(
    local_today: datetime, days: int
) -> list[tuple[int, int]]:
    """Return exact local-midnight periods for completed days, oldest first."""
    if local_today.tzinfo is None or local_today.utcoffset() is None:
        raise ValueError("local_today must be timezone-aware")

    periods: list[tuple[int, int]] = []
    for offset in range(days, 0, -1):
        start = local_today - timedelta(days=offset)
        end = start + timedelta(days=1)
        periods.append((int(start.timestamp()), int(end.timestamp())))
    return periods


def hourly_last_values(points: list[SMAArchivePoint]) -> dict[datetime, float]:
    """Keep the final official cumulative meter value in every UTC hour."""
    hourly: dict[datetime, float] = {}
    for point in points:
        hour = datetime.fromtimestamp(point.timestamp, timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        hourly[hour] = point.total_energy_kwh
    return hourly


def timestamp_series_complete(
    timestamp_sets: list[set[int]],
    start_timestamp: int,
    end_timestamp: int,
    *,
    require_all_slots: bool,
) -> bool:
    """Validate matching archive timestamps and, for past ranges, every slot."""
    if not timestamp_sets or not timestamp_sets[0]:
        return False
    reference = timestamp_sets[0]
    if any(timestamps != reference for timestamps in timestamp_sets[1:]):
        return False
    if require_all_slots:
        return reference == set(range(start_timestamp, end_timestamp, 300))
    return True


def cumulative_statistic_sums(
    states: list[float],
    previous: tuple[float, float] | None = None,
    following: tuple[float, float] | None = None,
) -> list[float]:
    """Build continuous Recorder sums for cumulative meter states.

    The tuples contain ``(state, sum)``. A preceding statistic is preferred as
    the anchor. If history starts inside the imported range, a following
    statistic keeps the other boundary continuous instead.
    """
    if not states:
        return []

    def increase(old: float, new: float) -> float:
        return new - old if new >= old else new

    increments = [0.0]
    for old, new in zip(states, states[1:]):
        increments.append(increments[-1] + increase(old, new))

    if previous is not None:
        first_sum = previous[1] + increase(previous[0], states[0])
    elif following is not None:
        first_sum = following[1] - (
            increments[-1] + increase(states[-1], following[0])
        )
    else:
        first_sum = 0.0

    return [first_sum + increment for increment in increments]
