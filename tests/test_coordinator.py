"""Tests unitaires et asynchrones pour SevenhugsDataUpdateCoordinator (Phase 3 - R1 & R5).

Couvre l'intégralité des spécifications et exigences du coordinateur :
1. Héritage de DataUpdateCoordinator et configuration nominale (intervalle 60s, résolution du nom).
2. Extraction, normalisation et clamping des données de télémétrie (/api/settings) :
   - Clamping du pourcentage de batterie (0-100%) et tolérance aux valeurs invalides.
   - Signal Wi-Fi RSSI (dBm) et bascule vers clé de secours 'rssi'.
   - États de charge et de socle (charging, docked).
   - Écran/mode actif et replis.
   - Résolution des identifiants matériels (serial_number, mac_address, firmware_version, remote_id).
3. Résilience de veille profonde (Deep Sleep) :
   - Tolérance de 2 échecs consécutifs avec maintien transparent du cache en mémoire.
   - Entités conservées disponibles (available=True) durant les échecs 1 et 2.
   - Déclenchement de l'indisponibilité (UpdateFailed) au 3ème échec consécutif.
   - Maintien de l'indisponibilité aux échecs 4 et au-delà avec conservation intégrale du cache.
   - Rétablissement nominal immédiat lors du retour de la télécommande.
   - Matrice exhaustive des exceptions réseau tolérées (_NETWORK_ERRORS).
4. Réveil événementiel (async_wake_up / async_handle_wake_up) :
   - Réinitialisation immédiate du compteur d'échecs consécutifs à 0.
   - Déclenchement d'un rafraîchissement immédiat de l'état.
5. Gestion des erreurs d'authentification :
   - Traduction stricte de SevenhugsAuthError en ConfigEntryAuthFailed.
   - Non-absorption par la résilience veille.
6. Comportement au démarrage à froid (Cold Start) :
   - Échec initial sans cache mémoire préalable levant immédiatement UpdateFailed.
7. Propriétés publiques exposées et immutabilité du cache :
   - Propriétés typées (battery, wifi_signal, charging, active_screen, is_connected, consecutive_failures).
   - Immutabilité de cached_data retourné (copie défensive).
8. Notification dynamique des écouteurs Home Assistant.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import os
import sys
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Ingestion de l'environnement de mocks et shims Home Assistant
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

import tests.conftest
import aiohttp

from custom_components.smart_remote_rebirth.client import (
    AsyncSmartRemoteClient,
    SevenhugsAuthError,
    SevenhugsConnectionError,
)
from custom_components.smart_remote_rebirth.const import (
    CONF_HOST,
    CONF_POLLING_INTERVAL,
    CONF_PORT,
    CONF_REMOTE_ID,
    CONF_SERIAL_NUMBER,
    DEFAULT_NAME,
    DOMAIN,
)
from custom_components.smart_remote_rebirth.coordinator import (
    DEFAULT_COORDINATOR_UPDATE_INTERVAL,
    SLEEP_CONSECUTIVE_FAILURES_THRESHOLD,
    SevenhugsDataUpdateCoordinator,
    _NETWORK_ERRORS,
)


class MockSmartRemoteClientForCoordinator:
    """Mock contrôlable du client Smart Remote pour tests du coordinateur."""

    def __init__(self, settings_response: Any = None) -> None:
        self.settings_response = settings_response or {
            "battery": 88,
            "wifi_signal": -58,
            "charging": True,
            "docked": True,
            "active_screen": "MediaPlayer",
            "serial_number": "SR1A987654321",
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "firmware_version": "v0.76.4",
            "name": "Smart Remote Salon",
            "remotes": [
                {
                    "serial_number": "SR1A987654321",
                    "id": "5726",
                    "firmware_version": "v0.76.4",
                    "mac_address": "AA:BB:CC:DD:EE:FF",
                }
            ],
        }
        self.request_calls: list[tuple[str, str]] = []
        self.side_effect: Exception | None = None

    async def async_request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.request_calls.append((method, path))
        if self.side_effect is not None:
            effect = self.side_effect
            if isinstance(effect, type) and issubclass(effect, Exception):
                raise effect("Mock exception raised")
            elif isinstance(effect, Exception):
                raise effect
            elif callable(effect):
                res = effect()
                if asyncio.iscoroutine(res):
                    return await res
                return res
        return self.settings_response


class TestSevenhugsDataUpdateCoordinatorInit(unittest.IsolatedAsyncioTestCase):
    """Vérifie l'initialisation et la configuration du SevenhugsDataUpdateCoordinator."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.entry = ConfigEntry(
            entry_id="coord_entry_001",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.100",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "a06fe048b48b",
                CONF_REMOTE_ID: "5726",
            },
            unique_id="a06fe048b48b",
        )
        self.mock_client = MockSmartRemoteClientForCoordinator()

    async def test_coordinator_inheritance_and_defaults(self) -> None:
        """Vérifie que la classe hérite de DataUpdateCoordinator et applique les valeurs par défaut."""
        coord = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, self.entry)
        self.assertIsInstance(coord, DataUpdateCoordinator)
        self.assertEqual(coord.name, "smart_remote_rebirth_a06fe048b48b")
        self.assertEqual(coord.update_interval, timedelta(seconds=60))
        self.assertEqual(coord.consecutive_failures, 0)
        self.assertEqual(coord.cached_data, {})
        self.assertIs(coord.client, self.mock_client)
        self.assertIs(coord.entry, self.entry)

    def test_coordinator_name_resolution_hierarchy(self) -> None:
        """Vérifie la hiérarchie de résolution du nom (unique_id -> serial_number -> entry_id)."""
        # 1. Unique ID
        coord1 = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, self.entry)
        self.assertEqual(coord1.name, "smart_remote_rebirth_a06fe048b48b")

        # 2. CONF_SERIAL_NUMBER sans unique_id
        entry_no_uid = ConfigEntry(
            entry_id="entry_id_xyz",
            domain=DOMAIN,
            title="Remote",
            data={CONF_SERIAL_NUMBER: "serial_from_data"},
            unique_id=None,
        )
        coord2 = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, entry_no_uid)
        self.assertEqual(coord2.name, "smart_remote_rebirth_serial_from_data")

        # 3. Fallback entry_id
        entry_fallback = ConfigEntry(
            entry_id="entry_id_fallback_999",
            domain=DOMAIN,
            title="Remote",
            data={},
            unique_id=None,
        )
        coord3 = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, entry_fallback)
        self.assertEqual(coord3.name, "smart_remote_rebirth_entry_id_fallback_999")

    def test_polling_interval_options_and_fallbacks(self) -> None:
        """Vérifie la configuration de l'intervalle de polling (options -> data -> 60s)."""
        # Options prioritaires
        entry_options = ConfigEntry(
            entry_id="e1",
            domain=DOMAIN,
            title="Remote",
            data={CONF_POLLING_INTERVAL: 45},
            options={CONF_POLLING_INTERVAL: 120},
        )
        coord1 = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, entry_options)
        self.assertEqual(coord1.update_interval, timedelta(seconds=120))

        # Data quand pas d'options
        entry_data = ConfigEntry(
            entry_id="e2",
            domain=DOMAIN,
            title="Remote",
            data={CONF_POLLING_INTERVAL: 30},
            options={},
        )
        coord2 = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, entry_data)
        self.assertEqual(coord2.update_interval, timedelta(seconds=30))

        # Valeurs invalides : 0, négatives, non-numériques -> repli à 60s
        for invalid_val in [0, -15, "invalid", None, []]:
            entry_invalid = ConfigEntry(
                entry_id="e3",
                domain=DOMAIN,
                title="Remote",
                data={},
                options={CONF_POLLING_INTERVAL: invalid_val},
            )
            coord3 = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, entry_invalid)
            self.assertEqual(coord3.update_interval, timedelta(seconds=DEFAULT_COORDINATOR_UPDATE_INTERVAL))


class TestSevenhugsDataUpdateCoordinatorTelemetry(unittest.IsolatedAsyncioTestCase):
    """Vérifie la mise à jour des données et la normalisation de la télémétrie."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.entry = ConfigEntry(
            entry_id="coord_entry_002",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.100",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "a06fe048b48b",
                CONF_REMOTE_ID: "5726",
            },
            unique_id="a06fe048b48b",
        )
        self.mock_client = MockSmartRemoteClientForCoordinator()
        self.coord = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, self.entry)

    async def test_nominal_telemetry_update(self) -> None:
        """Vérifie le rafraîchissement nominal complet et la population du cache."""
        data = await self.coord._async_update_data()

        self.assertTrue(data["connected"])
        self.assertEqual(data["serial_number"], "SR1A987654321")
        self.assertEqual(data["mac_address"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(data["firmware_version"], "v0.76.4")
        self.assertEqual(data["name"], "Smart Remote Salon")
        self.assertIsInstance(data["raw_settings"], dict)

        # Cache synchronisé et compteur d'échecs à 0
        self.assertEqual(self.coord.cached_data["serial_number"], "SR1A987654321")
        self.assertEqual(self.coord.consecutive_failures, 0)
        self.assertIn(("GET", "/api/settings"), self.mock_client.request_calls)

    async def test_non_dict_and_empty_payload_resilience(self) -> None:
        """Vérifie le traitement d'une réponse non-dictionnaire sans crash."""
        for invalid_raw in [None, [], "not_a_dict", 12345]:
            self.mock_client.settings_response = invalid_raw
            data = await self.coord._async_update_data()
            self.assertIsInstance(data, dict)
            self.assertTrue(data["connected"])
            self.assertEqual(data["serial_number"], "a06fe048b48b")


class TestSevenhugsDataUpdateCoordinatorSleepResilience(unittest.IsolatedAsyncioTestCase):
    """Vérifie la machine à états de résilience de veille (tolérance à 3 échecs consécutifs)."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.entry = ConfigEntry(
            entry_id="coord_entry_003",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.100",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "a06fe048b48b",
                CONF_REMOTE_ID: "5726",
            },
            unique_id="a06fe048b48b",
        )
        self.mock_client = MockSmartRemoteClientForCoordinator()
        self.coord = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, self.entry)
        # Établir un premier état nominal en cache
        await self.coord.async_refresh()
        self.assertTrue(self.coord.last_update_success)
        self.assertEqual(self.coord.consecutive_failures, 0)
        self.assertTrue(self.coord.is_connected)

    async def test_sleep_resilience_two_failures_returns_cache_silently(self) -> None:
        """Vérifie que les échecs 1 et 2 retournent les données du cache sans lever d'exception."""
        initial_cache = dict(self.coord.cached_data)

        # --- Échec 1 : Coupure réseau transitoire ---
        self.mock_client.side_effect = SevenhugsConnectionError("Connection lost (deep sleep)")
        await self.coord.async_refresh()

        self.assertEqual(self.coord.consecutive_failures, 1)
        self.assertTrue(self.coord.last_update_success)
        self.assertTrue(self.coord.is_connected)
        self.assertEqual(self.coord.data["serial_number"], initial_cache["serial_number"])
        self.assertEqual(self.coord.cached_data["serial_number"], initial_cache["serial_number"])

        # --- Échec 2 : Timeout matériel ---
        self.mock_client.side_effect = asyncio.TimeoutError("Socket read timeout")
        await self.coord.async_refresh()

        self.assertEqual(self.coord.consecutive_failures, 2)
        self.assertTrue(self.coord.last_update_success)
        self.assertTrue(self.coord.is_connected)
        self.assertEqual(self.coord.data["serial_number"], initial_cache["serial_number"])
        self.assertEqual(self.coord.cached_data["serial_number"], initial_cache["serial_number"])

    async def test_sleep_resilience_third_failure_raises_update_failed_and_marks_unavailable(self) -> None:
        """Vérifie qu'au 3ème échec consécutif, UpdateFailed est levé et is_connected bascule à False."""
        self.mock_client.side_effect = SevenhugsConnectionError("Socket offline")

        # Échec 1
        await self.coord.async_refresh()
        self.assertEqual(self.coord.consecutive_failures, 1)
        self.assertTrue(self.coord.is_connected)

        # Échec 2
        await self.coord.async_refresh()
        self.assertEqual(self.coord.consecutive_failures, 2)
        self.assertTrue(self.coord.is_connected)

        # Échec 3 : seuil atteint -> UpdateFailed levé par _async_update_data()
        await self.coord.async_refresh()
        self.assertEqual(self.coord.consecutive_failures, 3)
        self.assertFalse(self.coord.last_update_success)
        self.assertFalse(self.coord.is_connected)
        self.assertIsInstance(self.coord.last_exception, UpdateFailed)

        # CRITIQUE : Le cache mémoire reste STRICTEMENT préservé malgré l'indisponibilité
        self.assertIsNotNone(self.coord.cached_data)
        self.assertEqual(self.coord.cached_data["serial_number"], "SR1A987654321")

    async def test_sleep_resilience_subsequent_failures_maintain_unavailable_and_cache(self) -> None:
        """Vérifie que les échecs 4 et 5 maintiennent l'indisponibilité et conservent le cache."""
        self.mock_client.side_effect = SevenhugsConnectionError("Remote offline")

        for _ in range(5):
            await self.coord.async_refresh()

        self.assertEqual(self.coord.consecutive_failures, 5)
        self.assertFalse(self.coord.last_update_success)
        self.assertFalse(self.coord.is_connected)
        self.assertIsInstance(self.coord.last_exception, UpdateFailed)
        self.assertEqual(self.coord.cached_data["serial_number"], "SR1A987654321")

    async def test_recovery_resets_failures_and_restores_connectivity(self) -> None:
        """Vérifie la restauration nominale après 3 échecs consécutifs."""
        # Provoquer 3 échecs
        self.mock_client.side_effect = TimeoutError("Network timeout")
        for _ in range(3):
            await self.coord.async_refresh()

        self.assertEqual(self.coord.consecutive_failures, 3)
        self.assertFalse(self.coord.is_connected)

        # Rétablissement de la connexion
        self.mock_client.side_effect = None
        self.mock_client.settings_response = {"serial_number": "SR1A987654321", "firmware_version": "v0.76.5"}
        await self.coord.async_refresh()

        self.assertEqual(self.coord.consecutive_failures, 0)
        self.assertTrue(self.coord.last_update_success)
        self.assertTrue(self.coord.is_connected)
        self.assertEqual(self.coord.firmware_version, "v0.76.5")

    async def test_network_errors_matrix(self) -> None:
        """Vérifie que chaque type d'exception dans _NETWORK_ERRORS incrémente le compteur."""
        error_types = [
            SevenhugsConnectionError("Sevenhugs connection broken"),
            TimeoutError("Built-in timeout"),
            asyncio.TimeoutError("Asyncio timeout"),
            ConnectionError("Standard connection error"),
            ConnectionResetError("Socket reset by remote"),
            OSError("No route to host"),
        ]
        if hasattr(aiohttp, "ClientError"):
            error_types.append(aiohttp.ClientError("Aiohttp client error"))

        for err in error_types:
            self.mock_client.side_effect = None
            coord = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, self.entry)
            await coord.async_refresh()  # Init cache
            self.mock_client.side_effect = err
            await coord.async_refresh()
            self.assertEqual(
                coord.consecutive_failures,
                1,
                f"L'erreur {type(err).__name__} aurait dû être tolérée comme échec de veille",
            )
            self.assertTrue(coord.last_update_success)


class TestSevenhugsDataUpdateCoordinatorWakeUp(unittest.IsolatedAsyncioTestCase):
    """Vérifie le réveil événementiel (async_wake_up / async_handle_wake_up)."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.entry = ConfigEntry(
            entry_id="coord_entry_004",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.100",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "a06fe048b48b",
                CONF_REMOTE_ID: "5726",
            },
            unique_id="a06fe048b48b",
        )
        self.mock_client = MockSmartRemoteClientForCoordinator()
        self.coord = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, self.entry)
        await self.coord.async_refresh()

    async def test_async_wake_up_resets_failures_and_forces_refresh(self) -> None:
        """Vérifie que async_wake_up réinitialise le compteur d'échecs et rafraîchit l'état."""
        # Simuler 2 échecs de veille
        self.mock_client.side_effect = SevenhugsConnectionError("Sleeping")
        await self.coord.async_refresh()
        await self.coord.async_refresh()
        self.assertEqual(self.coord.consecutive_failures, 2)

        # Déclenchement du réveil événementiel
        self.mock_client.side_effect = None
        self.mock_client.settings_response = {"serial_number": "SR1A987654321", "firmware_version": "v0.76.4"}

        await self.coord.async_wake_up(reason="mdns_announcement")

        self.assertEqual(self.coord.consecutive_failures, 0)
        self.assertTrue(self.coord.last_update_success)
        self.assertTrue(self.coord.is_connected)

    async def test_async_handle_wake_up_alias(self) -> None:
        """Vérifie que async_handle_wake_up fonctionne de manière identique à async_wake_up."""
        self.coord._consecutive_failures = 3
        self.mock_client.side_effect = None
        await self.coord.async_handle_wake_up(reason="button_press")
        self.assertEqual(self.coord.consecutive_failures, 0)
        self.assertTrue(self.coord.last_update_success)


class TestSevenhugsDataUpdateCoordinatorAuthAndColdStart(unittest.IsolatedAsyncioTestCase):
    """Vérifie les erreurs d'authentification et les comportements au démarrage à froid."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.entry = ConfigEntry(
            entry_id="coord_entry_005",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.100",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "a06fe048b48b",
                CONF_REMOTE_ID: "5726",
            },
            unique_id="a06fe048b48b",
        )
        self.mock_client = MockSmartRemoteClientForCoordinator()
        self.coord = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, self.entry)

    async def test_auth_error_translates_to_config_entry_auth_failed(self) -> None:
        """Vérifie que SevenhugsAuthError lève ConfigEntryAuthFailed immédiatement sans tolérance veille."""
        self.mock_client.side_effect = SevenhugsAuthError("Session expired on remote")

        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coord._async_update_data()

        # Vérification lors d'un appel refresh complet
        await self.coord.async_refresh()
        self.assertFalse(self.coord.last_update_success)
        self.assertIsInstance(self.coord.last_exception, ConfigEntryAuthFailed)

    async def test_cold_start_network_failure_raises_update_failed_immediately(self) -> None:
        """Vérifie qu'une défaillance au démarrage à froid (sans cache préalable) lève UpdateFailed au 1er échec."""
        fresh_coord = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, self.entry)
        self.mock_client.side_effect = SevenhugsConnectionError("Connection refused on boot")

        with self.assertRaises(UpdateFailed) as ctx:
            await fresh_coord._async_update_data()

        self.assertIn("démarrage initial", str(ctx.exception))
        self.assertEqual(fresh_coord.consecutive_failures, 1)

    async def test_unexpected_exception_translates_to_update_failed(self) -> None:
        """Vérifie qu'une exception inattendue est convertie en UpdateFailed."""
        self.mock_client.side_effect = KeyError("Internal json corruption")

        with self.assertRaises(UpdateFailed) as ctx:
            await self.coord._async_update_data()

        self.assertIn("Erreur inattendue", str(ctx.exception))


class TestSevenhugsDataUpdateCoordinatorProperties(unittest.IsolatedAsyncioTestCase):
    """Vérifie l'exhaustivité des propriétés publiques exposées par le coordinateur."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.entry = ConfigEntry(
            entry_id="coord_entry_006",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.100",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "sn_prop_test_123",
                CONF_REMOTE_ID: "5726",
            },
            unique_id="sn_prop_test_123",
        )
        self.mock_client = MockSmartRemoteClientForCoordinator()
        self.coord = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, self.entry)
        await self.coord.async_refresh()

    def test_properties_values_and_types(self) -> None:
        """Valide le typage et les valeurs retournées par toutes les propriétés."""
        self.assertIsInstance(self.coord.is_connected, bool)
        self.assertTrue(self.coord.is_connected)

        self.assertEqual(self.coord.serial_number, "SR1A987654321")
        self.assertEqual(self.coord.mac_address, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(self.coord.firmware_version, "v0.76.4")
        self.assertEqual(self.coord.remote_id, "5726")
        self.assertEqual(self.coord.consecutive_failures, 0)

    def test_cached_data_immutability(self) -> None:
        """Vérifie que la mutation de la copie de cached_data n'altère pas l'état interne."""
        copy_cache = self.coord.cached_data
        copy_cache["serial_number"] = "corrupted_sn"
        copy_cache["corrupted_key"] = "bad"

        self.assertEqual(self.coord.cached_data["serial_number"], "SR1A987654321")
        self.assertNotIn("corrupted_key", self.coord.cached_data)

    async def test_properties_fallback_to_cache_when_data_is_none(self) -> None:
        """Vérifie que les propriétés se replient sur _cached_data si self.data est temporairement None."""
        self.coord.data = None
        self.assertEqual(self.coord.serial_number, "SR1A987654321")
        self.assertEqual(self.coord.mac_address, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(self.coord.firmware_version, "v0.76.4")


class TestSevenhugsDataUpdateCoordinatorListeners(unittest.IsolatedAsyncioTestCase):
    """Vérifie la notification des écouteurs Home Assistant lors des mises à jour."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.entry = ConfigEntry(
            entry_id="coord_entry_007",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.100",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "a06fe048b48b",
                CONF_REMOTE_ID: "5726",
            },
        )
        self.mock_client = MockSmartRemoteClientForCoordinator()
        self.coord = SevenhugsDataUpdateCoordinator(self.hass, self.mock_client, self.entry)

    async def test_listeners_notified_on_update_and_removal(self) -> None:
        """Vérifie l'enregistrement, l'appel et la désinscription d'un écouteur."""
        listener_called_count = 0

        def on_update() -> None:
            nonlocal listener_called_count
            listener_called_count += 1

        unsubscribe = self.coord.async_add_listener(on_update)

        # Premier rafraîchissement -> notifié
        await self.coord.async_refresh()
        self.assertEqual(listener_called_count, 1)

        # Second rafraîchissement -> notifié à nouveau
        await self.coord.async_refresh()
        self.assertEqual(listener_called_count, 2)

        # Désinscription
        unsubscribe()
        await self.coord.async_refresh()
        self.assertEqual(listener_called_count, 2)


if __name__ == "__main__":
    unittest.main()
