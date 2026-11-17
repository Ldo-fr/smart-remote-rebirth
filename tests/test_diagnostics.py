"""Tests unitaires pour le module diagnostics de Smart Remote Rebirth (Jalon v0.5)."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock  # noqa: F401
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.smart_remote_rebirth import SmartRemoteData
from custom_components.smart_remote_rebirth.const import (
    CONF_CLIENT_PRIVATE_KEY,
    CONF_CLIENT_PUBLIC_KEY,
    CONF_CONTROLLER_ID,
    CONF_ENTITIES,
    CONF_HOST,
    CONF_PORT,
    CONF_REMOTE_ID,
    CONF_REMOTE_PUBLIC_KEY,
    CONF_SERIAL_NUMBER,
    DOMAIN,
)
from custom_components.smart_remote_rebirth.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)


class TestSmartRemoteDiagnostics(unittest.IsolatedAsyncioTestCase):
    """Vérifie la génération et l'anonymisation des diagnostics Home Assistant."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.entry = ConfigEntry(
            entry_id="entry_diag_01",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.50",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "a06fe048b48b",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-secret-uuid",
                CONF_CLIENT_PRIVATE_KEY: "deadbeef" * 8,
                CONF_CLIENT_PUBLIC_KEY: "cafebabe" * 8,
                CONF_REMOTE_PUBLIC_KEY: "11223344" * 8,
            },
            options={
                CONF_ENTITIES: ["light.salon", "switch.cuisine"],
            },
            unique_id="a06fe048b48b",
        )

        self.client = MagicMock()
        self.client.is_verified = True
        self.client.session_generation = 3

        self.coordinator = MagicMock()
        self.coordinator.last_update_success = True
        self.coordinator.is_connected = True
        self.coordinator.consecutive_failures = 0
        self.coordinator.firmware_version = "v0.76.4-19.6.3-ae"
        self.coordinator.remote_id = "5726"
        self.coordinator.data = {
            "connected": True,
            "serial_number": "a06fe048b48b",
            "mac_address": "A0:6F:E0:48:B4:8B",
            "firmware_version": "v0.76.4-19.6.3-ae",
            "raw_settings": {
                "ip": "192.168.1.50",
                "ssid": "MyHomeWiFi",
                "carousel_order": [{"id": "sevenhugs.control.webhooks:1", "name": "HA"}],
            },
        }

        self.runtime_data = SmartRemoteData(
            client=self.client,
            session=MagicMock(),
            coordinator=self.coordinator,
            webhook_id="smart_remote_entry_diag_01",
        )
        self.hass.data[DOMAIN] = {self.entry.entry_id: self.runtime_data}
        self.entry.runtime_data = self.runtime_data

    async def test_diagnostics_redacts_all_sensitive_fields(self) -> None:
        """Vérifie que les clés cryptographiques, IP, MAC, SSID et numéro de série sont anonymisés."""
        diag = await async_get_config_entry_diagnostics(self.hass, self.entry)

        self.assertIn("entry", diag)
        self.assertIn("coordinator", diag)
        self.assertIn("client", diag)

        entry_data = diag["entry"]["data"]
        for sensitive_key in (
            CONF_HOST,
            CONF_SERIAL_NUMBER,
            CONF_CONTROLLER_ID,
            CONF_CLIENT_PRIVATE_KEY,
            CONF_CLIENT_PUBLIC_KEY,
            CONF_REMOTE_PUBLIC_KEY,
        ):
            self.assertEqual(entry_data[sensitive_key], "**REDACTED**")
            self.assertNotIn("deadbeef", str(diag))
            self.assertNotIn("192.168.1.50", str(diag))
            self.assertNotIn("a06fe048b48b", str(diag))

        # Les données non sensibles restent accessibles pour le support
        self.assertEqual(entry_data[CONF_PORT], 80)
        self.assertEqual(entry_data[CONF_REMOTE_ID], "5726")
        self.assertEqual(
            diag["entry"]["options"][CONF_ENTITIES],
            ["light.salon", "switch.cuisine"],
        )

        coord_data = diag["coordinator"]["data"]
        self.assertEqual(coord_data["serial_number"], "**REDACTED**")
        self.assertEqual(coord_data["mac_address"], "**REDACTED**")
        self.assertEqual(coord_data["raw_settings"]["ip"], "**REDACTED**")
        self.assertEqual(coord_data["raw_settings"]["ssid"], "**REDACTED**")
        self.assertEqual(diag["coordinator"]["firmware_version"], "v0.76.4-19.6.3-ae")
        self.assertTrue(diag["client"]["is_verified"])
        self.assertEqual(diag["client"]["session_generation"], 3)

    async def test_diagnostics_tolerates_missing_runtime_data(self) -> None:
        """Vérifie que le diagnostic fonctionne même si runtime_data est absent."""
        self.entry.runtime_data = None
        self.hass.data[DOMAIN] = {}

        diag = await async_get_config_entry_diagnostics(self.hass, self.entry)
        self.assertEqual(diag["coordinator"], {})
        self.assertEqual(diag["client"], {})
        self.assertEqual(diag["entry"]["data"][CONF_HOST], "**REDACTED**")
        self.assertTrue(len(TO_REDACT) >= 10)


if __name__ == "__main__":
    unittest.main()
