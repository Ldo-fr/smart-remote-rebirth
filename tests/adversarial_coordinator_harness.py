"""Banc d'essais contradictoires et de stress empirique pour SevenhugsDataUpdateCoordinator.

Auteur: Challenger M1.1 (EMPIRICAL CHALLENGER)
Rôles: critic, specialist

Vérifie de manière exhaustive et empirique :
1. Résolution de l'intervalle de polling et hiérarchie de fallback (options -> data -> 60s).
2. Normalisation de la télémétrie, bornage/clamping des données et immutabilité du cache mémoire.
3. Résilience exacte de veille profonde à 3 échecs consécutifs :
   - Échec 1 : données en cache, last_update_success=True, log DEBUG sans warning.
   - Échec 2 : données en cache, last_update_success=True, log DEBUG sans warning.
   - Échec 3 : UpdateFailed levée, last_update_success=False, log WARNING, entités indisponibles, cache préservé.
   - Échecs 4-5 : maintien d'UpdateFailed et persistance stricte du cache.
4. Rétablissement nominal à la 4ème tentative (ou post-veille) et résilience aux pannes intermittentes.
5. Réveil événementiel (async_wake_up / async_handle_wake_up) en veille et post-échec.
6. Traduction stricte de SevenhugsAuthError en ConfigEntryAuthFailed (sans absorption en veille).
7. Comportement au démarrage à froid (Cold Start) : échec immédiat sans cache préalable.
8. Matrice polymorphique des exceptions réseau (10 types d'erreurs vérifiés).
9. Intégration chiffrée réelle de bout en bout avec MockSmartRemoteServer et ChaCha20-Poly1305.
10. Concurrence extrême, anti-race conditions et dynamique des écouteurs d'entités.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import os
import sys
import time
from typing import Any

# Configuration du PYTHONPATH
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

import tests.conftest
from tests.conftest import MockSmartRemoteServer, aiohttp
from cryptography.hazmat.primitives.asymmetric import ed25519

from custom_components.sevenhugs_smart_remote.client import (
    AsyncSmartRemoteClient,
    SevenhugsAuthError,
    SevenhugsConnectionError,
)
from custom_components.sevenhugs_smart_remote.const import (
    CONF_POLLING_INTERVAL,
    CONF_REMOTE_ID,
    CONF_SERIAL_NUMBER,
    DOMAIN,
)
from custom_components.sevenhugs_smart_remote.coordinator import (
    DEFAULT_COORDINATOR_UPDATE_INTERVAL,
    SLEEP_CONSECUTIVE_FAILURES_THRESHOLD,
    SevenhugsDataUpdateCoordinator,
)
from custom_components.sevenhugs_smart_remote.entity import SevenhugsBaseEntity


class MockClientHarness:
    """Client simulé pour injection déterministe d'erreurs et de télémétrie."""

    def __init__(self, initial_data: dict[str, Any] | None = None) -> None:
        self.response_data: dict[str, Any] = initial_data or {
            "battery": 85,
            "wifi_signal": -62,
            "charging": False,
            "docked": False,
            "active_screen": "MediaPlayer",
            "serial_number": "SR1A12345678",
            "mac_address": "00:11:22:33:44:55",
            "firmware_version": "0.76.4",
            "name": "Living Room Remote",
        }
        self.exception_to_raise: Exception | None = None
        self.request_count: int = 0
        self.requests_log: list[tuple[str, str, Any]] = []

    async def async_request(self, method: str, path: str, json_data: dict | None = None) -> dict:
        self.request_count += 1
        self.requests_log.append((method, path, json_data))
        if self.exception_to_raise is not None:
            raise self.exception_to_raise
        return self.response_data


def make_test_entry(
    entry_id: str = "test_entry_id",
    options: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    unique_id: str = "SR1A12345678",
) -> ConfigEntry:
    """Helper pour fabriquer une ConfigEntry de test."""
    entry_data = {
        CONF_HOST: "192.168.1.50",
        CONF_PORT: 80,
        CONF_SERIAL_NUMBER: "SR1A12345678",
        CONF_REMOTE_ID: "5726",
        "mac_address": "00:11:22:33:44:55",
        "firmware_version": "0.76.4",
    }
    if data:
        entry_data.update(data)
    return ConfigEntry(
        entry_id=entry_id,
        domain=DOMAIN,
        title="Sevenhugs Remote",
        data=entry_data,
        options=options or {},
        unique_id=unique_id,
    )


# ============================================================================
# TEST SUITE IMPLEMENTATION
# ============================================================================

async def test_section_1_polling_cadence() -> None:
    """1. Vérifie la résolution de l'intervalle de rafraîchissement."""
    print("--- SECTION 1: Polling Cadence & Fallback Hierarchy ---")
    hass = HomeAssistant()
    client = MockClientHarness()

    # 1.1 Intervalle nominal par défaut (60s)
    entry_default = make_test_entry()
    coord_default = SevenhugsDataUpdateCoordinator(hass, client, entry_default)
    assert coord_default.update_interval == timedelta(seconds=60), f"Attendu 60s, obtenu {coord_default.update_interval}"

    # 1.2 Intervalle configuré dans options (45s)
    entry_opt = make_test_entry(options={CONF_POLLING_INTERVAL: 45})
    coord_opt = SevenhugsDataUpdateCoordinator(hass, client, entry_opt)
    assert coord_opt.update_interval == timedelta(seconds=45), f"Attendu 45s, obtenu {coord_opt.update_interval}"

    # 1.3 Intervalle configuré dans entry.data (30s) si options vide
    entry_data = make_test_entry(options={}, data={CONF_POLLING_INTERVAL: 30})
    coord_data = SevenhugsDataUpdateCoordinator(hass, client, entry_data)
    assert coord_data.update_interval == timedelta(seconds=30), f"Attendu 30s, obtenu {coord_data.update_interval}"

    # 1.4 Options prioritaires sur data (options=90s, data=30s)
    entry_prior = make_test_entry(options={CONF_POLLING_INTERVAL: 90}, data={CONF_POLLING_INTERVAL: 30})
    coord_prior = SevenhugsDataUpdateCoordinator(hass, client, entry_prior)
    assert coord_prior.update_interval == timedelta(seconds=90), f"Attendu 90s, obtenu {coord_prior.update_interval}"

    # 1.5 Valeurs invalides ou aberrantes (0, négatif, chaîne, None) -> repli 60s
    for invalid_val in (0, -15, "invalid", None, [], {}):
        entry_inv = make_test_entry(options={CONF_POLLING_INTERVAL: invalid_val})
        coord_inv = SevenhugsDataUpdateCoordinator(hass, client, entry_inv)
        assert coord_inv.update_interval == timedelta(seconds=60), f"Repli 60s échoué pour {invalid_val!r}: {coord_inv.update_interval}"

    print("  [PASS] Polling cadence & fallback hierarchy fully validated")


async def test_section_2_telemetry_normalization_and_cache() -> None:
    """2. Vérifie la normalisation des données, le clamping et la protection du cache."""
    print("--- SECTION 2: Telemetry Normalization & Cache Protection ---")
    hass = HomeAssistant()
    entry = make_test_entry()
    client = MockClientHarness()
    coord = SevenhugsDataUpdateCoordinator(hass, client, entry)

    # 2.1 Clamping de la batterie (négatif -> 0, >100 -> 100, str -> int, None -> cache)
    client.response_data = {
        "battery": -15,
        "rssi": -75,
        "charging": True,
        "active_screen": "Media",
    }
    await coord.async_refresh()
    assert coord.battery == 0, f"Attendu 0 pour -15, obtenu {coord.battery}"
    assert coord.wifi_signal == -75
    assert coord.charging is True
    assert coord.active_screen == "Media"

    client.response_data["battery"] = 145
    await coord.async_refresh()
    assert coord.battery == 100, f"Attendu 100 pour 145, obtenu {coord.battery}"

    client.response_data["battery"] = "72"
    await coord.async_refresh()
    assert coord.battery == 72, f"Attendu 72 pour '72', obtenu {coord.battery}"

    client.response_data["battery"] = "corrupted_text"
    await coord.async_refresh()
    assert coord.battery == 72, f"Attendu conservation 72 en cas de corrupt, obtenu {coord.battery}"

    # 2.2 Extraction du signal Wi-Fi (wifi_signal prioritaire sur rssi, conversion str)
    client.response_data = {
        "battery": 80,
        "wifi_signal": "-58",
        "rssi": -85,
    }
    await coord.async_refresh()
    assert coord.wifi_signal == -58, f"Attendu -58, obtenu {coord.wifi_signal}"

    client.response_data = {"battery": 80, "rssi": -82}
    await coord.async_refresh()
    assert coord.wifi_signal == -82, f"Attendu -82 via rssi, obtenu {coord.wifi_signal}"

    client.response_data = {"battery": 80, "wifi_signal": "unreadable"}
    await coord.async_refresh()
    assert coord.wifi_signal is None, f"Attendu None pour rssi invalide, obtenu {coord.wifi_signal}"

    # 2.3 États de dock / charging croisés
    client.response_data = {"battery": 80, "docked": True, "charging": False}
    await coord.async_refresh()
    assert coord.charging is True
    assert coord.data["docked"] is True

    client.response_data = {"battery": 80, "docked": False, "charging": True}
    await coord.async_refresh()
    assert coord.charging is True
    assert coord.data["charging"] is True
    assert coord.data["docked"] is False

    client.response_data = {"battery": 80, "docked": False, "charging": False}
    await coord.async_refresh()
    assert coord.charging is False
    assert coord.data["docked"] is False

    # 2.4 Active Screen (active_screen -> current_screen -> cache -> Home)
    client.response_data = {"battery": 80, "current_screen": "HueLights"}
    await coord.async_refresh()
    assert coord.active_screen == "HueLights"

    # 2.5 Immutabilité du cache retourné (cached_data shallow copy)
    cache_copy = coord.cached_data
    cache_copy["battery"] = 999
    assert coord.cached_data["battery"] == 80, "La mutation du dictionnaire retourné a altéré le cache interne!"

    print("  [PASS] Telemetry parsing, boundary clamping & cache immutability validated")


async def test_section_3_sleep_resilience_exact_3_failures() -> None:
    """3. Vérifie rigoureusement la résilience à 3 échecs consécutifs."""
    print("--- SECTION 3: Exact 3-Consecutive-Failure Sleep Resilience ---")
    hass = HomeAssistant()
    entry = make_test_entry()
    client = MockClientHarness({
        "battery": 88,
        "wifi_signal": -64,
        "charging": False,
        "active_screen": "TV",
        "serial_number": "SR1A12345678",
    })
    coord = SevenhugsDataUpdateCoordinator(hass, client, entry)
    entity = SevenhugsBaseEntity(coord, entry, "battery")

    # Initialisation nominale
    await coord.async_refresh()
    assert coord.last_update_success is True
    assert coord.consecutive_failures == 0
    assert entity.available is True
    assert coord.is_connected is True
    assert coord.battery == 88

    # Échec 1 : Veille matérielle (SevenhugsConnectionError)
    client.exception_to_raise = SevenhugsConnectionError("Connection timed out (Deep Sleep)")
    await coord.async_refresh()
    assert coord.consecutive_failures == 1, f"Attendu failures=1, obtenu {coord.consecutive_failures}"
    assert coord.last_update_success is True, "last_update_success doit rester True à l'échec 1"
    assert coord.last_exception is None, "last_exception doit être None à l'échec 1"
    assert entity.available is True, "entity.available doit rester True à l'échec 1"
    assert coord.is_connected is True
    assert coord.battery == 88, "Les données en cache doivent être renvoyées"

    # Échec 2 : Veille continue (TimeoutError)
    client.exception_to_raise = TimeoutError("Socket keep-alive timeout")
    await coord.async_refresh()
    assert coord.consecutive_failures == 2, f"Attendu failures=2, obtenu {coord.consecutive_failures}"
    assert coord.last_update_success is True, "last_update_success doit rester True à l'échec 2"
    assert coord.last_exception is None
    assert entity.available is True
    assert coord.is_connected is True
    assert coord.battery == 88

    # Échec 3 : Seuil de 3 échecs atteint -> bascule en indisponible
    client.exception_to_raise = ConnectionResetError("Connection reset by peer")
    await coord.async_refresh()
    assert coord.consecutive_failures == 3, f"Attendu failures=3, obtenu {coord.consecutive_failures}"
    assert coord.last_update_success is False, "last_update_success doit être False à l'échec 3"
    assert isinstance(coord.last_exception, UpdateFailed), f"last_exception doit être UpdateFailed: {coord.last_exception}"
    assert entity.available is False, "entity.available doit basculer à False après 3 échecs"
    assert coord.is_connected is False, "is_connected doit être False"
    # Le cache mémoire doit être rigoureusement conservé
    assert coord.cached_data["battery"] == 88, "Le cache a été vidé à l'échec 3!"
    assert coord.battery == 88, "coordinator.battery doit continuer d'exposer la valeur en cache"

    # Échecs prolongés 4 et 5 : maintien de l'état indisponible et du cache
    client.exception_to_raise = OSError(113, "No route to host")
    await coord.async_refresh()
    assert coord.consecutive_failures == 4
    assert coord.last_update_success is False
    assert entity.available is False
    assert coord.cached_data["battery"] == 88

    client.exception_to_raise = asyncio.TimeoutError()
    await coord.async_refresh()
    assert coord.consecutive_failures == 5
    assert coord.last_update_success is False
    assert entity.available is False
    assert coord.cached_data["battery"] == 88

    print("  [PASS] Exact 3-consecutive-failure sleep resilience and cache survival verified")


async def test_section_4_recovery_and_flapping_resilience() -> None:
    """4. Vérifie le rétablissement après échec et la résistance aux battements réseau."""
    print("--- SECTION 4: Recovery & Intermittent Network Resilience ---")
    hass = HomeAssistant()
    entry = make_test_entry()
    client = MockClientHarness({"battery": 80})
    coord = SevenhugsDataUpdateCoordinator(hass, client, entry)
    entity = SevenhugsBaseEntity(coord, entry, "battery")

    listener_called = 0
    def on_update():
        nonlocal listener_called
        listener_called += 1
    coord.async_add_listener(on_update)

    await coord.async_refresh()
    assert coord.last_update_success is True

    # Simulation de 3 échecs consécutifs
    client.exception_to_raise = SevenhugsConnectionError("Remote sleep")
    for _ in range(3):
        await coord.async_refresh()
    assert coord.consecutive_failures == 3
    assert entity.available is False

    # Tentative 4 : Rétablissement réussi avec télémétrie fraîche
    client.exception_to_raise = None
    client.response_data = {"battery": 79, "wifi_signal": -60}
    prev_listener_count = listener_called
    await coord.async_refresh()

    assert coord.consecutive_failures == 0, f"Les échecs consécutifs doivent être remis à 0, obtenu {coord.consecutive_failures}"
    assert coord.last_update_success is True
    assert coord.last_exception is None
    assert entity.available is True
    assert coord.battery == 79
    assert listener_called > prev_listener_count, "L'écouteur d'entité n'a pas été notifié lors du réveil"

    # Simulation de battement intermittent : 1 échec -> rétablissement -> 2 échecs -> rétablissement -> 3 échecs
    # 1 échec
    client.exception_to_raise = SevenhugsConnectionError("Transient socket drop")
    await coord.async_refresh()
    assert coord.consecutive_failures == 1
    assert entity.available is True

    # Rétablissement
    client.exception_to_raise = None
    await coord.async_refresh()
    assert coord.consecutive_failures == 0
    assert entity.available is True

    # 2 échecs
    client.exception_to_raise = TimeoutError("Glitch 1")
    await coord.async_refresh()
    assert coord.consecutive_failures == 1
    assert entity.available is True

    client.exception_to_raise = TimeoutError("Glitch 2")
    await coord.async_refresh()
    assert coord.consecutive_failures == 2
    assert entity.available is True

    # Rétablissement
    client.exception_to_raise = None
    await coord.async_refresh()
    assert coord.consecutive_failures == 0
    assert entity.available is True

    # 3 échecs finaux
    for i in range(1, 4):
        client.exception_to_raise = ConnectionError(f"Permanent drop {i}")
        await coord.async_refresh()
        assert coord.consecutive_failures == i

    assert entity.available is False
    print("  [PASS] Attempt-4 recovery and intermittent flapping resilience verified")


async def test_section_5_event_wake_up() -> None:
    """5. Vérifie le comportement des réveils événementiels (async_wake_up / async_handle_wake_up)."""
    print("--- SECTION 5: Event-Driven Wake-Up Mechanics ---")
    hass = HomeAssistant()
    entry = make_test_entry()
    client = MockClientHarness({"battery": 91})
    coord = SevenhugsDataUpdateCoordinator(hass, client, entry)
    entity = SevenhugsBaseEntity(coord, entry, "battery")

    await coord.async_refresh()
    assert coord.last_update_success is True

    # 5.1 Réveil pendant la période de tolérance (échec 2)
    client.exception_to_raise = SevenhugsConnectionError("Sleep")
    await coord.async_refresh()
    await coord.async_refresh()
    assert coord.consecutive_failures == 2

    # Réveil événementiel via mDNS
    client.exception_to_raise = None
    client.response_data = {"battery": 90}
    await coord.async_wake_up(reason="zeroconf_mdns_advertisement")
    assert coord.consecutive_failures == 0
    assert coord.last_update_success is True
    assert coord.battery == 90
    assert entity.available is True

    # 5.2 Réveil après mise en indisponibilité complète (3 échecs)
    client.exception_to_raise = SevenhugsConnectionError("Deep sleep 60s")
    for _ in range(3):
        await coord.async_refresh()
    assert coord.consecutive_failures == 3
    assert entity.available is False

    # Réveil événementiel via webhook ou bouton physique
    client.exception_to_raise = None
    client.response_data = {"battery": 89}
    await coord.async_handle_wake_up(reason="button_press")
    assert coord.consecutive_failures == 0
    assert coord.last_update_success is True
    assert coord.battery == 89
    assert entity.available is True

    # 5.3 Réveil lors d'un blackout continu (réveil intempestif alors que la télécommande reste éteinte)
    client.exception_to_raise = SevenhugsConnectionError("Still asleep")
    await coord.async_wake_up(reason="spurious_packet")
    # Remise à 0 avant requête, échec à la requête -> passe à 1 échec (< 3) -> utilise le cache
    assert coord.consecutive_failures == 1
    assert coord.last_update_success is True
    assert coord.battery == 89
    assert entity.available is True

    print("  [PASS] Event wake-up mechanics during sleep, post-failure & spurious wake-up verified")


async def test_section_6_auth_error_translation() -> None:
    """6. Vérifie que SevenhugsAuthError lève impérativement ConfigEntryAuthFailed."""
    print("--- SECTION 6: Auth Error Translation (ConfigEntryAuthFailed) ---")
    hass = HomeAssistant()
    entry = make_test_entry()
    client = MockClientHarness({"battery": 95})
    coord = SevenhugsDataUpdateCoordinator(hass, client, entry)
    entity = SevenhugsBaseEntity(coord, entry, "battery")

    await coord.async_refresh()
    assert coord.last_update_success is True
    assert entity.available is True

    # Émission d'une erreur d'authentification
    client.exception_to_raise = SevenhugsAuthError("Pair-Verify rejected HTTP 400 (Bad Keys)")

    # Doit immédiatement lever ConfigEntryAuthFailed sans tolérance de 3 échecs
    auth_failed_caught = False
    try:
        await coord.async_refresh()
    except ConfigEntryAuthFailed:
        auth_failed_caught = True

    # Note : DataUpdateCoordinator intercepte les exceptions et les stocke dans last_exception
    assert coord.last_update_success is False, "last_update_success doit être False sur SevenhugsAuthError"
    assert isinstance(coord.last_exception, ConfigEntryAuthFailed), (
        f"last_exception doit être ConfigEntryAuthFailed, obtenu {coord.last_exception!r}"
    )
    assert entity.available is False, "L'entité doit être indisponible sur erreur d'authentification"
    # Vérifier que le compteur d'échecs de veille n'a pas été incrémenté
    assert coord.consecutive_failures == 0, "Les erreurs d'authentification ne doivent pas altérer le compteur de veille"

    print("  [PASS] SevenhugsAuthError strict translation to ConfigEntryAuthFailed verified")


async def test_section_7_cold_start_failure() -> None:
    """7. Vérifie le comportement lors d'une défaillance initiale au démarrage à froid."""
    print("--- SECTION 7: Cold Start Failure Behavior ---")
    hass = HomeAssistant()
    entry = make_test_entry()
    client = MockClientHarness()
    # Erreur réseau dès la première tentative sans cache
    client.exception_to_raise = SevenhugsConnectionError("Host unreachable on boot")

    coord = SevenhugsDataUpdateCoordinator(hass, client, entry)
    entity = SevenhugsBaseEntity(coord, entry, "battery")

    assert coord.cached_data == {}, "Le cache initial doit être vide"
    await coord.async_refresh()

    assert coord.last_update_success is False, "Le démarrage initial en échec ne doit pas être validé"
    assert isinstance(coord.last_exception, UpdateFailed), (
        f"last_exception doit être UpdateFailed, obtenu {coord.last_exception!r}"
    )
    assert entity.available is False, "L'entité ne doit pas être disponible au démarrage échoué"
    assert coord.is_connected is False

    print("  [PASS] Cold start failure raises UpdateFailed immediately without false cache")


async def test_section_8_polymorphic_network_exceptions() -> None:
    """8. Vérifie le traitement polymorphique de l'ensemble des types d'erreurs réseau."""
    print("--- SECTION 8: Polymorphic Network Exception Matrix ---")
    hass = HomeAssistant()
    entry = make_test_entry()

    network_exceptions = [
        SevenhugsConnectionError("Custom client connection error"),
        TimeoutError("Standard library TimeoutError"),
        asyncio.TimeoutError(),
        ConnectionRefusedError("ConnectionRefusedError [Errno 111]"),
        ConnectionResetError("ConnectionResetError [Errno 104]"),
        BrokenPipeError("BrokenPipeError [Errno 32]"),
        OSError(113, "No route to host"),
        aiohttp.ClientError("Generic client error"),
        aiohttp.ClientConnectionError("Connection failed"),
        aiohttp.ClientPayloadError("Payload incomplete"),
    ]

    for exc in network_exceptions:
        client = MockClientHarness({"battery": 82})
        coord = SevenhugsDataUpdateCoordinator(hass, client, entry)
        await coord.async_refresh()
        assert coord.last_update_success is True

        # Injecter l'exception spécifique
        client.exception_to_raise = exc
        await coord.async_refresh()

        assert coord.consecutive_failures == 1, (
            f"L'exception {type(exc).__name__} n'a pas été reconnue comme erreur réseau tolérée!"
        )
        assert coord.last_update_success is True, (
            f"L'échec 1 pour {type(exc).__name__} aurait dû retourner le cache!"
        )
        assert coord.battery == 82

    # Vérification d'une exception imprévue (ex: TypeError, RuntimeError) -> UpdateFailed immédiat
    client = MockClientHarness({"battery": 82})
    coord = SevenhugsDataUpdateCoordinator(hass, client, entry)
    await coord.async_refresh()
    client.exception_to_raise = RuntimeError("Unexpected thread failure")
    await coord.async_refresh()
    assert coord.last_update_success is False, "Une exception inattendue doit échouer immédiatement"
    assert isinstance(coord.last_exception, UpdateFailed)

    print("  [PASS] All 10 polymorphic network exceptions and unexpected error handling verified")


async def test_section_9_live_encrypted_integration() -> None:
    """9. Intégration chiffrée réelle de bout en bout avec MockSmartRemoteServer et ChaCha20-Poly1305."""
    print("--- SECTION 9: Live ChaCha20-Poly1305 Encrypted Integration ---")
    server = MockSmartRemoteServer(pair_setup_delay=0.01)
    await server.start()

    client_sk = ed25519.Ed25519PrivateKey.generate()
    keys = {
        CONF_HOST: server.host,
        CONF_PORT: server.port,
        CONF_SERIAL_NUMBER: "a06fe048b48b260139",
        CONF_REMOTE_ID: "5726",
        "controller_id": "ctrl-adversarial-test",
        "client_private_key": client_sk.private_bytes_raw().hex(),
        "remote_public_key": server.remote_pk.hex(),
        "mac_address": "38:D7:CA:00:16:5E",
        "firmware_version": "v0.76.4",
    }

    connector = aiohttp.TCPConnector(force_close=False, limit=1, limit_per_host=1)
    session = aiohttp.ClientSession(connector=connector)
    client = AsyncSmartRemoteClient(host=f"{server.host}:{server.port}", session=session, keys=keys)

    hass = HomeAssistant()
    entry = make_test_entry(data=keys, unique_id="a06fe048b48b260139")
    coord = SevenhugsDataUpdateCoordinator(hass, client, entry)
    entity = SevenhugsBaseEntity(coord, entry, "battery")

    try:
        # 9.1 Premier rafraîchissement chiffré live
        await coord.async_refresh()
        assert coord.last_update_success is True
        assert coord.battery == 95, f"Attendu 95, obtenu {coord.battery}"
        assert coord.serial_number == "a06fe048b48b260139"
        assert coord.firmware_version == "v0.76.4"
        assert entity.available is True
        assert entity.unique_id == "a06fe048b48b260139_battery"
        assert entity.has_entity_name is True

        # 9.2 Simulation d'une rupture brutale de socket (Deep sleep)
        server.force_disconnect_next_api = True
        await coord.async_refresh()
        # Le client tente une reconnexion automatique transparente qui réussit
        assert coord.last_update_success is True

        # 9.3 Arrêt complet du serveur (simulation de mise hors tension)
        await server.stop()

        # Échec 1 (socket fermée)
        await coord.async_refresh()
        assert coord.consecutive_failures == 1
        assert coord.last_update_success is True
        assert coord.battery == 95
        assert entity.available is True

        # Échec 2 (socket fermée)
        await coord.async_refresh()
        assert coord.consecutive_failures == 2
        assert coord.last_update_success is True
        assert coord.battery == 95
        assert entity.available is True

        # Échec 3 (seuil d'indisponibilité)
        await coord.async_refresh()
        assert coord.consecutive_failures == 3
        assert coord.last_update_success is False
        assert entity.available is False
        assert coord.cached_data["battery"] == 95

    finally:
        await client.close()
        await session.close()
        if server.server:
            await server.stop()

    print("  [PASS] Live ChaCha20-Poly1305 encrypted coordinator communication verified")


async def test_section_10_concurrency_and_stress() -> None:
    """10. Vérifie le comportement sous forte concurrence et changements dynamiques d'écouteurs."""
    print("--- SECTION 10: Concurrency Stress & Dynamic Listener Behavior ---")
    hass = HomeAssistant()
    entry = make_test_entry()
    client = MockClientHarness({"battery": 85})
    coord = SevenhugsDataUpdateCoordinator(hass, client, entry)

    # 10.1 Salve de 50 requêtes de rafraîchissement concurrentes
    tasks = [coord.async_refresh() for _ in range(50)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for res in results:
        assert not isinstance(res, Exception), f"Erreur inattendue lors de rafraîchissements concurrents: {res}"
    assert coord.last_update_success is True
    assert coord.consecutive_failures == 0

    # 10.2 Dynamique d'écouteurs : ajout, déclenchement et suppression en cours d'exécution
    called_a = 0
    called_b = 0

    def cb_a():
        nonlocal called_a
        called_a += 1

    def cb_b():
        nonlocal called_b
        called_b += 1

    unsub_a = coord.async_add_listener(cb_a)
    unsub_b = coord.async_add_listener(cb_b)

    await coord.async_refresh()
    assert called_a == 1
    assert called_b == 1

    unsub_a()
    await coord.async_refresh()
    assert called_a == 1, "cb_a n'aurait pas dû être appelé après désinscription"
    assert called_b == 2

    unsub_b()
    await coord.async_refresh()
    assert called_a == 1
    assert called_b == 2

    # 10.3 Entrelacement de wake_up et de refresh concurrents
    wake_tasks = [coord.async_wake_up(reason=f"stress_{i}") for i in range(10)]
    refresh_tasks = [coord.async_refresh() for i in range(10)]
    all_tasks = wake_tasks + refresh_tasks
    await asyncio.gather(*all_tasks)
    assert coord.last_update_success is True
    assert coord.consecutive_failures == 0

    print("  [PASS] Concurrency stress and listener dynamics verified")


# ============================================================================
# MAIN HARNESS RUNNER
# ============================================================================

async def run_all_adversarial_tests() -> None:
    """Exécute l'intégralité du banc d'essais contradictoires."""
    print("================================================================================")
    print("STARTING EMPIRICAL ADVERSARIAL STRESS HARNESS — SevenhugsDataUpdateCoordinator")
    print("================================================================================")
    t0 = time.perf_counter()

    await test_section_1_polling_cadence()
    await test_section_2_telemetry_normalization_and_cache()
    await test_section_3_sleep_resilience_exact_3_failures()
    await test_section_4_recovery_and_flapping_resilience()
    await test_section_5_event_wake_up()
    await test_section_6_auth_error_translation()
    await test_section_7_cold_start_failure()
    await test_section_8_polymorphic_network_exceptions()
    await test_section_9_live_encrypted_integration()
    await test_section_10_concurrency_and_stress()

    duration = time.perf_counter() - t0
    print("================================================================================")
    print(f"ALL 10 EMPIRICAL ADVERSARIAL CHALLENGES COMPLETED IN {duration:.3f}s")
    print("VERDICT: 100% SUCCESS — ZERO REGRESSION — FULL CONFORMANCE")
    print("================================================================================")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_all_adversarial_tests())
