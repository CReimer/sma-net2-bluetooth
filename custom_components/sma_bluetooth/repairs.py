"""Guided Repairs flows for SMA topology and ownership changes."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


class SMAConfigurationRepairFlow(RepairsFlow):
    """Keep the repair open until the user completes the described safe steps."""

    def __init__(self, issue_id: str, data: dict[str, Any]) -> None:
        self._issue_id = issue_id
        self._placeholders = {
            key: str(value) for key, value in data.items() if value is not None
        }

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Explain the exact reconfigure/remove order and verify completion."""
        errors: dict[str, str] = {}
        if user_input is not None:
            issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self._issue_id)
            if issue is None:
                return self.async_create_entry(title="", data={})
            errors["base"] = "still_present"
        return self.async_show_form(
            step_id="init",
            errors=errors,
            description_placeholders=self._placeholders,
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create the matching guided SMA repair flow."""
    return SMAConfigurationRepairFlow(issue_id, data or {})
