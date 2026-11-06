"""Tests d'intégration réseau asynchrone pour custom_components/sevenhugs_smart_remote/client.py.

Valide :
- Latence matérielle de 16 secondes sur /pair-setup (M1) de façon strictement non-bloquante pour la boucle asyncio.
- Maintien strict de l'unicité du descripteur de socket TCP Keep-Alive sur Pair-Verify (M1 à M4).
- Rejet strict HTTP 400 (SevenhugsAuthError) si la socket TCP a été altérée ou fermée entre M1 et M3.
- Protection anti-thundering-herd : 10 appels concurrents n'exécutent qu'un seul échange physique M1/M3.
- Reconnexion et ré-authentification transparentes lors des ruptures de socket (mise en veille matérielle).
- Échec propagé en cas de défaillance persistante.
- Déroulement complet de la machine à états de l'assistant Lua Webhooks avec le cookie 7HUGS_SID.
"""

from __future__ import annotations

import asyncio
import os
import sys

# Injection de l'environnement de test
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.conftest  # Configure le path et les shims si besoin

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from custom_components.sevenhugs_smart_remote.client import (
    AsyncSmartRemoteClient,
    SevenhugsAuthError,
    SevenhugsConnectionError,
)
from tests.conftest import MockSmartRemoteServer, aiohttp


@pytest.mark.asyncio
async def test_pair_setup_latency_non_blocking() -> None:
    """Valide que la temporisation de calcul matériel sur /pair-setup est strictement non-bloquante pour l'Event Loop."""
    # Simulation d'un délai matériel contrôlé (0.15 seconde pour un test rapide et probant)
    server = MockSmartRemoteServer(pair_setup_delay=0.15)
    await server.start()

    session = aiohttp.ClientSession()
    client = AsyncSmartRemoteClient(
        host=f"{server.host}:{server.port}",
        session=session,
        keys={"controller_id": "test-uuid"},
    )

    ticks = 0
    stop_ticker = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop_ticker.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    ticker_task = asyncio.create_task(ticker())

    try:
        salt, b_bytes = await client.async_pair_setup_m1()
        stop_ticker.set()
        await ticker_task

        assert salt is not None
        assert len(salt) == 16
        assert b_bytes is not None
        assert len(b_bytes) == 384
        # Le ticker a tourné pendant l'attente de M1 -> preuve formelle de non-blocage de l'Event Loop
        assert ticks >= 5, f"La boucle asyncio a été ralentie ou bloquée (ticks = {ticks})"
    finally:
        stop_ticker.set()
        if not ticker_task.done():
            ticker_task.cancel()
        await client.close()
        await session.close()
        await server.stop()


@pytest.mark.asyncio
async def test_pair_verify_same_socket_descriptor() -> None:
    """Valide que Pair-Verify (M1 à M4) s'exécute sur le même descripteur de socket TCP."""
    server = MockSmartRemoteServer(pair_setup_delay=0.01)
    await server.start()

    client_sk = ed25519.Ed25519PrivateKey.generate()
    keys = {
        "controller_id": "ctrl-1234",
        "client_private_key": client_sk.private_bytes_raw().hex(),
        "remote_public_key": server.remote_pk.hex(),
    }

    connector = aiohttp.TCPConnector(
        force_close=False,
        limit=1,
        limit_per_host=1,
        keepalive_timeout=60.0,
    )
    session = aiohttp.ClientSession(connector=connector)
    client = AsyncSmartRemoteClient(
        host=f"{server.host}:{server.port}",
        session=session,
        keys=keys,
    )

    try:
        assert client.is_verified is False
        success = await client.async_pair_verify()
        assert success is True
        assert client.is_verified is True
        assert "session_write_key" in keys
        assert "session_read_key" in keys
        assert server.m1_verify_count == 1
        assert server.m3_verify_count == 1
    finally:
        await client.close()
        await session.close()
        await server.stop()


@pytest.mark.asyncio
async def test_pair_verify_fails_if_socket_changed() -> None:
    """Valide le rejet strict HTTP 400 ({'0x06': 4, '0x07': 2}) si la socket TCP est fermée ou modifiée entre M1 et M3."""
    server = MockSmartRemoteServer(pair_setup_delay=0.01)
    await server.start()

    client_sk = ed25519.Ed25519PrivateKey.generate()
    keys = {
        "controller_id": "ctrl-fail-socket",
        "client_private_key": client_sk.private_bytes_raw().hex(),
        "remote_public_key": server.remote_pk.hex(),
    }

    # Connecteur forçant la fermeture de socket après chaque requête (force_close=True)
    connector = aiohttp.TCPConnector(force_close=True)
    session = aiohttp.ClientSession(connector=connector)
    client = AsyncSmartRemoteClient(
        host=f"{server.host}:{server.port}",
        session=session,
        keys=keys,
    )

    try:
        with pytest.raises(SevenhugsAuthError):
            await client.async_pair_verify()
        assert client.is_verified is False
    finally:
        await client.close()
        await session.close()
        await server.stop()


@pytest.mark.asyncio
async def test_pair_verify_concurrency_anti_thundering_herd() -> None:
    """Valide qu'une ruée de 10 coroutines concurrentes n'exécute qu'une seule négociation physique."""
    server = MockSmartRemoteServer(pair_setup_delay=0.01)
    await server.start()

    client_sk = ed25519.Ed25519PrivateKey.generate()
    keys = {
        "controller_id": "ctrl-thundering",
        "client_private_key": client_sk.private_bytes_raw().hex(),
        "remote_public_key": server.remote_pk.hex(),
    }

    connector = aiohttp.TCPConnector(
        force_close=False, limit=1, limit_per_host=1, keepalive_timeout=60.0
    )
    session = aiohttp.ClientSession(connector=connector)
    client = AsyncSmartRemoteClient(
        host=f"{server.host}:{server.port}",
        session=session,
        keys=keys,
    )

    try:
        # Lancement simultané de 10 vérifications
        tasks = [asyncio.create_task(client.async_pair_verify()) for _ in range(10)]
        results = await asyncio.gather(*tasks)

        assert all(res is True for res in results)
        assert client.is_verified is True
        # Une seule requête M1 et une seule requête M3 ont transité physiquement sur la socket
        assert server.m1_verify_count == 1
        assert server.m3_verify_count == 1
    finally:
        await client.close()
        await session.close()
        await server.stop()


@pytest.mark.asyncio
async def test_async_request_transparent_reconnect_on_closed_socket() -> None:
    """Valide la reconnexion automatique transparente lors d'une coupure de socket (Deep Sleep)."""
    server = MockSmartRemoteServer(pair_setup_delay=0.01)
    await server.start()

    client_sk = ed25519.Ed25519PrivateKey.generate()
    keys = {
        "controller_id": "ctrl-reconnect",
        "client_private_key": client_sk.private_bytes_raw().hex(),
        "remote_public_key": server.remote_pk.hex(),
    }

    connector = aiohttp.TCPConnector(
        force_close=False, limit=1, limit_per_host=1, keepalive_timeout=60.0
    )
    session = aiohttp.ClientSession(connector=connector)
    client = AsyncSmartRemoteClient(
        host=f"{server.host}:{server.port}",
        session=session,
        keys=keys,
    )

    try:
        # 1. Première requête nominale (provoque le premier Pair-Verify)
        resp1 = await client.async_request("GET", "/api/settings")
        assert resp1.get("battery") == 95
        assert server.m1_verify_count == 1

        # 2. Simulation d'une rupture brutale de socket TCP par le matériel
        server.force_disconnect_next_api = True

        # 3. La seconde requête doit ré-authentifier en arrière-plan et retourner la réponse sans lever d'erreur
        resp2 = await client.async_request("GET", "/api/settings")
        assert resp2.get("battery") == 95
        # Le serveur a comptabilisé exactement 2 handshakes Pair-Verify
        assert server.m1_verify_count == 2
        assert server.m3_verify_count == 2
    finally:
        await client.close()
        await session.close()
        await server.stop()


@pytest.mark.asyncio
async def test_async_request_concurrent_recovery_under_socket_failure() -> None:
    """Valide qu'une salve de N=10 requêtes concurrentes lors d'une déconnexion réussit à 100% sans exception."""
    server = MockSmartRemoteServer(pair_setup_delay=0.01)
    await server.start()

    client_sk = ed25519.Ed25519PrivateKey.generate()
    keys = {
        "controller_id": "ctrl-concurrent-recovery",
        "client_private_key": client_sk.private_bytes_raw().hex(),
        "remote_public_key": server.remote_pk.hex(),
    }

    connector = aiohttp.TCPConnector(
        force_close=False, limit=1, limit_per_host=1, keepalive_timeout=60.0
    )
    session = aiohttp.ClientSession(connector=connector)
    client = AsyncSmartRemoteClient(
        host=f"{server.host}:{server.port}",
        session=session,
        keys=keys,
    )

    try:
        # 1. Établissement initial de la session chiffrée
        await client.async_pair_verify()
        assert client.is_verified is True
        assert server.m1_verify_count == 1
        assert server.m3_verify_count == 1

        # 2. Armement de la déconnexion brutale de la socket TCP au prochain appel API
        server.force_disconnect_next_api = True

        # 3. Salve de N=10 requêtes concurrentes en vol
        coro_count = 10
        tasks = [client.async_request("GET", "/api/settings") for _ in range(coro_count)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 4. Contrôle strict de non-régression : 0 exception levée, 100% de succès
        exceptions = [r for r in results if isinstance(r, Exception)]
        assert not exceptions, f"Exceptions levées lors de la salve concurrente: {exceptions}"
        assert len(results) == coro_count
        assert all(isinstance(r, dict) and r.get("battery") == 95 for r in results)

        # 5. Contrôle d'anti-thundering herd : une unique ré-authentification mutualisée
        assert server.m1_verify_count == 2, (
            f"Handshakes M1 anormaux: {server.m1_verify_count} (attendu: 2 - 1 initial + 1 mutualisé)"
        )
        assert server.m3_verify_count == 2
        assert client.is_verified is True
    finally:
        await client.close()
        await session.close()
        await server.stop()


@pytest.mark.asyncio
async def test_async_request_persistent_failure_raises() -> None:
    """Valide qu'en cas d'échec persistant sur les 2 tentatives, l'erreur est propagée."""
    server = MockSmartRemoteServer(pair_setup_delay=0.01)
    await server.start()

    client_sk = ed25519.Ed25519PrivateKey.generate()
    keys = {
        "controller_id": "ctrl-persistent-fail",
        "client_private_key": client_sk.private_bytes_raw().hex(),
        "remote_public_key": server.remote_pk.hex(),
    }

    connector = aiohttp.TCPConnector(
        force_close=False, limit=1, limit_per_host=1, keepalive_timeout=60.0
    )
    session = aiohttp.ClientSession(connector=connector)
    client = AsyncSmartRemoteClient(
        host=f"{server.host}:{server.port}",
        session=session,
        keys=keys,
    )

    try:
        # Établissement initial
        await client.async_pair_verify()

        # Le serveur rejette systématiquement les requêtes d'API
        server.force_auth_error_next_api = True

        # Deuxième rupture forcée
        async def trigger_failure():
            # Forcer un statut d'erreur
            return await client.async_request("GET", "/api/invalid_path")

        # Vérifie que l'erreur est levée au terme des 2 tentatives
        # (ici 404 lève SevenhugsConnectionError)
        with pytest.raises((SevenhugsConnectionError, SevenhugsAuthError)):
            await client.async_request("GET", "/api/unsupported_route_causing_failure")
    finally:
        await client.close()
        await session.close()
        await server.stop()


@pytest.mark.asyncio
async def test_async_push_webhooks_flow() -> None:
    """Valide le pilotage complet de l'assistant Lua Server-Driven UI avec le cookie 7HUGS_SID."""
    server = MockSmartRemoteServer(pair_setup_delay=0.01)
    await server.start()

    client_sk = ed25519.Ed25519PrivateKey.generate()
    keys = {
        "controller_id": "ctrl-webhooks",
        "client_private_key": client_sk.private_bytes_raw().hex(),
        "remote_public_key": server.remote_pk.hex(),
    }

    connector = aiohttp.TCPConnector(
        force_close=False, limit=1, limit_per_host=1, keepalive_timeout=60.0
    )
    session = aiohttp.ClientSession(connector=connector)
    client = AsyncSmartRemoteClient(
        host=f"{server.host}:{server.port}",
        session=session,
        keys=keys,
    )

    triggers = [
        "Lumiere Salon|POST|http://192.168.1.50:8123/api/webhook/salon_toggle",
        {"name": "Volets Fermer", "method": "post", "url": "http://192.168.1.50:8123/api/webhook/shutter_down", "body": "{}"},
    ]

    try:
        success = await client.async_push_webhooks(triggers)
        assert success is True
        # Vérification des étapes Lua séquentielles reçues par le mock
        expected_steps = [
            "connect",
            "main_page",
            "add_choice",
            "advanced_add_edit",
            "post_advanced_add_edit",
            "add_choice",
            "advanced_add_edit",
            "post_advanced_add_edit",
            "done",
        ]
        assert server.lua_steps_called == expected_steps
    finally:
        await client.close()
        await session.close()
        await server.stop()


@pytest.mark.asyncio
async def test_async_request_timeout_recovery() -> None:
    """Valide la reconnexion automatique sur timeout API et la propagation en SevenhugsConnectionError si persistant."""
    server = MockSmartRemoteServer(pair_setup_delay=0.01)
    await server.start()

    client_sk = ed25519.Ed25519PrivateKey.generate()
    keys = {
        "controller_id": "ctrl-timeout-recovery",
        "client_private_key": client_sk.private_bytes_raw().hex(),
        "remote_public_key": server.remote_pk.hex(),
    }

    connector = aiohttp.TCPConnector(
        force_close=False, limit=1, limit_per_host=1, keepalive_timeout=60.0
    )
    session = aiohttp.ClientSession(connector=connector)
    client = AsyncSmartRemoteClient(
        host=f"{server.host}:{server.port}",
        session=session,
        keys=keys,
    )

    try:
        # Établissement initial de la session
        await client.async_pair_verify()
        assert client.is_verified is True
        assert server.m1_verify_count == 1
        initial_gen = client.session_generation

        # Interception de session.request pour injecter un TimeoutError sur /api/
        orig_request = session.request
        timeout_injections_remaining = 1

        def timeout_intercept_request(method: str, url: str, **kwargs: Any) -> Any:
            nonlocal timeout_injections_remaining
            if "/api/" in str(url) and timeout_injections_remaining > 0:
                timeout_injections_remaining -= 1
                raise asyncio.TimeoutError("Timeout réseau simulé sur endpoint API")
            return orig_request(method, url, **kwargs)

        session.request = timeout_intercept_request

        # 1. Tentative 0 subit un timeout -> doit reconnecter et réussir sur tentative 1
        resp = await client.async_request("GET", "/api/settings")
        assert resp.get("battery") == 95
        assert client.is_verified is True
        # Une nouvelle négociation Pair-Verify doit avoir eu lieu
        assert server.m1_verify_count == 2
        assert client.session_generation > initial_gen

        # 2. Timeout persistant sur les deux tentatives -> doit lever SevenhugsConnectionError
        timeout_injections_remaining = 10  # Échoue systématiquement
        with pytest.raises(SevenhugsConnectionError) as exc_info:
            await client.async_request("GET", "/api/settings")

        assert isinstance(exc_info.value, SevenhugsConnectionError)
        assert "Erreur réseau lors de GET /api/settings" in str(exc_info.value)
    finally:
        await client.close()
        await session.close()
        await server.stop()


def run_all_async_tests() -> int:
    """Exécute l'ensemble des tests asynchrones de test_client.py et rapporte le bilan."""
    tests = [
        test_pair_setup_latency_non_blocking,
        test_pair_verify_same_socket_descriptor,
        test_pair_verify_fails_if_socket_changed,
        test_pair_verify_concurrency_anti_thundering_herd,
        test_async_request_transparent_reconnect_on_closed_socket,
        test_async_request_concurrent_recovery_under_socket_failure,
        test_async_request_persistent_failure_raises,
        test_async_request_timeout_recovery,
        test_async_push_webhooks_flow,
    ]
    passed = 0
    failed = 0
    print(f"\n=== Lancement de {len(tests)} tests pour test_client.py ===")
    for test_fn in tests:
        try:
            asyncio.run(test_fn())
            print(f"  [PASS] {test_fn.__name__}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"  [FAIL] {test_fn.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"=== Bilan test_client.py : {passed} réussis, {failed} échoués ===\n")
    return failed


def load_tests(loader: Any, standard_tests: Any, pattern: Any) -> Any:
    """Protocole de découverte unittest pour exécuter les tests asynchrones sans dépendance externe."""
    import unittest
    suite = unittest.TestSuite()
    tests = [
        test_pair_setup_latency_non_blocking,
        test_pair_verify_same_socket_descriptor,
        test_pair_verify_fails_if_socket_changed,
        test_pair_verify_concurrency_anti_thundering_herd,
        test_async_request_transparent_reconnect_on_closed_socket,
        test_async_request_concurrent_recovery_under_socket_failure,
        test_async_request_persistent_failure_raises,
        test_async_request_timeout_recovery,
        test_async_push_webhooks_flow,
    ]
    for fn in tests:
        tc = unittest.FunctionTestCase(lambda f=fn: asyncio.run(f()), description=fn.__name__)
        suite.addTest(tc)
    return suite


if __name__ == "__main__":
    sys.exit(run_all_async_tests())
