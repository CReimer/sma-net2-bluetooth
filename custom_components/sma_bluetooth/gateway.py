"""Adapter-wide RFCOMM session serialization for SMA Bluetooth."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
import logging
from time import monotonic
from typing import TypeVar

from bluetooth_adapters import ADAPTER_ADDRESS, get_adapters
from bluetooth_auto_recovery import recover_adapter
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.util import dt as dt_util

from .const import (
    DATA_ADAPTER_GATE,
    DOMAIN,
    RFCOMM_RECOVERY_COOLDOWN,
    RFCOMM_RECOVERY_FAILURES,
    RFCOMM_RELEASE_DELAY,
    RFCOMM_SESSION_ATTEMPTS,
)
from .daylight import daylight_schedule
from .protocol import (
    SMAAuthenticationError,
    SMAClassicClient,
    SMAConfigurationError,
    SMANetworkModeError,
    SMAProtocolError,
    SMATransportError,
)

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")
ISSUE_BLUETOOTH_RECOVERY_FAILED = "bluetooth_recovery_failed"


class SMADaylightError(SMAProtocolError):
    """An SMA operation was refused outside daylight hours."""


class SMAAdapterGate:
    """Own the one Bluetooth Classic adapter session gate for Home Assistant."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last_connection_closed: float | None = None
        self.active_address: str | None = None
        self.consecutive_transport_failures = 0
        self.last_recovery_attempt: float | None = None
        self.last_recovery_at: datetime | None = None
        self.recovery_count = 0
        self.recovery_issue_open = False

    @staticmethod
    def _ensure_daylight(hass: HomeAssistant) -> None:
        if not daylight_schedule(hass).active:
            raise SMADaylightError("SMA communication is paused until sunrise")

    async def _async_release_pause(self, hass: HomeAssistant) -> None:
        """Honor BlueZ's adapter-wide RFCOMM release interval."""
        if self.last_connection_closed is None:
            return
        remaining = RFCOMM_RELEASE_DELAY - (monotonic() - self.last_connection_closed)
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._ensure_daylight(hass)

    def _clear_recovery_failure(self, hass: HomeAssistant) -> None:
        """Clear failure state and a previously raised Repairs issue."""
        self.consecutive_transport_failures = 0
        if not self.recovery_issue_open:
            return
        ir.async_delete_issue(hass, DOMAIN, ISSUE_BLUETOOTH_RECOVERY_FAILED)
        self.recovery_issue_open = False

    def _raise_recovery_issue(
        self, hass: HomeAssistant, address: str, error: SMAProtocolError
    ) -> None:
        """Expose a persistent repair when automatic transport recovery failed."""
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_BLUETOOTH_RECOVERY_FAILED,
            data={"address": address, "error": str(error)},
            is_fixable=False,
            is_persistent=True,
            severity=IssueSeverity.ERROR,
            translation_key="bluetooth_recovery_failed",
            translation_placeholders={"address": address},
        )
        self.recovery_issue_open = True

    async def _async_recover_adapter(self) -> bool:
        """Power-cycle the default local adapter without escalating to USB reset."""
        self.last_recovery_attempt = monotonic()
        self.last_recovery_at = dt_util.utcnow()
        self.recovery_count += 1
        adapters = get_adapters()
        await adapters.refresh()
        adapter_name = adapters.default_adapter
        details = adapters.adapters.get(adapter_name)
        address = details.get(ADAPTER_ADDRESS) if details else None
        if (
            not isinstance(adapter_name, str)
            or not adapter_name.startswith("hci")
            or not adapter_name.removeprefix("hci").isdigit()
            or not isinstance(address, str)
        ):
            _LOGGER.error("Could not resolve the default Bluetooth adapter")
            return False

        _LOGGER.warning(
            "Power-cycling Bluetooth adapter %s after %s consecutive failed "
            "SMA transport operations",
            adapter_name,
            self.consecutive_transport_failures,
        )
        recovered = await recover_adapter(
            int(adapter_name.removeprefix("hci")), address, gone_silent=False
        )
        if recovered:
            _LOGGER.info("Bluetooth adapter %s recovery completed", adapter_name)
        else:
            _LOGGER.error("Bluetooth adapter %s recovery failed", adapter_name)
        return recovered

    async def _async_execute_once(
        self,
        address: str,
        password: str,
        connection_mode: str,
        operation: Callable[[SMAClassicClient], Awaitable[_T]],
        timeout: float | None,
    ) -> _T:
        """Execute one bounded session and retain transport error semantics."""
        try:
            coroutine = self._async_run_once(
                address, password, connection_mode, operation
            )
            if timeout is None:
                return await coroutine
            async with asyncio.timeout(timeout):
                return await coroutine
        except TimeoutError as err:
            error = SMATransportError(
                "SMA session timed out"
                if timeout is None
                else f"SMA session exceeded {timeout:g} seconds"
            )
            error.__cause__ = err
            raise error
        except OSError as err:
            raise SMATransportError(f"SMA transport failed: {err}") from err

    async def _async_run_once(
        self,
        address: str,
        password: str,
        connection_mode: str,
        operation: Callable[[SMAClassicClient], Awaitable[_T]],
    ) -> _T:
        """Connect, authenticate, run exactly one operation, and disconnect."""
        client = SMAClassicClient(
            address,
            password,
            connection_mode=connection_mode,
        )
        self.active_address = address
        try:
            async with client:
                try:
                    await client.async_start_session()
                    return await operation(client)
                finally:
                    try:
                        await client.async_stop_session()
                    except (OSError, SMAProtocolError) as err:
                        _LOGGER.debug("Could not log off SMA session cleanly: %s", err)
        finally:
            self.active_address = None
            self.last_connection_closed = monotonic()

    async def async_run(
        self,
        hass: HomeAssistant,
        address: str,
        password: str,
        connection_mode: str,
        operation: Callable[[SMAClassicClient], Awaitable[_T]],
        *,
        timeout: float | None = None,
        attempts: int = RFCOMM_SESSION_ATTEMPTS,
    ) -> _T:
        """Run one operation with adapter-wide daylight, delay, and retries."""
        # Checking both sides of the lock wait prevents a queued daytime call
        # from opening RFCOMM after sunset.
        self._ensure_daylight(hass)
        async with self.lock:
            self._ensure_daylight(hass)
            last_error: SMAProtocolError | None = None
            for attempt in range(1, attempts + 1):
                await self._async_release_pause(hass)
                try:
                    result = await self._async_execute_once(
                        address, password, connection_mode, operation, timeout
                    )
                except SMAAuthenticationError:
                    raise
                except (SMAConfigurationError, SMANetworkModeError):
                    raise
                except SMADaylightError:
                    raise
                except SMAProtocolError as err:
                    last_error = err
                else:
                    self._clear_recovery_failure(hass)
                    return result

                if attempt < attempts:
                    _LOGGER.debug(
                        "Retrying adapter-wide SMA RFCOMM session after attempt "
                        "%s/%s: %s",
                        attempt,
                        attempts,
                        last_error,
                    )

            assert last_error is not None
            if not isinstance(last_error, SMATransportError):
                self.consecutive_transport_failures = 0
                raise last_error

            self.consecutive_transport_failures += 1
            if self.consecutive_transport_failures < RFCOMM_RECOVERY_FAILURES:
                raise last_error

            since_recovery = (
                None
                if self.last_recovery_attempt is None
                else monotonic() - self.last_recovery_attempt
            )
            if since_recovery is not None and since_recovery < RFCOMM_RECOVERY_COOLDOWN:
                self._raise_recovery_issue(hass, address, last_error)
                raise last_error

            try:
                await self._async_recover_adapter()
                self._ensure_daylight(hass)
                await self._async_release_pause(hass)
                result = await self._async_execute_once(
                    address, password, connection_mode, operation, timeout
                )
            except (SMAAuthenticationError, SMAConfigurationError, SMANetworkModeError):
                raise
            except SMADaylightError:
                raise
            except SMATransportError as err:
                self._raise_recovery_issue(hass, address, err)
                raise
            except SMAProtocolError:
                self._clear_recovery_failure(hass)
                raise
            except Exception as err:  # Recovery must not break normal HA retries.
                _LOGGER.exception("Unexpected Bluetooth adapter recovery failure")
                recovery_error = SMATransportError(
                    f"Bluetooth adapter recovery failed: {err}"
                )
                self._raise_recovery_issue(hass, address, recovery_error)
                raise recovery_error from err

            self._clear_recovery_failure(hass)
            return result


def async_get_adapter_gate(hass: HomeAssistant) -> SMAAdapterGate:
    """Return the one adapter gate shared by runtime entries and config flows."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    gate = domain_data.get(DATA_ADAPTER_GATE)
    if isinstance(gate, SMAAdapterGate):
        return gate
    gate = SMAAdapterGate()
    domain_data[DATA_ADAPTER_GATE] = gate
    return gate
