"""Banc de validation et de stress-testing contradictoire pour la plateforme Button (Jalon 2).

Auteur: Challenger 2 (Milestone 2 - EMPIRICAL CHALLENGER)
Rôles: critic, specialist

Ce banc d'essais valide empiriquement la robustesse, la gestion de la concurrence
et la traduction d'exceptions pour custom_components/smart_remote_rebirth/button.py :

1. SevenhugsForceSyncButton:
   - Appel nominal de coordinator.async_request_refresh().
   - Salve de concurrence massive (Thundering herd, 100 requêtes concurrentes).
   - Matrice exhaustive de traduction d'exceptions vers HomeAssistantError.
   - Enquête empirique sur l'absorption d'erreurs par DataUpdateCoordinator.
2. SevenhugsPingButton:
   - Séquencement strict : GET /api/hello puis coordinator.async_request_refresh().
   - Injection de coupures sockets (timeouts, connection resets, ECONNREFUSED).
   - Injection d'erreurs d'authentification HTTP 400 et SevenhugsAuthError.
   - Échecs lors de la phase de rafraîchissement secondaire.
   - Salve de concurrence (60 requêtes concurrentes avec latence aléatoire).
3. SevenhugsBackupConfigButton:
   - Appel nominal GET /api/config et stockage dans _last_backup.
   - Exposition contrôlée des métadonnées dans extra_state_attributes.
   - Cas limites et formats inattendus (None, listes, scalaires, dicts vides).
   - Conservation de l'intégrité du backup précédent après une tentative échouée.
   - Injection d'erreurs d'authentification et de communication réseau.
4. Concurrence Mixte (Chaos multi-boutons):
   - 90 requêtes concurrentes entrelacées (Sync + Ping + Backup) sous latence variable.
5. Cycle de Vie, Extraction de Coordinateur et Duck-Typing:
   - Double stockage entry.runtime_data et hass.data[DOMAIN][entry.entry_id].
   - Résilience absolue aux coordinateurs manquants ou corrompus.
   - Analyse empirique du duck-typing vs isinstance.
6. Conformité Home Assistant & ADR:
   - Category DIAGNOSTIC, has_entity_name=True, PARALLEL_UPDATES=0, should_poll=False.
   - Propagation stricte de asyncio.CancelledError sans masquage.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ingestion de tests.ha_mock
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.smart_remote_rebirth.button import (
    PARALLEL_UPDATES,
    SevenhugsBackupConfigButton,
    SevenhugsForceSyncButton,
    SevenhugsPingButton,
    _resolve_coordinator,
    async_setup_entry,
)
from custom_components.smart_remote_rebirth.client import (
    SevenhugsAuthError,
    SevenhugsConnectionError,
)
from custom_components.smart_remote_rebirth.const import (
    CONF_REMOTE_ID,
    CONF_SERIAL_NUMBER,
    DOMAIN,
)
from custom_components.smart_remote_rebirth.coordinator import (
    SevenhugsDataUpdateCoordinator,
)


class TestAdversarialButtonStress(unittest.IsolatedAsyncioTestCase):
    """Suite d'épreuves de résistance et de validation contradictoire pour button.py."""

    async def asyncSetUp(self) -> None:
        """Initialise un environnement d'exécution sain avant chaque test."""
        self.hass = HomeAssistant()
        self.entry = ConfigEntry(
            entry_id="sr1a_entry_stress",
            domain=DOMAIN,
            title="Smart Remote 1A (Stress)",
            data={
                CONF_SERIAL_NUMBER: "SR1A_CHALLENGER_007",
                CONF_REMOTE_ID: "REMOTE_007",
                "firmware_version": "0.76.4",
                "mac_address": "AA:BB:CC:DD:EE:07",
            },
        )
        self.mock_client = AsyncMock()
        self.mock_coordinator = MagicMock(spec=SevenhugsDataUpdateCoordinator)
        self.mock_coordinator.client = self.mock_client
        self.mock_coordinator.serial_number = "SR1A_CHALLENGER_007"
        self.mock_coordinator.remote_id = "REMOTE_007"
        self.mock_coordinator.firmware_version = "0.76.4"
        self.mock_coordinator.mac_address = "AA:BB:CC:DD:EE:07"
        self.mock_coordinator.last_update_success = True
        self.mock_coordinator.data = {"battery": 95, "wifi_signal": -55}
        self.mock_coordinator.async_request_refresh = AsyncMock()

    # =========================================================================
    # DIMENSION 1: SEVENHUGS FORCE SYNC BUTTON
    # =========================================================================

    async def test_force_sync_nominal_execution(self) -> None:
        """Vérifie le déclenchement nominal de coordinator.async_request_refresh()."""
        button = SevenhugsForceSyncButton(self.mock_coordinator, self.entry)

        # Attributs Home Assistant
        self.assertEqual(button.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(button.icon, "mdi:sync")
        self.assertTrue(button.has_entity_name)
        self.assertFalse(button.should_poll)
        self.assertEqual(button.unique_id, "SR1A_CHALLENGER_007_force_sync")
        self.assertEqual(button.translation_key, "force_sync")

        # Exécution de l'appui
        await button.async_press()
        self.assertEqual(self.mock_coordinator.async_request_refresh.call_count, 1)

    async def test_force_sync_exception_translation_matrix(self) -> None:
        """Vérifie que toute exception levée lors du refresh est convertie en HomeAssistantError."""
        button = SevenhugsForceSyncButton(self.mock_coordinator, self.entry)

        exception_matrix = [
            SevenhugsConnectionError("TCP connection lost"),
            SevenhugsAuthError("Token rejected by remote"),
            TimeoutError("Socket read timeout"),
            asyncio.TimeoutError("Event loop timeout"),
            ConnectionResetError("Connection reset by peer"),
            ConnectionRefusedError("Port 80 closed"),
            OSError("Network interface down"),
            RuntimeError("Unexpected threadpool fault"),
            ValueError("Malformed JSON payload"),
            UpdateFailed("Home Assistant UpdateFailed error"),
        ]

        for exc in exception_matrix:
            with self.subTest(exc=type(exc).__name__):
                self.mock_coordinator.async_request_refresh.side_effect = exc
                with self.assertRaises(HomeAssistantError) as ctx:
                    await button.async_press()

                # Vérifie le message et la chaîne causale
                self.assertIn("Échec de la synchronisation forcée Sevenhugs", str(ctx.exception))
                self.assertIs(ctx.exception.__cause__, exc)

    async def test_force_sync_thundering_herd_concurrency(self) -> None:
        """Stress-test : 100 appuis ultra-rapides concurrents sur le bouton Force Sync."""
        button = SevenhugsForceSyncButton(self.mock_coordinator, self.entry)

        call_times: list[float] = []

        async def simulated_refresh():
            await asyncio.sleep(random.uniform(0.001, 0.005))
            call_times.append(asyncio.get_event_loop().time())

        self.mock_coordinator.async_request_refresh.side_effect = simulated_refresh

        # Salve concurrente de 100 coroutines
        tasks = [asyncio.create_task(button.async_press()) for _ in range(100)]
        await asyncio.gather(*tasks)

        self.assertEqual(len(call_times), 100)
        self.assertEqual(self.mock_coordinator.async_request_refresh.call_count, 100)

    async def test_force_sync_underlying_coordinator_swallowing_behavior(self) -> None:
        """Enquête empirique : comportement quand le coordinateur réel absorbe l'échec réseau."""
        client = AsyncMock()
        client.async_request.side_effect = SevenhugsConnectionError("Remote completely dead")

        real_coord = SevenhugsDataUpdateCoordinator(self.hass, client, self.entry)
        button = SevenhugsForceSyncButton(real_coord, self.entry)

        # 1. Succès initial pour amorcer le cache _cached_data
        client.async_request.side_effect = None
        client.async_request.return_value = {"battery": 90, "charging": False, "wifi_signal": -60}
        await button.async_press()
        self.assertTrue(real_coord.last_update_success)
        self.assertEqual(real_coord.consecutive_failures, 0)
        self.assertEqual(real_coord.battery, 90)

        # 2. Bascule en défaillance réseau
        client.async_request.side_effect = SevenhugsConnectionError("Remote completely dead")

        # Les 2 premiers échecs sont tolérés par la résilience veille (last_update_success reste True)
        await button.async_press()
        self.assertTrue(real_coord.last_update_success)
        self.assertEqual(real_coord.consecutive_failures, 1)

        await button.async_press()
        self.assertTrue(real_coord.last_update_success)
        self.assertEqual(real_coord.consecutive_failures, 2)

        # Au 3ème échec, _async_update_data lève UpdateFailed.
        # Mais async_refresh() de DataUpdateCoordinator attrape UpdateFailed en interne
        # et positionne last_update_success = False sans relancer l'exception.
        await button.async_press()
        self.assertFalse(real_coord.last_update_success)
        self.assertEqual(real_coord.consecutive_failures, 3)
        self.assertIsNotNone(real_coord.last_exception)

        # Observation empirique : button.async_press() ne lève pas d'exception
        # car async_request_refresh() ne propage pas UpdateFailed dans le standard HA.
        # Le coordinateur passe simplement à last_update_success=False.
        self.assertFalse(real_coord.last_update_success)

    # =========================================================================
    # DIMENSION 2: SEVENHUGS PING BUTTON
    # =========================================================================

    async def test_ping_nominal_execution_and_call_order(self) -> None:
        """Vérifie que le ping appelle d'abord /api/hello puis async_request_refresh()."""
        button = SevenhugsPingButton(self.mock_coordinator, self.entry)

        self.assertEqual(button.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(button.icon, "mdi:network-outline")
        self.assertTrue(button.has_entity_name)
        self.assertEqual(button.unique_id, "SR1A_CHALLENGER_007_ping")
        self.assertEqual(button.translation_key, "ping")

        call_sequence: list[str] = []

        async def mock_request(method: str, path: str, *args, **kwargs):
            call_sequence.append(f"{method} {path}")
            return {"status": "ok"}

        async def mock_refresh():
            call_sequence.append("refresh")

        self.mock_client.async_request.side_effect = mock_request
        self.mock_coordinator.async_request_refresh.side_effect = mock_refresh

        await button.async_press()

        self.assertEqual(call_sequence, ["GET /api/hello", "refresh"])

    async def test_ping_injected_socket_timeouts(self) -> None:
        """Vérifie la capture et conversion des timeouts réseau sur le ping."""
        button = SevenhugsPingButton(self.mock_coordinator, self.entry)

        for timeout_exc in [TimeoutError("TCP timeout"), asyncio.TimeoutError()]:
            with self.subTest(exc=type(timeout_exc).__name__):
                self.mock_client.async_request.side_effect = timeout_exc
                self.mock_coordinator.async_request_refresh.reset_mock()

                with self.assertRaises(HomeAssistantError) as ctx:
                    await button.async_press()

                self.assertIn("Impossible de joindre la télécommande Sevenhugs lors du ping", str(ctx.exception))
                self.assertIs(ctx.exception.__cause__, timeout_exc)
                # Le rafraîchissement ne doit PAS être appelé si le ping /api/hello échoue
                self.mock_coordinator.async_request_refresh.assert_not_called()

    async def test_ping_injected_connection_resets_and_errors(self) -> None:
        """Vérifie la conversion des erreurs de transport/socket en HomeAssistantError."""
        button = SevenhugsPingButton(self.mock_coordinator, self.entry)

        conn_errors = [
            SevenhugsConnectionError("Connection lost"),
            ConnectionResetError("Connection reset by peer"),
            ConnectionRefusedError("Connection refused on port 80"),
            ConnectionError("General network disconnect"),
            OSError("No route to host"),
        ]

        for err in conn_errors:
            with self.subTest(err=type(err).__name__):
                self.mock_client.async_request.side_effect = err
                self.mock_coordinator.async_request_refresh.reset_mock()

                with self.assertRaises(HomeAssistantError) as ctx:
                    await button.async_press()

                self.assertIn("Impossible de joindre la télécommande Sevenhugs lors du ping", str(ctx.exception))
                self.assertIs(ctx.exception.__cause__, err)
                self.mock_coordinator.async_request_refresh.assert_not_called()

    async def test_ping_injected_auth_and_400_errors(self) -> None:
        """Vérifie la conversion des erreurs d'authentification en HomeAssistantError spécifique."""
        button = SevenhugsPingButton(self.mock_coordinator, self.entry)

        auth_error = SevenhugsAuthError("Session expirée ou rejetée par la télécommande (HTTP 400)")
        self.mock_client.async_request.side_effect = auth_error
        self.mock_coordinator.async_request_refresh.reset_mock()

        with self.assertRaises(HomeAssistantError) as ctx:
            await button.async_press()

        self.assertIn("Échec d'authentification lors du ping Sevenhugs", str(ctx.exception))
        self.assertIs(ctx.exception.__cause__, auth_error)
        self.mock_coordinator.async_request_refresh.assert_not_called()

    async def test_ping_second_phase_refresh_failure(self) -> None:
        """Vérifie le comportement si /api/hello réussit mais que le refresh coordinateur échoue."""
        button = SevenhugsPingButton(self.mock_coordinator, self.entry)

        self.mock_client.async_request.return_value = {"status": "ok"}
        refresh_err = RuntimeError("Deadlock detected in coordinator listeners")
        self.mock_coordinator.async_request_refresh.side_effect = refresh_err

        with self.assertRaises(HomeAssistantError) as ctx:
            await button.async_press()

        # Attrapé par la clause générique except Exception
        self.assertIn("Erreur inattendue lors du ping Sevenhugs", str(ctx.exception))
        self.assertIs(ctx.exception.__cause__, refresh_err)

    async def test_ping_thundering_herd_concurrency(self) -> None:
        """Stress-test : 60 pings simultanés avec gigue aléatoire sans blocage."""
        button = SevenhugsPingButton(self.mock_coordinator, self.entry)

        async def simulated_hello(method: str, path: str):
            await asyncio.sleep(random.uniform(0.001, 0.003))
            return {"status": "ok"}

        async def simulated_refresh():
            await asyncio.sleep(random.uniform(0.001, 0.003))

        self.mock_client.async_request.side_effect = simulated_hello
        self.mock_coordinator.async_request_refresh.side_effect = simulated_refresh

        tasks = [asyncio.create_task(button.async_press()) for _ in range(60)]
        await asyncio.gather(*tasks)

        self.assertEqual(self.mock_client.async_request.call_count, 60)
        self.assertEqual(self.mock_coordinator.async_request_refresh.call_count, 60)

    # =========================================================================
    # DIMENSION 3: SEVENHUGS BACKUP CONFIG BUTTON
    # =========================================================================

    async def test_backup_config_nominal_execution_and_attributes(self) -> None:
        """Vérifie la sauvegarde de configuration et l'exposition stricte des métadonnées."""
        button = SevenhugsBackupConfigButton(self.mock_coordinator, self.entry)

        self.assertEqual(button.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(button.icon, "mdi:cloud-download")
        self.assertTrue(button.has_entity_name)
        self.assertEqual(button.unique_id, "SR1A_CHALLENGER_007_backup_config")
        self.assertEqual(button.translation_key, "backup_config")

        # Avant le premier appui, les attributs d'état doivent être vides
        self.assertEqual(button.extra_state_attributes, {})

        mock_config = {
            "user_info": {"name": "Test User", "id": 123},
            "devices": [{"id": 1, "type": "tv"}, {"id": 2, "type": "amp"}],
            "rooms": [{"id": 10, "name": "Living Room"}],
            "network": {"ssid": "Home-WiFi", "ip": "192.168.1.50"},
        }
        self.mock_client.async_request.return_value = mock_config

        await button.async_press()

        self.assertEqual(self.mock_client.async_request.call_count, 1)
        self.assertEqual(self.mock_client.async_request.call_args[0], ("GET", "/api/config"))

        attrs = button.extra_state_attributes
        self.assertEqual(attrs["last_backup_size"], 4)
        self.assertEqual(set(attrs["last_backup_sections"]), {"user_info", "devices", "rooms", "network"})

    async def test_backup_config_malformed_and_boundary_payloads(self) -> None:
        """Vérifie la robustesse aux payloads inattendus retournés par /api/config."""
        button = SevenhugsBackupConfigButton(self.mock_coordinator, self.entry)

        test_payloads = [
            ({}, 0, []),
            (None, 0, []),
            (["not", "a", "dict"], 0, []),
            ("string_payload", 0, []),
            (12345, 0, []),
            (True, 0, []),
        ]

        for payload, expected_size, expected_sections in test_payloads:
            with self.subTest(payload_type=type(payload).__name__):
                self.mock_client.async_request.return_value = payload
                await button.async_press()
                attrs = button.extra_state_attributes
                self.assertEqual(attrs["last_backup_size"], expected_size)
                self.assertEqual(attrs["last_backup_sections"], expected_sections)

    async def test_backup_config_injected_errors(self) -> None:
        """Vérifie la conversion des erreurs de sauvegarde en HomeAssistantError."""
        button = SevenhugsBackupConfigButton(self.mock_coordinator, self.entry)

        # Auth Error
        self.mock_client.async_request.side_effect = SevenhugsAuthError("Forbidden 403")
        with self.assertRaises(HomeAssistantError) as ctx:
            await button.async_press()
        self.assertIn("Échec d'authentification lors de la sauvegarde Sevenhugs", str(ctx.exception))

        # Comm Errors
        for comm_err in [
            SevenhugsConnectionError("Host unreachable"),
            TimeoutError("Timeout reading /api/config"),
            ConnectionResetError("RST on config dump"),
            OSError("Socket broken"),
        ]:
            with self.subTest(err=type(comm_err).__name__):
                self.mock_client.async_request.side_effect = comm_err
                with self.assertRaises(HomeAssistantError) as ctx:
                    await button.async_press()
                self.assertIn("Impossible de joindre la télécommande Sevenhugs pour sauvegarder la configuration", str(ctx.exception))

        # Erreur inattendue
        self.mock_client.async_request.side_effect = KeyError("Corrupted internal crypto state")
        with self.assertRaises(HomeAssistantError) as ctx:
            await button.async_press()
        self.assertIn("Erreur inattendue lors de la sauvegarde Sevenhugs", str(ctx.exception))

    async def test_backup_config_state_integrity_after_failed_press(self) -> None:
        """Garantit qu'un appui échoué n'efface ni ne corrompt le backup précédent réussi."""
        button = SevenhugsBackupConfigButton(self.mock_coordinator, self.entry)

        # 1. Sauvegarde réussie
        self.mock_client.async_request.return_value = {"section1": 1, "section2": 2}
        await button.async_press()
        self.assertEqual(button.extra_state_attributes["last_backup_size"], 2)

        # 2. Sauvegarde suivante échouée
        self.mock_client.async_request.side_effect = SevenhugsConnectionError("Network glitch")
        with self.assertRaises(HomeAssistantError):
            await button.async_press()

        # Invariant critique : le backup précédent doit rester intact !
        self.assertEqual(button.extra_state_attributes["last_backup_size"], 2)
        self.assertEqual(set(button.extra_state_attributes["last_backup_sections"]), {"section1", "section2"})

        # 3. Sauvegarde suivante à nouveau réussie avec 3 sections
        self.mock_client.async_request.side_effect = None
        self.mock_client.async_request.return_value = {"a": 1, "b": 2, "c": 3}
        await button.async_press()
        self.assertEqual(button.extra_state_attributes["last_backup_size"], 3)
        self.assertEqual(set(button.extra_state_attributes["last_backup_sections"]), {"a", "b", "c"})

    async def test_backup_config_thundering_herd_concurrency(self) -> None:
        """Stress-test : 60 sauvegardes concurrentes avec latence asynchrone."""
        button = SevenhugsBackupConfigButton(self.mock_coordinator, self.entry)

        async def simulated_config(method: str, path: str):
            await asyncio.sleep(random.uniform(0.001, 0.004))
            return {"devices": [], "rooms": []}

        self.mock_client.async_request.side_effect = simulated_config

        tasks = [asyncio.create_task(button.async_press()) for _ in range(60)]
        await asyncio.gather(*tasks)

        self.assertEqual(self.mock_client.async_request.call_count, 60)
        self.assertEqual(button.extra_state_attributes["last_backup_size"], 2)

    # =========================================================================
    # DIMENSION 4: CONCURRENCE MIXTE (CHAOS MULTI-BOUTONS)
    # =========================================================================

    async def test_mixed_buttons_thundering_herd_chaos(self) -> None:
        """Stress-test : 90 appuis concurrents entrelacés (Sync + Ping + Backup)."""
        force_sync = SevenhugsForceSyncButton(self.mock_coordinator, self.entry)
        ping_btn = SevenhugsPingButton(self.mock_coordinator, self.entry)
        backup_btn = SevenhugsBackupConfigButton(self.mock_coordinator, self.entry)

        async def simulated_request(method: str, path: str, *args, **kwargs):
            await asyncio.sleep(random.uniform(0.0005, 0.003))
            if path == "/api/hello":
                return {"status": "ok"}
            elif path == "/api/config":
                return {"chaos": True, "count": 1}
            return {}

        async def simulated_refresh():
            await asyncio.sleep(random.uniform(0.0005, 0.003))

        self.mock_client.async_request.side_effect = simulated_request
        self.mock_coordinator.async_request_refresh.side_effect = simulated_refresh

        # Création d'une salve de 90 opérations entrelacées
        tasks = []
        for _ in range(30):
            tasks.append(asyncio.create_task(force_sync.async_press()))
            tasks.append(asyncio.create_task(ping_btn.async_press()))
            tasks.append(asyncio.create_task(backup_btn.async_press()))

        random.shuffle(tasks)
        await asyncio.gather(*tasks)

        # Vérification qu'aucun blocage n'a eu lieu et que tous les appels sont arrivés
        self.assertEqual(self.mock_coordinator.async_request_refresh.call_count, 60)  # 30 force_sync + 30 ping
        self.assertEqual(self.mock_client.async_request.call_count, 60)  # 30 ping + 30 backup
        self.assertEqual(backup_btn.extra_state_attributes["last_backup_size"], 2)

    # =========================================================================
    # DIMENSION 5: CYCLE DE VIE, EXTRACTION & DUCK-TYPING DE COORDINATEUR
    # =========================================================================

    async def test_setup_entry_all_storage_resolution_patterns(self) -> None:
        """Vérifie l'extraction du coordinateur sous tous les schémas de stockage supportés."""
        # Pattern 1: runtime_data direct
        self.entry.runtime_data = self.mock_coordinator
        added: list[ButtonEntity] = []
        await async_setup_entry(self.hass, self.entry, added.extend)
        self.assertEqual(len(added), 3)

        # Pattern 2: runtime_data.coordinator
        container = MagicMock()
        container.coordinator = self.mock_coordinator
        self.entry.runtime_data = container
        added.clear()
        await async_setup_entry(self.hass, self.entry, added.extend)
        self.assertEqual(len(added), 3)

        # Pattern 3: runtime_data dict
        self.entry.runtime_data = {"coordinator": self.mock_coordinator}
        added.clear()
        await async_setup_entry(self.hass, self.entry, added.extend)
        self.assertEqual(len(added), 3)

        # Pattern 4: hass.data[DOMAIN][entry_id] direct
        self.entry.runtime_data = None
        self.hass.data[DOMAIN] = {self.entry.entry_id: self.mock_coordinator}
        added.clear()
        await async_setup_entry(self.hass, self.entry, added.extend)
        self.assertEqual(len(added), 3)

        # Pattern 5: hass.data[DOMAIN][entry_id].coordinator
        self.entry.runtime_data = None
        self.hass.data[DOMAIN] = {self.entry.entry_id: container}
        added.clear()
        await async_setup_entry(self.hass, self.entry, added.extend)
        self.assertEqual(len(added), 3)

        # Pattern 6: hass.data[DOMAIN][entry_id]["coordinator"]
        self.entry.runtime_data = None
        self.hass.data[DOMAIN] = {self.entry.entry_id: {"coordinator": self.mock_coordinator}}
        added.clear()
        await async_setup_entry(self.hass, self.entry, added.extend)
        self.assertEqual(len(added), 3)

    async def test_setup_entry_missing_and_corrupt_structures(self) -> None:
        """Vérifie la résilience lors de l'absence totale ou corruption de structure."""
        corrupt_scenarios = [
            None,
            "corrupt_string_data",
            123456,
            [1, 2, 3],
            {"not_coordinator": True},
        ]

        for bad_data in corrupt_scenarios:
            with self.subTest(scenario=type(bad_data).__name__):
                self.entry.runtime_data = bad_data
                self.hass.data.pop(DOMAIN, None)
                added: list[ButtonEntity] = []

                # Ne doit jamais lever d'exception non gérée
                await async_setup_entry(self.hass, self.entry, added.extend)
                self.assertEqual(len(added), 0)

        # hass sans attribut 'data'
        hass_no_data = MagicMock()
        del hass_no_data.data
        added.clear()
        await async_setup_entry(hass_no_data, self.entry, added.extend)
        self.assertEqual(len(added), 0)

    def test_duck_typing_coordinator_resolution_investigation(self) -> None:
        """Enquête empirique contradictoire : résolution du duck-typing dans _resolve_coordinator."""

        class PureDuckTypedCoordinator:
            """Objet simulant un coordinateur sans dériver de SevenhugsDataUpdateCoordinator."""
            def __init__(self):
                self.serial_number = "DUCK_123"
                self.async_request_refresh = AsyncMock()

        duck = PureDuckTypedCoordinator()

        # Cas A : duck direct dans entry.runtime_data
        entry_a = ConfigEntry("e_a", DOMAIN, "A", {})
        entry_a.runtime_data = duck
        resolved_a = _resolve_coordinator(self.hass, entry_a)
        # Constat empirique : rejeté car isinstance(duck, SevenhugsDataUpdateCoordinator) est False
        # et duck n'a pas d'attribut .coordinator
        self.assertIsNone(resolved_a)

        # Cas B : duck encapsulé dans un dictionnaire {"coordinator": duck}
        entry_b = ConfigEntry("e_b", DOMAIN, "B", {})
        entry_b.runtime_data = {"coordinator": duck}
        resolved_b = _resolve_coordinator(self.hass, entry_b)
        # Constat empirique : accepté car l'accès par clé ne filtre pas par isinstance
        self.assertIs(resolved_b, duck)

        # Cas C : duck encapsulé dans un objet avec attribut .coordinator
        entry_c = ConfigEntry("e_c", DOMAIN, "C", {})
        container = MagicMock()
        container.coordinator = duck
        entry_c.runtime_data = container
        resolved_c = _resolve_coordinator(self.hass, entry_c)
        # Constat empirique : accepté car l'accès par attribut ne filtre pas par isinstance
        self.assertIs(resolved_c, duck)

    # =========================================================================
    # DIMENSION 6: CONFORMITÉ HOME ASSISTANT, PARALLÉLISME & ANNULATION
    # =========================================================================

    def test_parallel_updates_constant(self) -> None:
        """Vérifie que PARALLEL_UPDATES est strictement égal à 0."""
        self.assertEqual(PARALLEL_UPDATES, 0)

    async def test_asyncio_cancellation_propagation(self) -> None:
        """Garantit que l'annulation d'une tâche (CancelledError) n'est jamais masquée."""
        button = SevenhugsPingButton(self.mock_coordinator, self.entry)

        async def hanging_request(*args, **kwargs):
            await asyncio.sleep(10.0)
            return {}

        self.mock_client.async_request.side_effect = hanging_request

        task = asyncio.create_task(button.async_press())
        await asyncio.sleep(0.01)  # Laisser le temps à la tâche de démarrer
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
