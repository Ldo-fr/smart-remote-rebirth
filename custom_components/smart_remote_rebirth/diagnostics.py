"""Support de diagnostic Home Assistant pour Smart Remote Rebirth (SR1A)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CLIENT_PRIVATE_KEY,
    CONF_CLIENT_PUBLIC_KEY,
    CONF_CONTROLLER_ID,
    CONF_HOST,
    CONF_KEYS,
    CONF_REMOTE_PUBLIC_KEY,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_READ_KEY,
    CONF_SESSION_WRITE_KEY,
    DOMAIN,
)

try:
    from homeassistant.components.diagnostics import async_redact_data
except ImportError:
    REDACTED = "**REDACTED**"

    def async_redact_data(data: Any, to_redact: set[str] | list[str]) -> Any:
        """Fonction de repli d'anonymisation récursive hors environnement Home Assistant complet."""
        if isinstance(data, Mapping):
            redacted: dict[str, Any] = {}
            for key, val in data.items():
                if key in to_redact:
                    redacted[key] = REDACTED
                else:
                    redacted[key] = async_redact_data(val, to_redact)
            return redacted
        if isinstance(data, list):
            return [async_redact_data(item, to_redact) for item in data]
        return data

TO_REDACT: set[str] = {
    CONF_CLIENT_PRIVATE_KEY,
    CONF_CLIENT_PUBLIC_KEY,
    CONF_CONTROLLER_ID,
    CONF_HOST,
    CONF_KEYS,
    CONF_REMOTE_PUBLIC_KEY,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_READ_KEY,
    CONF_SESSION_WRITE_KEY,
    "bssid",
    "ip",
    "mac_address",
    "password",
    "pin",
    "secret",
    "ssid",
    "token",
    "unique_id",
    "webhook_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Retourne les données de diagnostic anonymisées pour une ConfigEntry Smart Remote."""
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None and hasattr(hass, "data") and isinstance(hass.data, Mapping):
        domain_data = hass.data.get(DOMAIN, {})
        if isinstance(domain_data, Mapping):
            runtime_data = domain_data.get(entry.entry_id)

    coordinator = None
    client = None
    if runtime_data is not None:
        coordinator = getattr(runtime_data, "coordinator", None) or (
            runtime_data.get("coordinator") if isinstance(runtime_data, Mapping) else None
        )
        client = getattr(runtime_data, "client", None) or (
            runtime_data.get("client") if isinstance(runtime_data, Mapping) else None
        )

    coord_info: dict[str, Any] = {}
    carousel_summary: list[dict[str, Any]] = []
    if coordinator is not None:
        raw_coord_data = getattr(coordinator, "data", None) or getattr(
            coordinator, "cached_data", {}
        )
        if isinstance(raw_coord_data, Mapping):
            raw_settings = raw_coord_data.get("raw_settings")
            if isinstance(raw_settings, Mapping) and isinstance(
                raw_settings.get("carousel_order"), list
            ):
                carousel_summary = [
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "category": item.get("category"),
                        "ui_package_id": item.get("ui_package_id"),
                        "hidden": item.get("hidden", False),
                    }
                    for item in raw_settings["carousel_order"]
                    if isinstance(item, Mapping)
                ]
        coord_info = {
            "last_update_success": getattr(coordinator, "last_update_success", None),
            "is_connected": getattr(coordinator, "is_connected", None),
            "consecutive_failures": getattr(coordinator, "consecutive_failures", 0),
            "firmware_version": getattr(coordinator, "firmware_version", None),
            "remote_id": getattr(coordinator, "remote_id", None),
            "carousel_order": carousel_summary,
            "data": async_redact_data(raw_coord_data, TO_REDACT),
        }

    client_info: dict[str, Any] = {}
    if client is not None:
        client_info = {
            "is_verified": getattr(client, "is_verified", None),
            "session_generation": getattr(client, "session_generation", None),
        }

    entry_data = dict(entry.data) if isinstance(getattr(entry, "data", None), Mapping) else {}
    entry_options = (
        dict(entry.options) if isinstance(getattr(entry, "options", None), Mapping) else {}
    )

    return {
        "entry": {
            "title": getattr(entry, "title", ""),
            "data": async_redact_data(entry_data, TO_REDACT),
            "options": async_redact_data(entry_options, TO_REDACT),
        },
        "coordinator": coord_info,
        "client": client_info,
    }
