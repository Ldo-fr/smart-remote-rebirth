"""Banc d'essais contradictoires empiriques — Cycle de vie, Rollback et Concurrence M4.

Auteur : M4 Lifecycle & Rollback Concurrency Challenger (challenger_p4_m4_2)
Rôles : critic, specialist
Milestone : Phase 4 — Milestone 4

Vérifications empiriques couvertes :
1. Webhooks : Enregistrement nominal, idempotence de double/triple enregistrement,
   idempotence de désenregistrements répétés, désenregistrement d'identifiants inconnus
   ou corrompus, tolérance aux pannes du gestionnaire de webhooks, code HTTP 404 post-désenregistrement.
2. Rollback lors d'échec de démarrage (Setup failure) :
   - Échec d'authentification (ConfigEntryAuthFailed)
   - Échec de connexion réseau (ConfigEntryNotReady)
   - Exception inattendue lors du first_refresh
   - Échec d'aiguillage des plateformes (forwarding failure)
   - Détection empirique stricte de fuite de sockets TCP via /proc/self/fd (0 fuite après 25 cycles)
3. Concurrence et conditions de course :
   - Setup et unload concurrents sur la même entrée
   - Multiples unloads concurrents (10x simultanés)
   - Multiples setups concurrents
   - Cycle de vie parallèle sur 5 entrées distinctes
   - Unload en cours de synchronisation en arrière-plan
4. Synchronisation en arrière-plan non-bloquante :
   - async_setup_entry retourne en < 100ms même si injector.async_sync_entities est retardé (2s)
   - async_setup_entry réussit même si injector.async_sync_entities lève une erreur réseau
   - Matrice complète des options (auto_sync_webhooks / trigger_resync)
   - Rechargement via update_listener et repli sans async_reload
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

import tests.conftest
from tests.conftest import MockSmartRemoteServer
import aiohttp

from custom_components.smart_remote_rebirth import (
    DOMAIN,
    PLATFORMS,
    AsyncSmartRemoteClient,
    SevenhugsAuthError,
    SevenhugsConnectionError,
    SevenhugsData,
    SevenhugsDataUpdateCoordinator,
    SevenhugsLuaInjector,
    async_setup_entry,
    async_unload_entry,
    update_listener,
)
from custom_components.smart_remote_rebirth.const import (
    CONF_AUTO_SYNC_WEBHOOKS,
    CONF_CLIENT_PRIVATE_KEY,
    CONF_CLIENT_PUBLIC_KEY,
    CONF_CONTROLLER_ID,
    CONF_ENTITIES,
    CONF_HOST,
    CONF_PORT,
    CONF_REMOTE_ID,
    CONF_REMOTE_PUBLIC_KEY,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_READ_KEY,
    CONF_SESSION_WRITE_KEY,
    CONF_TRIGGER_RESYNC,
    DEFAULT_AUTO_SYNC_WEBHOOKS,
    DEFAULT_PORT,
    WEBHOOK_ID_PREFIX,
)
from custom_components.smart_remote_rebirth.webhook import (
    async_generate_webhook_url,
    async_get_webhook_id,
    async_register_webhook,
    async_unregister_webhook,
)


def get_current_socket_fd_count() -> int:
    """Compte de manière robuste les descripteurs de socket ouverts via /proc/self/fd."""
    fd_dir = "/proc/self/fd"
    if not os.path.exists(fd_dir):
        return -1
    count = 0
    try:
        entries = os.listdir(fd_dir)
    except OSError:
        return -1

    for fd_name in entries:
        try:
            target = os.readlink(os.path.join(fd_dir, fd_name))
            if "socket:" in target or target.startswith("socket:"):
                count += 1
        except OSError:
            pass
    return count


def create_mock_entry(
    entry_id: str,
    server: MockSmartRemoteServer,
    options: dict[str, Any] | None = None,
    serial: str | None = None,
) -> ConfigEntry:
    """Génère une ConfigEntry valide liée à une instance de MockSmartRemoteServer."""
    return ConfigEntry(
        entry_id=entry_id,
        domain=DOMAIN,
        title=f"Sevenhugs Remote ({entry_id})",
        data={
            CONF_HOST: server.host,
            CONF_PORT: server.port,
            CONF_SERIAL_NUMBER: serial or f"SN_{entry_id}",
            CONF_REMOTE_ID: "5726",
            CONF_CONTROLLER_ID: f"ctrl_{entry_id}",
            CONF_CLIENT_PRIVATE_KEY: "01" * 32,
            CONF_CLIENT_PUBLIC_KEY: "02" * 32,
            CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
            CONF_SESSION_WRITE_KEY: "03" * 32,
            CONF_SESSION_READ_KEY: "04" * 32,
        },
        options=options or {},
    )


# ============================================================================
# AXE 1 : WEBHOOK LIFECYCLE & IDEMPOTENCE ADVERSARIAL
# ============================================================================

class TestAdversarialWebhookLifecycle(unittest.IsolatedAsyncioTestCase):
    """Banc d'essais contradictoires sur le cycle de vie des webhooks locaux."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.server = MockSmartRemoteServer()
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def test_webhook_registration_nominal_contract(self) -> None:
        """Vérifie le contrat d'enregistrement : ID préfixé, local_only=True, méthodes POST/PUT."""
        entry = create_mock_entry("adv_wh_01", self.server)
        wh_id = async_register_webhook(self.hass, entry)

        self.assertEqual(wh_id, f"smart_remote_{entry.entry_id}")
        self.assertTrue(self.hass.webhook.has_webhook(wh_id))

        wh_info = self.hass.webhook.get_webhook(wh_id)
        self.assertIsNotNone(wh_info)
        self.assertTrue(wh_info.local_only)
        self.assertIn("POST", wh_info.allowed_methods)
        self.assertIn("PUT", wh_info.allowed_methods)

        gen_url = async_generate_webhook_url(self.hass, entry)
        self.assertIn(f"/api/webhook/{wh_id}", gen_url)

    async def test_webhook_registration_idempotence(self) -> None:
        """Vérifie que des enregistrements répétés (ex: rechargement rapide) n'écrasent pas en double."""
        entry = create_mock_entry("adv_wh_idem", self.server)
        wh_id1 = async_register_webhook(self.hass, entry)
        wh_id2 = async_register_webhook(self.hass, entry)
        wh_id3 = async_register_webhook(self.hass, entry)

        self.assertEqual(wh_id1, wh_id2)
        self.assertEqual(wh_id2, wh_id3)
        self.assertEqual(len([w for w in self.hass.webhook.webhooks if w.webhook_id == wh_id1]), 1)

    async def test_webhook_multi_entry_isolation(self) -> None:
        """Vérifie que deux entrées ont des webhooks isolés et que désenregistrer l'une n'affecte pas l'autre."""
        entry_a = create_mock_entry("adv_wh_a", self.server)
        entry_b = create_mock_entry("adv_wh_b", self.server)

        wh_a = async_register_webhook(self.hass, entry_a)
        wh_b = async_register_webhook(self.hass, entry_b)

        self.assertNotEqual(wh_a, wh_b)
        self.assertTrue(self.hass.webhook.has_webhook(wh_a))
        self.assertTrue(self.hass.webhook.has_webhook(wh_b))

        async_unregister_webhook(self.hass, entry_a)
        self.assertFalse(self.hass.webhook.has_webhook(wh_a))
        self.assertTrue(self.hass.webhook.has_webhook(wh_b))

        async_unregister_webhook(self.hass, entry_b)
        self.assertFalse(self.hass.webhook.has_webhook(wh_b))

    async def test_webhook_unregister_idempotence_multiple_calls(self) -> None:
        """Vérifie qu'appeler async_unregister_webhook 5 fois consécutives ne soulève aucune exception."""
        entry = create_mock_entry("adv_wh_multi_unreg", self.server)
        wh_id = async_register_webhook(self.hass, entry)
        self.assertTrue(self.hass.webhook.has_webhook(wh_id))

        for i in range(5):
            async_unregister_webhook(self.hass, entry)
            self.assertFalse(self.hass.webhook.has_webhook(wh_id))

    async def test_webhook_unregister_unknown_ids_and_corrupt_entry(self) -> None:
        """Vérifie que désenregistrer des IDs inconnus, vides ou arbitraires est silencieux et tolérant."""
        for target in [
            "nonexistent_entry_id",
            "sevenhugs_unknown_404",
            "",
            "sevenhugs_",
            ConfigEntry(entry_id="ghost_entry", domain=DOMAIN, title="", data={}),
        ]:
            async_unregister_webhook(self.hass, target)

    async def test_webhook_unregistered_route_returns_404(self) -> None:
        """Vérifie qu'une fois désenregistré, un appel HTTP au webhook retourne HTTP 404."""
        entry = create_mock_entry("adv_wh_404", self.server)
        wh_id = async_register_webhook(self.hass, entry)

        # Désenregistrement
        async_unregister_webhook(self.hass, entry)

        # Simulation de requête arrivant sur la route désenregistrée
        mock_req = MagicMock()
        mock_req.method = "POST"
        resp = await self.hass.webhook.async_handle_webhook(wh_id, mock_req)
        self.assertEqual(resp.status, 404)
        self.assertIn("not found", resp.text.lower())

    async def test_webhook_unregister_tolerates_faulty_webhook_manager(self) -> None:
        """Vérifie la tolérance à une panne interne du composant webhook lors du désenregistrement."""
        entry = create_mock_entry("adv_wh_faulty", self.server)
        with patch.object(self.hass.webhook, "async_unregister", side_effect=RuntimeError("HA Webhook Manager Panic")):
            # Doit attraper l'erreur et ne rien propager
            async_unregister_webhook(self.hass, entry)


# ============================================================================
# AXE 2 : SETUP FAILURE ROLLBACK & ZERO RESOURCE LEAKS
# ============================================================================

class TestAdversarialSetupRollback(unittest.IsolatedAsyncioTestCase):
    """Banc d'essais contradictoires sur le rollback atomique lors d'échecs de démarrage."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.server = MockSmartRemoteServer()
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def test_setup_failure_auth_error_rollback(self) -> None:
        """Vérifie que ConfigEntryAuthFailed lors de first_refresh déclenche un rollback complet sans fuite."""
        entry = create_mock_entry("adv_setup_auth_fail", self.server)
        # Clés absentes provoquant SevenhugsAuthError lors de Pair-Verify
        entry.data.pop(CONF_CLIENT_PRIVATE_KEY, None)
        entry.data.pop(CONF_SESSION_WRITE_KEY, None)
        entry.data.pop(CONF_SESSION_READ_KEY, None)

        with self.assertRaises(ConfigEntryAuthFailed):
            await async_setup_entry(self.hass, entry)

        # Vérification 1 : Webhook désenregistré
        expected_wh = f"smart_remote_{entry.entry_id}"
        self.assertFalse(self.hass.webhook.has_webhook(expected_wh))

        # Vérification 2 : hass.data nettoyé
        self.assertNotIn(entry.entry_id, self.hass.data.get(DOMAIN, {}))
        self.assertNotIn(DOMAIN, self.hass.data)

        # Vérification 3 : runtime_data réinitialisé
        self.assertIsNone(entry.runtime_data)

    async def test_setup_failure_connection_error_not_ready_rollback(self) -> None:
        """Vérifie que SevenhugsConnectionError / ConfigEntryNotReady déclenche un rollback propre."""
        entry = create_mock_entry("adv_setup_conn_fail", self.server)
        # Port invalide injoignable
        entry.data[CONF_PORT] = 59999

        with self.assertRaises(ConfigEntryNotReady):
            await async_setup_entry(self.hass, entry)

        expected_wh = f"smart_remote_{entry.entry_id}"
        self.assertFalse(self.hass.webhook.has_webhook(expected_wh))
        self.assertNotIn(entry.entry_id, self.hass.data.get(DOMAIN, {}))
        self.assertIsNone(entry.runtime_data)

    async def test_setup_failure_unexpected_coordinator_crash_rollback(self) -> None:
        """Vérifie qu'un crash inattendu (RuntimeError) dans le coordinateur déclenche un rollback propre."""
        entry = create_mock_entry("adv_setup_crash", self.server)

        with patch.object(
            SevenhugsDataUpdateCoordinator,
            "async_config_entry_first_refresh",
            side_effect=RuntimeError("Simulated memory allocation fault in coordinator"),
        ):
            with self.assertRaises(RuntimeError):
                await async_setup_entry(self.hass, entry)

        expected_wh = f"smart_remote_{entry.entry_id}"
        self.assertFalse(self.hass.webhook.has_webhook(expected_wh))
        self.assertNotIn(entry.entry_id, self.hass.data.get(DOMAIN, {}))
        self.assertIsNone(entry.runtime_data)

    async def test_setup_failure_platform_forwarding_error_rollback(self) -> None:
        """Vérifie qu'un échec lors de async_forward_entry_setups déclenche un rollback complet."""
        entry = create_mock_entry("adv_setup_plat_fail", self.server)

        with patch.object(
            self.hass.config_entries,
            "async_forward_entry_setups",
            side_effect=RuntimeError("Entity platform sensor crashed during setup"),
        ):
            with self.assertRaises(RuntimeError):
                await async_setup_entry(self.hass, entry)

        expected_wh = f"smart_remote_{entry.entry_id}"
        self.assertFalse(self.hass.webhook.has_webhook(expected_wh))
        self.assertNotIn(entry.entry_id, self.hass.data.get(DOMAIN, {}))
        self.assertIsNone(entry.runtime_data)

    async def test_setup_failure_endurance_zero_fd_leak(self) -> None:
        """Vérifie empiriquement sur 25 échecs consécutifs qu'aucune socket TCP n'est fuitée."""
        await asyncio.sleep(0.05)
        initial_fds = get_current_socket_fd_count()

        cycles = 25
        for i in range(cycles):
            entry = create_mock_entry(f"adv_leak_test_{i}", self.server)
            if i % 2 == 0:
                entry.data.pop(CONF_CLIENT_PRIVATE_KEY, None)
                entry.data.pop(CONF_SESSION_WRITE_KEY, None)
                entry.data.pop(CONF_SESSION_READ_KEY, None)
                expected_exc = ConfigEntryAuthFailed
            else:
                entry.data[CONF_PORT] = 59998
                expected_exc = ConfigEntryNotReady

            with self.assertRaises(expected_exc):
                await async_setup_entry(self.hass, entry)

            # Vérification état propre à chaque cycle
            self.assertFalse(self.hass.webhook.has_webhook(f"smart_remote_{entry.entry_id}"))
            self.assertNotIn(entry.entry_id, self.hass.data.get(DOMAIN, {}))
            self.assertIsNone(entry.runtime_data)

        await asyncio.sleep(0.05)
        final_fds = get_current_socket_fd_count()
        fd_delta = final_fds - initial_fds
        # Tolérance 0 fuite nette de socket
        self.assertLessEqual(fd_delta, 0, f"Fuite de descripteurs de socket détectée: delta = {fd_delta:+d}")


# ============================================================================
# AXE 3 : CONCURRENCE & CONDITIONS DE COURSE
# ============================================================================

class TestAdversarialConcurrency(unittest.IsolatedAsyncioTestCase):
    """Banc d'essais contradictoires sur la concurrence et les conditions de course."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.server = MockSmartRemoteServer()
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def test_concurrent_setup_and_unload_same_entry(self) -> None:
        """Vérifie que setup et unload concurrents sur la même entrée ne créent ni interblocage ni état corrompu."""
        entry = create_mock_entry("adv_conc_su_un", self.server)

        # Lancement simultané de setup et unload
        results = await asyncio.gather(
            async_setup_entry(self.hass, entry),
            async_unload_entry(self.hass, entry),
            return_exceptions=True,
        )

        # Aucun interblocage, les deux coroutines sont terminées
        self.assertEqual(len(results), 2)
        for r in results:
            if isinstance(r, Exception):
                self.assertIsInstance(r, (ConfigEntryNotReady, ConfigEntryAuthFailed, RuntimeError))

        # Nettoyage final pour certifier que l'état post-concurrence peut être stabilisé
        await async_unload_entry(self.hass, entry)
        self.assertFalse(self.hass.webhook.has_webhook(f"smart_remote_{entry.entry_id}"))
        self.assertNotIn(entry.entry_id, self.hass.data.get(DOMAIN, {}))
        self.assertIsNone(entry.runtime_data)

    async def test_concurrent_multiple_unloads(self) -> None:
        """Vérifie que 10 appels simultanés à async_unload_entry s'exécutent sans exception."""
        entry = create_mock_entry("adv_conc_10_unloads", self.server)
        setup_ok = await async_setup_entry(self.hass, entry)
        self.assertTrue(setup_ok)

        # 10 unloads simultanés
        tasks = [async_unload_entry(self.hass, entry) for _ in range(10)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        self.assertEqual(len(results), 10)
        # Chaque retour doit être booléen (True) sans aucune exception levée
        for r in results:
            self.assertIsInstance(r, bool)
            self.assertTrue(r)

        self.assertFalse(self.hass.webhook.has_webhook(f"smart_remote_{entry.entry_id}"))
        self.assertNotIn(entry.entry_id, self.hass.data.get(DOMAIN, {}))
        self.assertIsNone(entry.runtime_data)

    async def test_concurrent_multiple_setups_same_entry(self) -> None:
        """Vérifie que 2 setups concurrents sur la même entrée ne provoquent ni crash ni webhook orphelin."""
        entry = create_mock_entry("adv_conc_2_setups", self.server)
        created_sessions = []
        import homeassistant.helpers.aiohttp_client as aio_helpers
        orig_create = aio_helpers.async_create_clientsession

        def tracking_create(*args: Any, **kwargs: Any) -> Any:
            sess = orig_create(*args, **kwargs)
            created_sessions.append(sess)
            return sess

        with patch.object(aio_helpers, "async_create_clientsession", side_effect=tracking_create):
            results = await asyncio.gather(
                async_setup_entry(self.hass, entry),
                async_setup_entry(self.hass, entry),
                return_exceptions=True,
            )

        for r in results:
            self.assertTrue(r is True or isinstance(r, Exception))

        # Déchargement garanti et libération de toutes les sessions créées
        await async_unload_entry(self.hass, entry)
        for s in created_sessions:
            if hasattr(s, "close") and not getattr(s, "closed", False):
                await s.close()
        for w in list(self.server.active_writers):
            try:
                w.close()
                await w.wait_closed()
            except Exception:
                pass
        await asyncio.sleep(0.02)

        self.assertFalse(self.hass.webhook.has_webhook(f"smart_remote_{entry.entry_id}"))
        self.assertNotIn(entry.entry_id, self.hass.data.get(DOMAIN, {}))
        self.assertIsNone(entry.runtime_data)

    async def test_concurrent_multi_entry_parallel_lifecycle(self) -> None:
        """Vérifie le setup parallèle de 5 télécommandes distinctes puis leur déchargement simultané."""
        entries = [create_mock_entry(f"adv_multi_{i}", self.server) for i in range(5)]

        # Setup parallèle de 5 entrées
        setup_results = await asyncio.gather(*[async_setup_entry(self.hass, e) for e in entries])
        self.assertEqual(setup_results, [True] * 5)

        for e in entries:
            self.assertTrue(self.hass.webhook.has_webhook(f"smart_remote_{e.entry_id}"))
            self.assertIn(e.entry_id, self.hass.data[DOMAIN])
            self.assertIsNotNone(e.runtime_data)

        # Unload parallèle de 5 entrées
        unload_results = await asyncio.gather(*[async_unload_entry(self.hass, e) for e in entries])
        self.assertEqual(unload_results, [True] * 5)

        for e in entries:
            self.assertFalse(self.hass.webhook.has_webhook(f"smart_remote_{e.entry_id}"))
            self.assertNotIn(e.entry_id, self.hass.data.get(DOMAIN, {}))
            self.assertIsNone(e.runtime_data)

        self.assertNotIn(DOMAIN, self.hass.data)

    async def test_unload_while_background_sync_in_progress(self) -> None:
        """Vérifie qu'un déchargement intervenant pendant qu'une sync d'arrière-plan tourne ne corrompt rien."""
        entry = create_mock_entry(
            "adv_sync_in_flight",
            self.server,
            options={CONF_TRIGGER_RESYNC: True, CONF_AUTO_SYNC_WEBHOOKS: True},
        )

        sync_started = asyncio.Event()
        sync_can_finish = asyncio.Event()

        async def controlled_sync(*args: Any, **kwargs: Any) -> bool:
            sync_started.set()
            await sync_can_finish.wait()
            return True

        with patch.object(SevenhugsLuaInjector, "async_sync_entities", side_effect=controlled_sync):
            setup_task = asyncio.create_task(async_setup_entry(self.hass, entry))
            await setup_task
            self.assertTrue(setup_task.result())

            # Attente du démarrage de la tâche de synchronisation
            await asyncio.wait_for(sync_started.wait(), timeout=1.0)

            # Déchargement pendant que sync_entities est en attente
            unload_res = await async_unload_entry(self.hass, entry)
            self.assertTrue(unload_res)

            # Libération de la tâche de sync
            sync_can_finish.set()
            await asyncio.sleep(0.05)

        # Vérification finale
        self.assertFalse(self.hass.webhook.has_webhook(f"smart_remote_{entry.entry_id}"))
        self.assertNotIn(entry.entry_id, self.hass.data.get(DOMAIN, {}))
        self.assertIsNone(entry.runtime_data)


# ============================================================================
# AXE 4 : NON-BLOCKING BACKGROUND SYNC & OPTIONS
# ============================================================================

class TestAdversarialBackgroundSync(unittest.IsolatedAsyncioTestCase):
    """Banc d'essais contradictoires sur le caractère strictement non-bloquant de la synchronisation."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.server = MockSmartRemoteServer()
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def test_async_setup_entry_returns_under_100ms_when_sync_slow(self) -> None:
        """Vérifie empiriquement que async_setup_entry retourne en < 100ms même si la sync dure 2 secondes."""
        entry = create_mock_entry(
            "adv_sync_slow",
            self.server,
            options={CONF_TRIGGER_RESYNC: True, CONF_AUTO_SYNC_WEBHOOKS: True},
        )

        async def slow_sync(*args: Any, **kwargs: Any) -> bool:
            await asyncio.sleep(2.0)
            return True

        with patch.object(SevenhugsLuaInjector, "async_sync_entities", side_effect=slow_sync):
            t0 = time.perf_counter()
            setup_ok = await async_setup_entry(self.hass, entry)
            elapsed = time.perf_counter() - t0

            self.assertTrue(setup_ok)
            # Exigence critique : temps d'exécution strictement inférieur à 100ms (généralement < 25ms)
            self.assertLess(
                elapsed,
                0.100,
                f"async_setup_entry a été bloqué par la synchronisation ! Temps mesuré: {elapsed * 1000:.2f}ms",
            )

        await async_unload_entry(self.hass, entry)

    async def test_async_setup_entry_succeeds_when_sync_throws_network_error(self) -> None:
        """Vérifie que des exceptions réseau dans l'injecteur Lua sont absorbées et n'interrompent pas le chargement."""
        entry = create_mock_entry(
            "adv_sync_err",
            self.server,
            options={
                CONF_TRIGGER_RESYNC: True,
                CONF_ENTITIES: ["light.living_room"],
            },
        )

        # Simulation d'une rupture réseau au niveau du client HTTP sous-jacent
        with patch.object(
            AsyncSmartRemoteClient,
            "async_push_webhooks",
            side_effect=SevenhugsConnectionError("Simulated remote reset / Wi-Fi disconnected"),
        ):
            setup_ok = await async_setup_entry(self.hass, entry)
            self.assertTrue(setup_ok)
            # L'intégration est fonctionnelle malgré l'échec de la synchronisation de fond
            self.assertIn(entry.entry_id, self.hass.data[DOMAIN])
            await asyncio.sleep(0.05)

        await async_unload_entry(self.hass, entry)

    async def test_sync_scheduling_matrix(self) -> None:
        """Vérifie la matrice de planification de synchronisation selon les options configurées."""
        test_cases = [
            # (options, expected_scheduled)
            ({CONF_AUTO_SYNC_WEBHOOKS: True, CONF_TRIGGER_RESYNC: False}, True),
            ({CONF_AUTO_SYNC_WEBHOOKS: False, CONF_TRIGGER_RESYNC: True}, True),
            ({CONF_AUTO_SYNC_WEBHOOKS: True, CONF_TRIGGER_RESYNC: True}, True),
            ({CONF_AUTO_SYNC_WEBHOOKS: False, CONF_TRIGGER_RESYNC: False}, False),
            ({}, DEFAULT_AUTO_SYNC_WEBHOOKS),  # Options par défaut
        ]

        for i, (opts, expected_scheduled) in enumerate(test_cases):
            with self.subTest(case_index=i, options=opts, expected=expected_scheduled):
                entry = create_mock_entry(f"adv_matrix_{i}", self.server, options=opts)
                sync_called = False

                async def spy_sync(*args: Any, **kwargs: Any) -> bool:
                    nonlocal sync_called
                    sync_called = True
                    return True

                with patch.object(SevenhugsLuaInjector, "async_sync_entities", side_effect=spy_sync):
                    setup_ok = await async_setup_entry(self.hass, entry)
                    self.assertTrue(setup_ok)

                    # Laisser l'event loop exécuter d'éventuelles micro-tâches
                    await asyncio.sleep(0.01)

                    if expected_scheduled:
                        self.assertTrue(sync_called, f"Case {i}: la synchronisation aurait dû être planifiée")
                    else:
                        self.assertFalse(sync_called, f"Case {i}: la synchronisation ne devait pas être planifiée")

                await async_unload_entry(self.hass, entry)

    async def test_update_listener_reloads_entry(self) -> None:
        """Vérifie que update_listener déclenche le rechargement via async_reload."""
        entry = create_mock_entry("adv_upd_listener", self.server)
        with patch.object(self.hass.config_entries, "async_reload", new_callable=AsyncMock) as mock_reload:
            await update_listener(self.hass, entry)
            mock_reload.assert_awaited_once_with(entry.entry_id)

    async def test_update_listener_fallback_without_reload(self) -> None:
        """Vérifie que update_listener déclenche la synchronisation directe si async_reload n'est pas disponible."""
        entry = create_mock_entry("adv_upd_fallback", self.server)
        setup_ok = await async_setup_entry(self.hass, entry)
        self.assertTrue(setup_ok)

        sync_called = False

        async def spy_sync(*args: Any, **kwargs: Any) -> bool:
            nonlocal sync_called
            sync_called = True
            return True

        runtime_data = self.hass.data[DOMAIN][entry.entry_id]
        runtime_data.injector.async_sync_entities = spy_sync

        # Simulation d'un objet config_entries sans méthode async_reload
        stripped_config_entries = types.SimpleNamespace()

        with patch.object(self.hass, "config_entries", stripped_config_entries):
            await update_listener(self.hass, entry)
            await asyncio.sleep(0.02)
            self.assertTrue(sync_called, "Le repli de secours sans async_reload doit déclencher async_sync_entities")

        await async_unload_entry(self.hass, entry)


if __name__ == "__main__":
    unittest.main()
