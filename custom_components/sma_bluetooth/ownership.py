"""Serial ownership, registry continuity, and Repairs issues for SMA Bluetooth."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.issue_registry import IssueSeverity

from .const import (
    CONF_CONNECTION_MODE,
    CONF_KNOWN_INVERTERS,
    CONF_LAST_DETECTED_NET_ID,
    CONF_LAST_DETECTED_SERIALS,
    CONF_NET_ID,
    CONF_PLANT_NAME,
    CONNECTION_MODE_AUTO,
    CONNECTION_MODE_NETWORK,
    CONNECTION_MODE_SINGLE,
    DOMAIN,
    EFFECTIVE_MODE_NETWORK,
    EFFECTIVE_MODE_SINGLE,
)

ISSUE_OVERLAP_PREFIX = "serial_overlap_"
ISSUE_NETID_PREFIXES = (
    "network_to_single_",
    "single_to_network_",
    "netid_changed_",
)


def _raw_known_inverters(entry: ConfigEntry) -> list[dict[str, Any]]:
    """Return the entry's authoritative identity snapshot."""
    raw = entry.options.get(
        CONF_KNOWN_INVERTERS, entry.data.get(CONF_KNOWN_INVERTERS, [])
    )
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def entry_known_serials(entry: ConfigEntry) -> set[str]:
    """Return the serials currently claimed by one config entry."""
    return {
        str(item["serial"])
        for item in _raw_known_inverters(entry)
        if isinstance(item.get("serial"), (str, int))
    }


def entries_claiming_serials(
    hass: HomeAssistant,
    serials: Iterable[str],
    *,
    exclude_entry_id: str | None = None,
) -> dict[str, list[ConfigEntry]]:
    """Return configured entries claiming each requested serial."""
    requested = set(serials)
    claims = {serial: [] for serial in requested}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude_entry_id:
            continue
        for serial in requested & entry_known_serials(entry):
            claims[serial].append(entry)
    return claims


def _effective_mode(mode: str, net_id: int) -> str:
    if mode == CONNECTION_MODE_AUTO:
        return EFFECTIVE_MODE_SINGLE if net_id == 1 else EFFECTIVE_MODE_NETWORK
    if mode == CONNECTION_MODE_SINGLE:
        return EFFECTIVE_MODE_SINGLE
    return EFFECTIVE_MODE_NETWORK


def _entry_sort_key(entry: ConfigEntry) -> tuple[str, str]:
    return (str(getattr(entry, "created_at", "")), entry.entry_id)


def _entity_matches_serial(unique_id: str, serial: str) -> bool:
    return unique_id.startswith(f"sma_bluetooth_{serial}_")


def _move_serial_registries(
    hass: HomeAssistant, serial: str, owner_entry_id: str
) -> None:
    """Move existing registry rows without changing IDs or unique IDs."""
    entity_registry = er.async_get(hass)
    for entity in list(entity_registry.entities.values()):
        if entity.platform != DOMAIN or not _entity_matches_serial(
            entity.unique_id, serial
        ):
            continue
        if entity.config_entry_id != owner_entry_id:
            entity_registry.async_update_entity(
                entity.entity_id, config_entry_id=owner_entry_id
            )

    device_registry = dr.async_get(hass)
    devices = device_registry.async_get_devices(identifiers={(DOMAIN, serial)})
    device = next(
        (
            candidate
            for candidate in devices
            if candidate.config_entry_id != owner_entry_id
        ),
        None,
    )
    if device is not None:
        device_registry.async_update_device(
            device.id, new_config_entry_id=owner_entry_id
        )


def _sync_overlap_issues(hass: HomeAssistant) -> None:
    """Create exact overlap issues and remove issues that are now resolved."""
    claims: dict[str, list[ConfigEntry]] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        for serial in entry_known_serials(entry):
            claims.setdefault(serial, []).append(entry)

    expected_issue_ids: set[str] = set()
    for serial, entries in claims.items():
        if len(entries) < 2:
            continue
        issue_id = f"{ISSUE_OVERLAP_PREFIX}{serial}"
        expected_issue_ids.add(issue_id)
        titles = ", ".join(
            entry.title for entry in sorted(entries, key=_entry_sort_key)
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            data={
                "serial": serial,
                "entry_ids": ",".join(entry.entry_id for entry in entries),
                "entries": titles,
            },
            is_fixable=True,
            is_persistent=True,
            severity=IssueSeverity.WARNING,
            translation_key="serial_overlap",
            translation_placeholders={"serial": serial, "entries": titles},
        )

    issue_registry = ir.async_get(hass)
    for domain, issue_id in list(issue_registry.issues):
        if (
            domain == DOMAIN
            and issue_id.startswith(ISSUE_OVERLAP_PREFIX)
            and issue_id not in expected_issue_ids
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def async_refresh_overlap_issues(hass: HomeAssistant) -> None:
    """Refresh overlap Repairs after config-entry changes."""
    _sync_overlap_issues(hass)


def async_reconcile_ownership(
    hass: HomeAssistant, entry: ConfigEntry, serials: Iterable[str]
) -> set[str]:
    """Assign serials deterministically while preserving historical registry rows."""
    requested = set(serials)
    all_entries = hass.config_entries.async_entries(DOMAIN)
    entries_by_id = {candidate.entry_id: candidate for candidate in all_entries}
    claims = entries_claiming_serials(hass, requested)
    owned: set[str] = set()
    entity_registry = er.async_get(hass)

    for serial in requested:
        serial_claims = claims[serial]
        if entry.entry_id not in {candidate.entry_id for candidate in serial_claims}:
            serial_claims.append(entry)

        registry_owners = {
            entity.config_entry_id
            for entity in entity_registry.entities.values()
            if entity.platform == DOMAIN
            and _entity_matches_serial(entity.unique_id, serial)
            and entity.config_entry_id is not None
        }
        valid_registry_owners = [
            owner_id
            for owner_id in registry_owners
            if owner_id in entries_by_id
            and serial in entry_known_serials(entries_by_id[owner_id])
        ]
        if valid_registry_owners:
            owner_id = sorted(valid_registry_owners)[0]
        else:
            owner_id = min(serial_claims, key=_entry_sort_key).entry_id

        if owner_id == entry.entry_id:
            owned.add(serial)
            if len(serial_claims) == 1:
                _move_serial_registries(hass, serial, entry.entry_id)

    _sync_overlap_issues(hass)
    return owned


def async_note_netid_change(
    hass: HomeAssistant,
    entry: ConfigEntry,
    detected_net_id: int,
    detected_serials: Iterable[str],
) -> None:
    """Persist detection evidence and raise the appropriate guided repair."""
    old_net_id = entry.data.get(CONF_NET_ID)
    serials = sorted(set(detected_serials))
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_LAST_DETECTED_NET_ID: detected_net_id,
            CONF_LAST_DETECTED_SERIALS: serials,
        },
    )
    configured_mode = entry.data.get(CONF_CONNECTION_MODE, CONNECTION_MODE_AUTO)
    old_effective = (
        _effective_mode(configured_mode, old_net_id)
        if isinstance(old_net_id, int)
        else (
            EFFECTIVE_MODE_NETWORK
            if configured_mode == CONNECTION_MODE_NETWORK
            else EFFECTIVE_MODE_SINGLE
        )
    )
    new_effective = _effective_mode(configured_mode, detected_net_id)
    if old_effective == EFFECTIVE_MODE_NETWORK and detected_net_id == 1:
        translation_key = "network_to_single"
    elif (
        old_effective == EFFECTIVE_MODE_SINGLE
        and new_effective == EFFECTIVE_MODE_NETWORK
    ):
        translation_key = "single_to_network"
    else:
        translation_key = "netid_changed"

    issue_id = f"{translation_key}_{entry.entry_id}"
    placeholders = {
        "entry": entry.title,
        "old_net_id": f"{old_net_id:X}" if isinstance(old_net_id, int) else "?",
        "new_net_id": f"{detected_net_id:X}",
        "serials": ", ".join(serials) or "unknown",
    }
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        data={
            "entry_id": entry.entry_id,
            "entry": entry.title,
            "old_net_id": old_net_id if isinstance(old_net_id, int) else -1,
            "new_net_id": detected_net_id,
            "serials": ",".join(serials),
        },
        is_fixable=True,
        is_persistent=True,
        severity=IssueSeverity.WARNING,
        translation_key=translation_key,
        translation_placeholders=placeholders,
    )


def async_clear_netid_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Delete all resolved NetID transition issues for one entry."""
    for prefix in ISSUE_NETID_PREFIXES:
        ir.async_delete_issue(hass, DOMAIN, f"{prefix}{entry_id}")


def async_transfer_departing_entry(
    hass: HomeAssistant, departing: ConfigEntry
) -> str | None:
    """Preserve serial registries when a guided single-to-network entry is removed."""
    detected_net_id = departing.options.get(CONF_LAST_DETECTED_NET_ID)
    if not isinstance(detected_net_id, int) or detected_net_id <= 1:
        return None

    plant_name = departing.data.get(CONF_PLANT_NAME)
    password = departing.data.get("password")
    candidates = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.options.get(CONF_LAST_DETECTED_NET_ID) == detected_net_id
        and entry.data.get(CONF_PLANT_NAME) == plant_name
        and entry.data.get("password") == password
    ]
    if len(candidates) != 1:
        return None
    owner = candidates[0]

    departing_snapshot = _raw_known_inverters(departing)
    owner_snapshot = _raw_known_inverters(owner)
    merged = {str(item.get("serial")): item for item in owner_snapshot}
    for item in departing_snapshot:
        serial = item.get("serial")
        if isinstance(serial, (str, int)):
            merged[str(serial)] = item
            _move_serial_registries(hass, str(serial), owner.entry_id)
    hass.config_entries.async_update_entry(
        owner,
        options={
            **owner.options,
            CONF_KNOWN_INVERTERS: [merged[key] for key in sorted(merged)],
        },
    )
    return owner.entry_id
