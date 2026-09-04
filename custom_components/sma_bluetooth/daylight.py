"""Independent daylight scheduling for SMA Bluetooth communication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.const import SUN_EVENT_SUNRISE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import sun
from homeassistant.util import dt as dt_util

MINIMUM_SLEEP_INTERVAL = timedelta(seconds=60)
POLAR_NIGHT_RECHECK_INTERVAL = timedelta(hours=12)


@dataclass(frozen=True, slots=True)
class DaylightSchedule:
    """Whether communication is allowed and when to evaluate again."""

    active: bool
    next_interval: timedelta | None = None


def daylight_schedule(
    hass: HomeAssistant, now: datetime | None = None
) -> DaylightSchedule:
    """Return a schedule derived only from Home Assistant's location data."""
    current = now or dt_util.utcnow()
    if sun.is_up(hass, current):
        return DaylightSchedule(True)

    try:
        sunrise = sun.get_astral_event_next(
            hass, SUN_EVENT_SUNRISE, current
        )
    except ValueError:
        return DaylightSchedule(False, POLAR_NIGHT_RECHECK_INTERVAL)

    return DaylightSchedule(
        False,
        max(sunrise - current, MINIMUM_SLEEP_INTERVAL),
    )
