"""Banc d'essais contradictoires et de stress empirique pour Milestone 3 (Cycle de vie & Localisation).

Auteur: Challenger M3.1 (EMPIRICAL CHALLENGER)
Rôles: critic, specialist
Milestone: Phase 3 — Milestone 3

Objectifs contradictoires :
1. Cycle d'endurance rapide : 50+ cycles de setup/unload consécutifs avec surveillance stricte de /proc/self/fd (0 fuite de socket).
2. Scénarios d'échec au démarrage (Setup failure) :
   - Échec HTTP 500 sur /api/settings lors du first_refresh initial.
   - Timeout réseau sur /api/settings lors du first_refresh initial.
   - Vérification du rollback complet (nettoyage de hass.data, fermeture des sessions/sockets TCP, 0 orphelin).
3. Idempotence du déchargement (Double Unload) :
   - Double appel consécutif à async_unload_entry sur une même entrée.
   - Double unload en environnement multi-entrées (isolation stricte).
   - Déchargement sur structure hass.data altérée / vidée.
4. Échec d'aiguillage de plateformes (Platform Forwarding Failure) :
   - Injection d'exception lors de hass.config_entries.async_forward_entry_setups.
   - Vérification empirique de l'état post-échec : rollback propre vs ressources orphelines / fuite de descripteurs.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from unittest.mock import patch
from typing import Any

# Configuration du PYTHONPATH
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
    SevenhugsData,
    SevenhugsDataUpdateCoordinator,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.smart_remote_rebirth.const import (
    CONF_CLIENT_PRIVATE_KEY,
    CONF_CLIENT_PUBLIC_KEY,
    CONF_CONTROLLER_ID,
    CONF_HOST,
    CONF_PORT,
    CONF_REMOTE_ID,
    CONF_REMOTE_PUBLIC_KEY,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_READ_KEY,
    CONF_SESSION_WRITE_KEY,
    DEFAULT_POLLING_INTERVAL,
    SLEEP_CONSECUTIVE_FAILURES_THRESHOLD,
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
# AXE 1 : 50+ CYCLES RAPIDES SETUP / UNLOAD AVEC DÉTECTION DE FUITE FD
# ============================================================================

async def test_axis1_rapid_setup_unload_50_cycles() -> dict[str, Any]:
    """Exécute 50 cycles rapides de setup et unload complets de Milestone 3."""
    print("\n--- [AXE 1] 50 Cycles Rapides Setup / Unload & Détection de Fuite de Sockets ---")
    server = MockSmartRemoteServer()
    await server.start()

    # Pause de stabilisation des sockets
    await asyncio.sleep(0.05)
    baseline_fds = get_current_socket_fd_count()
    print(f"  [FD] Baseline socket FDs: {baseline_fds}")

    cycles = 50
    hass = HomeAssistant()

    try:
        for i in range(cycles):
            entry = ConfigEntry(
                entry_id=f"entry_m3_cycle_{i}",
                domain=DOMAIN,
                title=f"Remote M3 Cycle {i}",
                data={
                    CONF_HOST: server.host,
                    CONF_PORT: server.port,
                    CONF_SERIAL_NUMBER: f"SN_M3_CYCLE_{i}",
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
            assert setup_res is True, f"Cycle {i}: async_setup_entry a échoué"
            assert entry.entry_id in hass.data[DOMAIN], f"Cycle {i}: entry_id non présent dans hass.data"
            assert entry.runtime_data is not None, f"Cycle {i}: runtime_data est None"

            coord = entry.runtime_data.coordinator
            assert coord is not None, f"Cycle {i}: coordinator est None"
            assert coord.data is not None, f"Cycle {i}: coordinator.data est None après first_refresh"
            assert coord.data["battery"] == 95, f"Cycle {i}: batterie incohérente ({coord.data.get('battery')})"

            # 2. Vérification plateformes forwardées
            assert entry.entry_id in hass.config_entries.forwarded_platforms
            assert hass.config_entries.forwarded_platforms[entry.entry_id] == PLATFORMS

            # 3. Unload
            unload_res = await async_unload_entry(hass, entry)
            assert unload_res is True, f"Cycle {i}: async_unload_entry a échoué"
            assert entry.entry_id not in hass.data.get(DOMAIN, {}), f"Cycle {i}: entrée résiduelle dans hass.data"
            assert entry.runtime_data is None, f"Cycle {i}: runtime_data non réinitialisé"
            assert DOMAIN not in hass.data, f"Cycle {i}: DOMAIN non nettoyé de hass.data alors qu'il est vide"
            assert entry.entry_id in hass.config_entries.unloaded_platforms

        await asyncio.sleep(0.1)
        final_fds = get_current_socket_fd_count()
        fd_delta = final_fds - baseline_fds
        print(f"  [FD] Final socket FDs: {final_fds} (Delta: {fd_delta:+d})")

        assert fd_delta <= 0, f"Fuite de descripteurs de socket détectée ! Delta: {fd_delta}"
        return {"status": "PASS", "cycles": cycles, "fd_delta": fd_delta}

    finally:
        await server.stop()


# ============================================================================
# AXE 2 : SCÉNARIOS D'ÉCHEC AU DÉMARRAGE (500 ET TIMEOUT INITIAL FIRST_REFRESH)
# ============================================================================

async def test_axis2_setup_failure_scenarios() -> dict[str, Any]:
    """Teste les scénarios d'échec sur first_refresh : HTTP 500 et Timeout."""
    print("\n--- [AXE 2] Scénarios d'Échec Initial (HTTP 500 & Timeout) ---")
    results = {}

    # 2.1 Échec HTTP 500 sur /api/settings lors de first_refresh
    server_500 = MockSmartRemoteServer()
    orig_handle_api = server_500._handle_api

    async def fail_500_handle_api(path: str, body: bytes, conn_id: Any, writer: asyncio.StreamWriter):
        if path == "/api/settings":
            return 500, {}, b"Internal Server Error"
        return await orig_handle_api(path, body, conn_id, writer)

    server_500._handle_api = fail_500_handle_api
    await server_500.start()

    try:
        hass = HomeAssistant()
        entry_500 = ConfigEntry(
            entry_id="entry_fail_500",
            domain=DOMAIN,
            title="Remote 500 Fail",
            data={
                CONF_HOST: server_500.host,
                CONF_PORT: server_500.port,
                CONF_SERIAL_NUMBER: "SN_FAIL_500",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-fail-500",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: server_500.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
        )

        baseline_fds = get_current_socket_fd_count()

        # Observation empirique : que fait async_setup_entry face à un serveur 500 ?
        setup_returned_true = False
        exception_caught = None
        try:
            res = await async_setup_entry(hass, entry_500)
            if res is True:
                setup_returned_true = True
        except Exception as e:
            exception_caught = e

        in_hass_data = entry_500.entry_id in hass.data.get(DOMAIN, {})
        has_runtime_data = getattr(entry_500, "runtime_data", None) is not None

        print(f"  [2.1 HTTP 500] setup_returned_true: {setup_returned_true}, exception: {type(exception_caught).__name__ if exception_caught else None}")
        print(f"  [2.1 HTTP 500] in_hass_data: {in_hass_data}, has_runtime_data: {has_runtime_data}")

        # Si async_setup_entry a retourné True ou a laissé des données orphelines, c'est un échec
        if setup_returned_true or in_hass_data or has_runtime_data:
            print("  [BUG DÉTECTÉ 2.1] L'échec HTTP 500 lors de l'initialisation n'est pas rejeté ou n'a pas été nettoyé !")
            results["sub2_1_http_500"] = {
                "status": "FAIL",
                "setup_returned_true": setup_returned_true,
                "in_hass_data": in_hass_data,
                "has_runtime_data": has_runtime_data,
                "bug": "HTTP 500 during initial first_refresh does not trigger rollback or exception",
            }
        else:
            results["sub2_1_http_500"] = {"status": "PASS"}

    finally:
        await server_500.stop()

    # 2.2 Échec Timeout réseau sur /api/settings lors de first_refresh
    server_timeout = MockSmartRemoteServer()
    orig_handle_api_to = server_timeout._handle_api

    async def fail_to_handle_api(path: str, body: bytes, conn_id: Any, writer: asyncio.StreamWriter):
        if path == "/api/settings":
            await asyncio.sleep(5.0)
            return 200, {}, b"{}"
        return await orig_handle_api_to(path, body, conn_id, writer)

    server_timeout._handle_api = fail_to_handle_api
    await server_timeout.start()

    try:
        hass_to = HomeAssistant()
        entry_to = ConfigEntry(
            entry_id="entry_fail_timeout",
            domain=DOMAIN,
            title="Remote Timeout Fail",
            data={
                CONF_HOST: server_timeout.host,
                CONF_PORT: server_timeout.port,
                CONF_SERIAL_NUMBER: "SN_FAIL_TIMEOUT",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-fail-timeout",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: server_timeout.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
        )

        setup_to_returned_true = False
        exception_to_caught = None
        # On patche le timeout aiohttp à 0.5s pour une détection rapide
        with patch("custom_components.smart_remote_rebirth.client.aiohttp.ClientTimeout", return_value=0.5):
            try:
                res = await async_setup_entry(hass_to, entry_to)
                if res is True:
                    setup_to_returned_true = True
            except Exception as e:
                exception_to_caught = e

        in_hass_data_to = entry_to.entry_id in hass_to.data.get(DOMAIN, {})
        has_runtime_data_to = getattr(entry_to, "runtime_data", None) is not None

        print(f"  [2.2 Timeout] setup_returned_true: {setup_to_returned_true}, exception: {type(exception_to_caught).__name__ if exception_to_caught else None}")
        print(f"  [2.2 Timeout] in_hass_data: {in_hass_data_to}, has_runtime_data: {has_runtime_data_to}")

        if setup_to_returned_true or in_hass_data_to or has_runtime_data_to:
            print("  [BUG DÉTECTÉ 2.2] Le timeout lors de l'initialisation n'est pas rejeté ou n'a pas été nettoyé !")
            results["sub2_2_timeout"] = {
                "status": "FAIL",
                "setup_returned_true": setup_to_returned_true,
                "in_hass_data": in_hass_data_to,
                "has_runtime_data": has_runtime_data_to,
                "bug": "Network timeout during initial first_refresh does not trigger rollback or exception",
            }
        else:
            results["sub2_2_timeout"] = {"status": "PASS"}

    finally:
        await server_timeout.stop()

    return results


# ============================================================================
# AXE 3 : IDEMPOTENCE DU DOUBLE DÉCHARGEMENT (DOUBLE UNLOAD)
# ============================================================================

async def test_axis3_double_unload_idempotency() -> dict[str, Any]:
    """Vérifie l'idempotence stricte de async_unload_entry sous divers scénarios."""
    print("\n--- [AXE 3] Idempotence du Double Déchargement (Double Unload) ---")
    server = MockSmartRemoteServer()
    await server.start()

    try:
        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="entry_double_unload_axe3",
            domain=DOMAIN,
            title="Remote Double Unload",
            data={
                CONF_HOST: server.host,
                CONF_PORT: server.port,
                CONF_SERIAL_NUMBER: "SN_DOUBLE_AXE3",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-uuid-double",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
        )

        assert await async_setup_entry(hass, entry) is True

        # Premier unload
        u1 = await async_unload_entry(hass, entry)
        assert u1 is True, "Premier déchargement a retourné False"
        assert entry.entry_id not in hass.data.get(DOMAIN, {})
        assert getattr(entry, "runtime_data", None) is None

        # Deuxième unload consécutif
        u2 = await async_unload_entry(hass, entry)
        assert u2 is True, "Deuxième déchargement a retourné False au lieu de True (idempotence)"

        # Troisième unload après purge complète
        u3 = await async_unload_entry(hass, entry)
        assert u3 is True, "Troisième déchargement a levé une exception ou retourné False"

        # Multi-entrées : isolation de deux télécommandes
        e_a = ConfigEntry(
            entry_id="entry_multi_a",
            domain=DOMAIN,
            title="Remote Multi A",
            data={
                CONF_HOST: server.host,
                CONF_PORT: server.port,
                CONF_SERIAL_NUMBER: "SN_MULTI_A",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-uuid-a",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
        )
        e_b = ConfigEntry(
            entry_id="entry_multi_b",
            domain=DOMAIN,
            title="Remote Multi B",
            data={
                CONF_HOST: server.host,
                CONF_PORT: server.port,
                CONF_SERIAL_NUMBER: "SN_MULTI_B",
                CONF_REMOTE_ID: "5727",
                CONF_CONTROLLER_ID: "ctrl-uuid-b",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
        )

        assert await async_setup_entry(hass, e_a) is True
        assert await async_setup_entry(hass, e_b) is True
        assert len(hass.data[DOMAIN]) == 2

        # Double déchargement de e_a sans impacter e_b
        assert await async_unload_entry(hass, e_a) is True
        assert await async_unload_entry(hass, e_a) is True
        assert "entry_multi_b" in hass.data[DOMAIN]
        assert len(hass.data[DOMAIN]) == 1

        # Déchargement final de e_b
        assert await async_unload_entry(hass, e_b) is True
        assert DOMAIN not in hass.data

        print("  [OK] Idempotence et isolation multi-entrées validées.")
        return {"status": "PASS"}

    finally:
        await server.stop()


# ============================================================================
# AXE 4 : ÉCHEC D'AIGUILLAGE DE PLATEFORMES (PLATFORM FORWARDING FAILURE)
# ============================================================================

async def test_axis4_platform_forwarding_failure_rollback() -> dict[str, Any]:
    """Injecte une exception lors de async_forward_entry_setups et contrôle le rollback."""
    print("\n--- [AXE 4] Échec d'Aiguillage de Plateformes & Rollback ---")
    server = MockSmartRemoteServer()
    await server.start()

    try:
        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="entry_plat_fail_axe4",
            domain=DOMAIN,
            title="Remote Platform Fail",
            data={
                CONF_HOST: server.host,
                CONF_PORT: server.port,
                CONF_SERIAL_NUMBER: "SN_PLAT_FAIL",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-uuid-plat",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
        )

        # Injection d'exception sur async_forward_entry_setups
        with patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            side_effect=RuntimeError("Fault Injection: platform sensor crashed on setup"),
        ):
            exception_raised = False
            try:
                await async_setup_entry(hass, entry)
            except RuntimeError as e:
                exception_raised = True
                print(f"  [OK] Exception propagée lors de l'échec de forwarding: {e}")

        # VÉRIFICATION DU ROLLBACK CRITIQUE :
        in_hass_data = entry.entry_id in hass.data.get(DOMAIN, {})
        runtime_data_present = getattr(entry, "runtime_data", None) is not None

        session_closed = True
        if in_hass_data:
            rd = hass.data[DOMAIN][entry.entry_id]
            sess = getattr(rd, "session", None)
            if sess:
                session_closed = getattr(sess, "closed", False)

        print(f"  [AXE 4] in_hass_data: {in_hass_data}, runtime_data_present: {runtime_data_present}, session_closed: {session_closed}")

        if in_hass_data or runtime_data_present or not session_closed:
            print("  [BUG DÉTECTÉ AXE 4] async_setup_entry n'a PAS effectué de rollback lors de l'échec de async_forward_entry_setups !")
            print("  Ressources résiduelles :")
            if in_hass_data:
                print("    - entry_id toujours présent dans hass.data[DOMAIN]")
            if runtime_data_present:
                print("    - entry.runtime_data non réinitialisé à None")
            if not session_closed:
                print("    - aiohttp ClientSession & socket TCP toujours ouverts (fuite de descripteur)")

            return {
                "status": "FAIL",
                "in_hass_data": in_hass_data,
                "runtime_data_present": runtime_data_present,
                "session_closed": session_closed,
                "bug": "async_forward_entry_setups is outside try/except block; no rollback on failure",
            }

        return {"status": "PASS"}

    finally:
        await server.stop()


# ============================================================================
# EXÉCUTION DU HARNAIS COMPLET
# ============================================================================

async def run_all_harness_tests() -> dict[str, Any]:
    print("=" * 80)
    print("BANC D'ESSAIS CONTRADICTOIRES MILISTONE 3 (CHALLENGER_M3_GEN2_1)")
    print("=" * 80)

    t0 = time.time()
    results = {}

    try:
        results["axis1_endurance_fd"] = await test_axis1_rapid_setup_unload_50_cycles()
        results["axis2_setup_failures"] = await test_axis2_setup_failure_scenarios()
        results["axis3_double_unload"] = await test_axis3_double_unload_idempotency()
        results["axis4_platform_forwarding_failure"] = await test_axis4_platform_forwarding_failure_rollback()
    except Exception as e:
        import traceback
        print(f"\n[ÉCHEC HARNAIS] Exception inattendue : {e}")
        traceback.print_exc()
        results["fatal_error"] = str(e)

    duration = time.time() - t0
    print("\n" + "=" * 80)
    print(f"RÉSUMÉ DU BANC CONTRADICTOIRE M3 (Durée: {duration:.2f}s) :")
    for k, v in results.items():
        print(f"  - {k}: {v}")
    print("=" * 80)
    return results


if __name__ == "__main__":
    res = asyncio.run(run_all_harness_tests())
    # Détermination du statut global
    has_failures = False
    for k, v in res.items():
        if isinstance(v, dict):
            if v.get("status") == "FAIL":
                has_failures = True
            for sub_k, sub_v in v.items():
                if isinstance(sub_v, dict) and sub_v.get("status") == "FAIL":
                    has_failures = True
        elif k == "fatal_error":
            has_failures = True

    if has_failures:
        print("\n===> VERDICT DU BANC: ÉCHECS DÉTECTÉS (REQUEST_CHANGES) <===")
        sys.exit(1)
    else:
        print("\n===> VERDICT DU BANC: SUCCÈS (APPROVE) <===")
        sys.exit(0)
