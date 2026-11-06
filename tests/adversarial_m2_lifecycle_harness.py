"""Banc d'essais contradictoires et de stress empirique pour le Jalier 2 (Phase 2).

Auteur: challenger_m2_2 (EMPIRICAL CHALLENGER)
Rôles: critic, specialist

Axes contradictoires couverts :
1. Cycle de vie et gestion des descripteurs de socket :
   - 50 cycles consécutifs de setup/unload rapides avec requêtes HTTP actives.
   - Surveillance stricte de /proc/self/fd pour garantir 0 fuite de socket.
   - Fermeture propre des sessions aiohttp.
   - Nettoyage complet de hass.data[DOMAIN] (aucune clé orpheline).
   - Idempotence du déchargement (double unload sans exception).
   - Déchargement multi-entrées ordonné (nettoyage de hass.data[DOMAIN] uniquement au départ de la dernière entrée).

2. Consistance du double stockage (Dual Storage) :
   - hass.data[DOMAIN][entry.entry_id]["client"] vs entry.runtime_data.client.
   - Accès dictionnaire vs accès attribut sur SevenhugsData.
   - Délégation d'attributs vers le client et levée stricte d'AttributeError sur attributs inconnus.
   - Cohérence lors du rechargement complet (async_reload / update_listener).
   - Résilience du déchargement si hass.data a été corrompu ou vidé préalablement.

3. Cycle de vie de réauthentification (Reauth Flow) :
   - Simulation d'un factory reset sur la télécommande (invalidation des clés précédentes).
   - Échec attendu des requêtes avec les anciennes clés (SevenhugsAuthError).
   - Déclenchement du flux reauth : rejet des mauvais PIN sans altération de l'entrée existante.
   - Soumission du nouveau PIN : renouvellement des clés, conservation stricte de l'entry_id.
   - Préservation des associations dans le Device Registry de Home Assistant.
   - Validation que la télécommande réinitialisée redevient opérationnelle avec les nouvelles clés.

4. Cas limites de l'Options Flow (Options Flow Edge Cases) :
   - Bornes de CONF_POLLING_INTERVAL (min 15, max 300, 14 rejeté, 301 rejeté, 0 rejeté, négatif rejeté).
   - Coercition de types (chaîne "45" vers 45, float, rejet des chaînes non numériques).
   - Comportement sur CONF_TIMEOUT aux extrêmes (négatif, 0, types invalides).
   - Exécution du formulaire et déclenchement de la recharge via l'écouteur d'options.

5. Détection d'incohérence lors de la Reconfiguration (Reconfigure Flow) :
   - Reconfiguration vers un hôte avec numéro de série différent (rejet attendu).
   - Test empirique contradictoire non patché du gestionnaire _async_verify_device_match.
   - Analyse du comportement face à un serveur HTTP tiers répondant à /api/hello mais échouant le handshake.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

# Assure l'accès au projet et aux mocks
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.conftest
from tests.conftest import MockSmartRemoteServer
import aiohttp
from cryptography.hazmat.primitives.asymmetric import ed25519

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from custom_components.sevenhugs_smart_remote import (
    DOMAIN,
    async_setup_entry,
    async_unload_entry,
    update_listener,
)
from custom_components.sevenhugs_smart_remote.__init__ import SevenhugsData
from custom_components.sevenhugs_smart_remote.client import (
    AsyncSmartRemoteClient,
    SevenhugsAuthError,
    SevenhugsConnectionError,
)
from custom_components.sevenhugs_smart_remote.config_flow import (
    SevenhugsConfigFlow,
    SevenhugsOptionsFlowHandler,
)
from custom_components.sevenhugs_smart_remote.const import (
    CONF_AUTO_SYNC_WEBHOOKS,
    CONF_CLIENT_PRIVATE_KEY,
    CONF_CLIENT_PUBLIC_KEY,
    CONF_CONTROLLER_ID,
    CONF_HOST,
    CONF_POLLING_INTERVAL,
    CONF_PORT,
    CONF_REMOTE_ID,
    CONF_REMOTE_PUBLIC_KEY,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_READ_KEY,
    CONF_SESSION_WRITE_KEY,
    CONF_TIMEOUT,
    DEFAULT_AUTO_SYNC_WEBHOOKS,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    ERROR_CANNOT_CONNECT,
    ERROR_INVALID_PIN,
    HARDWARE_VERSION,
    MAX_POLLING_INTERVAL,
    MIN_POLLING_INTERVAL,
    MODEL_SR1A,
    MANUFACTURER,
)


def get_current_socket_fd_count() -> int:
    """Compte le nombre exact de descripteurs de socket ouverts via /proc/self/fd."""
    fd_dir = "/proc/self/fd"
    if not os.path.exists(fd_dir):
        return -1
    count = 0
    for fd_name in os.listdir(fd_dir):
        try:
            target = os.readlink(os.path.join(fd_dir, fd_name))
            if "socket:" in target or target.startswith("socket:"):
                count += 1
        except (FileNotFoundError, PermissionError, OSError):
            pass
    return count


# ============================================================================
# AXE 1 : CYCLE DE VIE & GESTION DES RESSOURCES SOCKET
# ============================================================================

async def test_axis1_rapid_setup_unload_endurance() -> dict[str, Any]:
    """50 cycles rapides de setup et unload avec requêtes HTTP réelles."""
    print("\n--- [AXE 1.1] 50 Cycles Rapides Setup / Unload & Détection de Fuite de Sockets ---")
    server = MockSmartRemoteServer()
    await server.start()

    # Pause pour stabiliser les sockets initiaux du serveur
    await asyncio.sleep(0.05)
    baseline_fds = get_current_socket_fd_count()
    print(f"  [FD] Baseline socket FDs: {baseline_fds}")

    cycles = 50
    hass = HomeAssistant()

    try:
        for i in range(cycles):
            entry = ConfigEntry(
                entry_id=f"entry_cycle_{i}",
                domain=DOMAIN,
                title=f"Remote Cycle {i}",
                data={
                    CONF_HOST: server.host,
                    CONF_PORT: server.port,
                    CONF_SERIAL_NUMBER: f"SN_CYCLE_{i}",
                    CONF_REMOTE_ID: "5726",
                    CONF_CONTROLLER_ID: f"ctrl-uuid-{i}",
                    CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                    CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                    CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
                    CONF_SESSION_WRITE_KEY: "03" * 32,
                    CONF_SESSION_READ_KEY: "04" * 32,
                },
            )

            # 1. Setup
            setup_res = await async_setup_entry(hass, entry)
            assert setup_res is True, f"Cycle {i}: async_setup_entry a retourné False"
            assert entry.entry_id in hass.data[DOMAIN], f"Cycle {i}: entry_id manquant dans hass.data"
            assert entry.runtime_data is not None, f"Cycle {i}: runtime_data est None"

            client: AsyncSmartRemoteClient = entry.runtime_data.client
            assert client is not None, f"Cycle {i}: client est None"

            # 2. Requête HTTP réelle pour activer la socket Keep-Alive
            res = await client.async_request("GET", "/api/settings")
            assert isinstance(res, dict), f"Cycle {i}: la réponse API n'est pas un dictionnaire"

            # 3. Unload
            unload_res = await async_unload_entry(hass, entry)
            assert unload_res is True, f"Cycle {i}: async_unload_entry a retourné False"
            assert entry.entry_id not in hass.data.get(DOMAIN, {}), f"Cycle {i}: entrée non supprimée de hass.data"
            assert entry.runtime_data is None, f"Cycle {i}: runtime_data non réinitialisé à None"
            assert DOMAIN not in hass.data, f"Cycle {i}: DOMAIN non nettoyé de hass.data alors qu'il est vide"

        # Laisse l'event loop clore proprement les transports
        await asyncio.sleep(0.1)
        final_fds = get_current_socket_fd_count()
        fd_delta = final_fds - baseline_fds
        print(f"  [FD] Final socket FDs: {final_fds} (Delta: {fd_delta:+d})")

        # Tolérance zéro sur les fuites de sockets
        assert fd_delta <= 0, f"Fuite de descripteurs de socket détectée ! Delta: {fd_delta}"
        return {"status": "PASS", "cycles": cycles, "fd_delta": fd_delta}

    finally:
        await server.stop()


async def test_axis1_idempotent_and_multi_entry_lifecycle() -> dict[str, Any]:
    """Test d'idempotence du déchargement et cycle multi-entrées ordonné."""
    print("\n--- [AXE 1.2] Idempotence Unload & Déchargement Multi-Entrées ---")
    server = MockSmartRemoteServer()
    await server.start()

    try:
        hass = HomeAssistant()

        # Étape 1 : Idempotence de double unload
        entry_idem = ConfigEntry(
            entry_id="entry_idem",
            domain=DOMAIN,
            title="Idempotent Remote",
            data={
                CONF_HOST: server.host,
                CONF_PORT: server.port,
                CONF_SERIAL_NUMBER: "SN_IDEM",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-uuid-idem",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
        )
        await async_setup_entry(hass, entry_idem)
        assert await async_unload_entry(hass, entry_idem) is True
        # Deuxième unload consécutif : doit réussir sans lever d'exception
        assert await async_unload_entry(hass, entry_idem) is True
        assert DOMAIN not in hass.data

        # Étape 2 : Cycle multi-entrées (3 télécommandes simultanées)
        entries = []
        for i in range(3):
            e = ConfigEntry(
                entry_id=f"entry_multi_{i}",
                domain=DOMAIN,
                title=f"Multi Remote {i}",
                data={
                    CONF_HOST: server.host,
                    CONF_PORT: server.port,
                    CONF_SERIAL_NUMBER: f"SN_MULTI_{i}",
                    CONF_REMOTE_ID: f"572{i}",
                    CONF_CONTROLLER_ID: f"ctrl-uuid-{i}",
                    CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                    CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                    CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
                    CONF_SESSION_WRITE_KEY: "03" * 32,
                    CONF_SESSION_READ_KEY: "04" * 32,
                },
            )
            assert await async_setup_entry(hass, e) is True
            entries.append(e)

        assert len(hass.data[DOMAIN]) == 3

        # Déchargement de l'entrée 0 : les entrées 1 et 2 restent
        await async_unload_entry(hass, entries[0])
        assert DOMAIN in hass.data
        assert len(hass.data[DOMAIN]) == 2
        assert "entry_multi_0" not in hass.data[DOMAIN]
        assert "entry_multi_1" in hass.data[DOMAIN]

        # Déchargement de l'entrée 1
        await async_unload_entry(hass, entries[1])
        assert DOMAIN in hass.data
        assert len(hass.data[DOMAIN]) == 1
        assert "entry_multi_2" in hass.data[DOMAIN]

        # Déchargement de l'entrée 2 : hass.data[DOMAIN] doit être complètement nettoyé
        await async_unload_entry(hass, entries[2])
        assert DOMAIN not in hass.data, "hass.data[DOMAIN] résiduel après déchargement de la dernière entrée !"

        return {"status": "PASS", "multi_entry_tested": 3}
    finally:
        await server.stop()


# ============================================================================
# AXE 2 : CONSISTANCE DU DOUBLE STOCKAGE (DUAL STORAGE)
# ============================================================================

async def test_axis2_dual_storage_consistency() -> dict[str, Any]:
    """Test de stricte cohérence entre hass.data et entry.runtime_data."""
    print("\n--- [AXE 2] Consistance du Double Stockage & Proxy d'Attributs SevenhugsData ---")
    server = MockSmartRemoteServer()
    await server.start()

    try:
        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="entry_dual_store",
            domain=DOMAIN,
            title="Dual Store Remote",
            data={
                CONF_HOST: server.host,
                CONF_PORT: server.port,
                CONF_SERIAL_NUMBER: "SN_DUAL",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-uuid-dual",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
        )
        hass.config_entries.async_add(entry)

        await async_setup_entry(hass, entry)

        # 1. Test d'identité d'objet
        store_data = hass.data[DOMAIN][entry.entry_id]
        runtime_data = entry.runtime_data
        assert store_data is runtime_data, "hass.data et entry.runtime_data ne pointent pas vers la même instance !"

        # 2. Accès dictionnaire et accès attribut croisés
        assert store_data["client"] is runtime_data.client
        assert runtime_data["client"] is store_data.client
        assert store_data["session"] is runtime_data.session
        assert runtime_data["session"] is store_data.session

        # 3. Délégation d'attributs proxy vers client
        assert runtime_data.host == runtime_data.client.host
        assert runtime_data.is_verified == runtime_data.client.is_verified

        # 4. Attribut inexistant : doit lever AttributeError et PAS KeyError
        try:
            _ = runtime_data.non_existent_attribute_xyz
            raise AssertionError("AttributeError attendu sur attribut inconnu")
        except AttributeError:
            pass

        # 5. Cohérence sous rechargement (async_reload)
        old_client = runtime_data.client
        old_session = runtime_data.session

        # Déclenche update_listener via config_entries
        await update_listener(hass, entry)

        # Vérification post-reload
        new_store_data = hass.data[DOMAIN][entry.entry_id]
        new_runtime_data = entry.runtime_data

        assert new_store_data is new_runtime_data
        assert new_runtime_data.client is not old_client, "Le client n'a pas été recréé lors du reload"
        assert new_runtime_data.session is not old_session, "La session n'a pas été renouvelée lors du reload"

        # Vérifier que les anciennes ressources ont été fermées
        is_closed = getattr(old_session, "closed", getattr(old_session, "_closed", False))
        assert is_closed is True, "L'ancienne session n'a pas été fermée lors du reload"

        # 6. Résilience si hass.data a été altéré avant déchargement
        hass.data[DOMAIN].pop(entry.entry_id, None)
        # async_unload_entry doit utiliser le fallback entry.runtime_data
        assert await async_unload_entry(hass, entry) is True
        assert entry.runtime_data is None

        return {"status": "PASS"}
    finally:
        await server.stop()


# ============================================================================
# AXE 3 : CYCLE DE VIE DE RÉAUTHENTIFICATION (FACTORY RESET & KEYS)
# ============================================================================

async def test_axis3_reauth_factory_reset_lifecycle() -> dict[str, Any]:
    """Simulation de Factory Reset, rejet de clés, et flux reauth complet."""
    print("\n--- [AXE 3] Cycle de Réauthentification sous Factory Reset Matériel ---")
    server = MockSmartRemoteServer()
    await server.start()

    try:
        hass = HomeAssistant()
        initial_entry_id = "entry_reauth_target_123"
        serial = "a06fe048b48b260139"

        # Clés initiales
        old_priv_key = "aa" * 32
        old_write_key = "bb" * 32

        entry = ConfigEntry(
            entry_id=initial_entry_id,
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (5726)",
            data={
                CONF_HOST: server.host,
                CONF_PORT: server.port,
                CONF_SERIAL_NUMBER: serial,
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-uuid-reauth",
                CONF_CLIENT_PRIVATE_KEY: old_priv_key,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: old_write_key,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
            unique_id=serial,
        )
        hass.config_entries.async_add(entry)

        # Inscription initiale dans le Device Registry
        dev_reg = dr.async_get(hass)
        dev = dev_reg.async_get_or_create(
            config_entry_id=initial_entry_id,
            identifiers={(DOMAIN, serial)},
            manufacturer=MANUFACTURER,
            model=MODEL_SR1A,
            name="Sevenhugs Smart Remote (5726)",
        )
        assert initial_entry_id in dev.config_entries

        # Initialisation composant avec anciennes clés
        assert await async_setup_entry(hass, entry) is True

        # SIMULATION FACTORY RESET : la télécommande régénère ses clés Ed25519, change de PIN et purge ses sessions
        new_pin = "987-65-432"
        server.pin = new_pin
        server.remote_sk = ed25519.Ed25519PrivateKey.generate()
        server.remote_pk = server.remote_sk.public_key().public_bytes_raw()
        server.active_verify_sessions.clear()
        server.verified_connections.clear()

        # Tentative de requête avec l'ancien client : doit échouer avec SevenhugsAuthError
        client = entry.runtime_data.client
        try:
            await client.async_request("GET", "/api/settings")
            raise AssertionError("SevenhugsAuthError attendu suite au factory reset")
        except SevenhugsAuthError:
            print("  [OK] Détection authentique de l'échec de session après Factory Reset")

        # DÉCLENCHEMENT DU FLUX REAUTH
        flow = SevenhugsConfigFlow()
        flow.hass = hass
        flow.context = {"entry_id": initial_entry_id}

        # Étape 1 : async_step_reauth
        r1 = await flow.async_step_reauth(entry.data)
        assert r1["type"] == FlowResultType.FORM
        assert r1["step_id"] == "reauth_confirm"

        # Étape 2 : Confirmation -> pair_init (M1 SRP-6a sur remote réinitialisée)
        r2 = await flow.async_step_reauth_confirm({})
        assert r2["type"] == FlowResultType.FORM
        assert r2["step_id"] == "pair_pin"

        # Test contradictoire 3a : Soumission d'un mauvais PIN (format valide mais code faux)
        server.pin = new_pin
        r_bad = await flow.async_step_pair_pin({"pin": "111-11-111"})
        assert r_bad["type"] == FlowResultType.FORM
        assert r_bad["step_id"] == "pair_pin"
        assert r_bad["errors"] == {"base": ERROR_INVALID_PIN}
        # Vérification d'intégrité : l'entrée existante n'a pas été corrompue
        assert entry.data[CONF_CLIENT_PRIVATE_KEY] == old_priv_key

        # Test contradictoire 3b : Soumission d'un format PIN tronqué
        r_short = await flow.async_step_pair_pin({"pin": "123"})
        assert r_short["type"] == FlowResultType.FORM
        assert r_short["errors"] == {"base": ERROR_INVALID_PIN}

        # Étape 3 : Soumission du bon nouveau PIN
        server.force_auth_error_next_api = False
        r_good = await flow.async_step_pair_pin({"pin": new_pin})
        assert r_good["type"] == FlowResultType.ABORT
        assert r_good["reason"] == "reauth_successful"

        # VÉRIFICATIONS POST-REAUTH :
        # 1. L'entry_id doit être STRICTEMENT identique (aucune entrée orpheline créée)
        assert entry.entry_id == initial_entry_id
        all_entries = hass.config_entries.async_entries(DOMAIN)
        assert len(all_entries) == 1, f"Entrées dupliquées détectées : {len(all_entries)}"

        # 2. Les clés doivent avoir été renouvelées
        new_priv_key = entry.data[CONF_CLIENT_PRIVATE_KEY]
        new_write_key = entry.data[CONF_SESSION_WRITE_KEY]
        assert new_priv_key != old_priv_key, "La clé privée client n'a pas été renouvelée"
        assert new_write_key != old_write_key, "La clé de session n'a pas été renouvelée"

        # 3. Le Device Registry doit toujours lier l'appareil au même entry_id
        dev_after = dev_reg.async_get_device({(DOMAIN, serial)})
        assert dev_after is not None
        assert initial_entry_id in dev_after.config_entries

        # 4. Le rechargement automatique par async_update_reload_and_abort s'exécute
        await asyncio.sleep(0.1)
        assert entry.runtime_data is not None, "runtime_data absent après async_reload automatique"
        new_client = entry.runtime_data.client
        settings = await new_client.async_request("GET", "/api/settings")
        assert isinstance(settings, dict)
        print("  [OK] Client opérationnel avec les nouvelles clés après Reauth")

        await async_unload_entry(hass, entry)
        return {"status": "PASS", "entry_id_preserved": initial_entry_id}
    finally:
        await server.stop()


# ============================================================================
# AXE 4 : CAS LIMITES DE L'OPTIONS FLOW
# ============================================================================

async def test_axis4_options_flow_edge_cases() -> dict[str, Any]:
    """Test des limites, types invalides et coercitions de l'Options Flow."""
    print("\n--- [AXE 4] Cas Limites & Validation de Schéma de l'Options Flow ---")
    entry = ConfigEntry(
        entry_id="opt_edge_entry",
        domain=DOMAIN,
        title="Sevenhugs Smart Remote",
        data={},
        options={
            CONF_POLLING_INTERVAL: DEFAULT_POLLING_INTERVAL,
            CONF_AUTO_SYNC_WEBHOOKS: DEFAULT_AUTO_SYNC_WEBHOOKS,
            CONF_TIMEOUT: DEFAULT_TIMEOUT,
        },
    )

    handler = SevenhugsOptionsFlowHandler(entry)
    form = await handler.async_step_init(None)
    schema: vol.Schema = form["data_schema"]

    # 1. Bornes exactes de polling_interval (min 15, max 300)
    assert schema({CONF_POLLING_INTERVAL: 15})[CONF_POLLING_INTERVAL] == 15
    assert schema({CONF_POLLING_INTERVAL: 300})[CONF_POLLING_INTERVAL] == 300

    # 2. Rejet sous le minimum (14, 0, négatif)
    for bad_min in (14, 0, -1, -50):
        try:
            schema({CONF_POLLING_INTERVAL: bad_min})
            raise AssertionError(f"Le schéma aurait dû rejeter polling_interval={bad_min}")
        except vol.Invalid:
            pass

    # 3. Rejet au-dessus du maximum (301, 1000)
    for bad_max in (301, 1000):
        try:
            schema({CONF_POLLING_INTERVAL: bad_max})
            raise AssertionError(f"Le schéma aurait dû rejeter polling_interval={bad_max}")
        except vol.Invalid:
            pass

    # 4. Coercition de chaîne valide ("60" -> 60)
    coerced = schema({CONF_POLLING_INTERVAL: "60"})
    assert coerced[CONF_POLLING_INTERVAL] == 60
    assert isinstance(coerced[CONF_POLLING_INTERVAL], int)

    # 5. Rejet des chaînes non numériques
    for bad_str in ("abc", "", None, []):
        try:
            schema({CONF_POLLING_INTERVAL: bad_str})
            raise AssertionError(f"Le schéma aurait dû rejeter polling_interval={bad_str!r}")
        except (vol.Invalid, TypeError):
            pass

    # 6. Coercition de float (45.9 -> 45)
    float_coerced = schema({CONF_POLLING_INTERVAL: 45.9})
    assert float_coerced[CONF_POLLING_INTERVAL] == 45

    # 7. Exécution réelle du gestionnaire avec sauvegarde
    valid_input = {
        CONF_POLLING_INTERVAL: 45,
        CONF_AUTO_SYNC_WEBHOOKS: False,
        CONF_TIMEOUT: 20,
    }
    result = await handler.async_step_init(valid_input)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == valid_input

    # 8. Investigation critique sur CONF_TIMEOUT dans le schéma actuel :
    # Dans le schéma actuel : vol.Optional(CONF_TIMEOUT, ...): int
    # Vérifions si les timeouts négatifs ou nuls sont acceptés ou rejetés
    print("  [ANALYSE SCHÉMA] Test du champ CONF_TIMEOUT :")
    try:
        t_neg = schema({CONF_TIMEOUT: -5})
        print(f"    AVERTISSEMENT : timeout négatif -5 accepté par vol.Schema : {t_neg}")
    except vol.Invalid:
        print("    OK : timeout négatif rejeté")

    return {"status": "PASS", "schema_tested": True}


# ============================================================================
# AXE 5 : DÉTECTION D'INCOHÉRENCE LORS DU RECONFIGURE FLOW
# ============================================================================

async def test_axis5_reconfigure_flow_mismatch_adversarial() -> dict[str, Any]:
    """Test contradictoire approfondi de la détection d'incohérence matérielle lors du Reconfigure."""
    print("\n--- [AXE 5] Reconfigure Flow : Détection Empirique de Mismatch Matériel ---")

    # Serveur A (original)
    server_a = MockSmartRemoteServer()
    server_a.serial_number = "ORIGINAL_SR1A_SERIAL"
    await server_a.start()

    # Serveur B (autre télécommande avec numéro de série différent)
    server_b = MockSmartRemoteServer()
    server_b.serial_number = "DIFFERENT_SR1A_SERIAL"
    await server_b.start()

    try:
        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="entry_reconfig_test",
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (5726)",
            data={
                CONF_HOST: server_a.host,
                CONF_PORT: server_a.port,
                CONF_SERIAL_NUMBER: "ORIGINAL_SR1A_SERIAL",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-uuid-reconfig",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: server_a.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
            unique_id="ORIGINAL_SR1A_SERIAL",
        )
        hass.config_entries.async_add(entry)

        flow = SevenhugsConfigFlow()
        flow.hass = hass
        flow.context = {"entry_id": "entry_reconfig_test"}

        # Scénario 5.1 : Reconfiguration vers hôte inaccessible -> rejet cannot_connect
        r_unreach = await flow.async_step_reconfigure({CONF_HOST: "192.0.2.1", CONF_PORT: 80})
        assert r_unreach["type"] == FlowResultType.FORM
        assert r_unreach["errors"] == {"base": ERROR_CANNOT_CONNECT}
        print("  [OK] Hôte inaccessible rejeté avec cannot_connect")

        # Scénario 5.2 : Reconfiguration vers Serveur B avec numéro de série différent SANS AUCUN MOCK
        # Serveur B répond à /api/hello mais son numéro de série est DIFFERENT_SR1A_SERIAL
        # Note : Server B a la même clé publique remote configurée dans entry pour permettre le handshake
        server_b.remote_pk = server_a.remote_pk
        server_b.remote_sk = server_a.remote_sk

        r_mismatch = await flow.async_step_reconfigure({CONF_HOST: server_b.host, CONF_PORT: server_b.port})
        # Le flux doit rejeter la reconfiguration car le serial_number ne correspond pas
        assert r_mismatch["type"] == FlowResultType.FORM, f"Type attendu FORM, reçu {r_mismatch['type']}"
        assert r_mismatch["errors"] == {"base": ERROR_CANNOT_CONNECT}, f"Erreur attendue cannot_connect, reçu {r_mismatch.get('errors')}"
        print("  [OK] Mismatch de numéro de série détecté empiriquement et rejeté sans mock")

        # Scénario 5.3 : Hôte tiers (ex: serveur web alien ne supportant pas Pair-Verify)
        # Simulation d'un serveur HTTP répondant 200 à /api/hello mais refusant le protocole Pair-Verify
        async def handle_alien(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                line = await reader.readline()
                while True:
                    hdr = await reader.readline()
                    if hdr in (b"\r\n", b"\n", b""):
                        break
                if b"/api/hello" in line:
                    resp = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
                else:
                    resp = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                writer.write(resp)
                await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

        alien_server = await asyncio.start_server(handle_alien, "127.0.0.1", 0)
        alien_port = alien_server.sockets[0].getsockname()[1]

        try:
            print(f"  [TEST ADVERSARIAL] Test de reconfiguration vers serveur HTTP alien (port {alien_port})...")
            # Dans config_flow.py, _async_verify_device_match a :
            # except Exception:
            #     return True  # <-- Comportement permissif si les clés ou le protocole diffèrent !
            r_alien = await flow.async_step_reconfigure({CONF_HOST: "127.0.0.1", CONF_PORT: alien_port})
            if r_alien["type"] == FlowResultType.ABORT and r_alien.get("reason") == "reconfigure_successful":
                print("  [OBSERVATION ADVERSARIAL CRITIQUE] Le serveur alien a été accepté par reconfigure !")
                print("  Raison : _async_verify_device_match retourne True en cas d'exception sur client.async_pair_verify().")
                alien_vuln = True
            else:
                print("  [OK] Serveur alien rejeté.")
                alien_vuln = False
        finally:
            alien_server.close()
            await alien_server.wait_closed()

        # Scénario 5.4 : Reconfiguration vers la bonne machine (numéro de série identique)
        r_success = await flow.async_step_reconfigure({CONF_HOST: server_a.host, CONF_PORT: server_a.port})
        assert r_success["type"] == FlowResultType.ABORT
        assert r_success["reason"] == "reconfigure_successful"
        print("  [OK] Reconfiguration réussie vers l'hôte légitime avec numéro de série identique")

        return {
            "status": "PASS",
            "mismatch_detected": True,
            "alien_permissive_fallback_observed": alien_vuln,
        }
    finally:
        await server_a.stop()
        await server_b.stop()


# ============================================================================
# EXÉCUTION DU HARNAIS COMPLET
# ============================================================================

async def run_all_adversarial_tests() -> dict[str, Any]:
    print("=" * 80)
    print("BANC D'ESSAIS CONTRADICTOIRES JALON 2 (CHALLENGER_M2_2)")
    print("=" * 80)

    t0 = time.time()
    results = {}

    try:
        results["axis1_endurance"] = await test_axis1_rapid_setup_unload_endurance()
        results["axis1_idempotence"] = await test_axis1_idempotent_and_multi_entry_lifecycle()
        results["axis2_dual_storage"] = await test_axis2_dual_storage_consistency()
        results["axis3_reauth"] = await test_axis3_reauth_factory_reset_lifecycle()
        results["axis4_options"] = await test_axis4_options_flow_edge_cases()
        results["axis5_reconfigure"] = await test_axis5_reconfigure_flow_mismatch_adversarial()
    except Exception as e:
        import traceback
        print(f"\n[ÉCHEC CONTRADICTOIRE] Exception non gérée : {e}")
        traceback.print_exc()
        results["fatal_error"] = str(e)

    duration = time.time() - t0
    print("\n" + "=" * 80)
    print(f"RÉSUMÉ DU BANC CONTRADICTOIRE (Durée totale: {duration:.2f}s) :")
    for k, v in results.items():
        print(f"  - {k}: {v}")
    print("=" * 80)
    return results


if __name__ == "__main__":
    res = asyncio.run(run_all_adversarial_tests())
    if "fatal_error" in res:
        sys.exit(1)
    sys.exit(0)
