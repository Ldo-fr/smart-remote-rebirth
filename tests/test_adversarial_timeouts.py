"""Tests contradictoires d'injection de timeouts et d'erreurs d'encapsulation.

Vérifie de manière empirique et exhaustive :
1. Capture stricte de TimeoutError, asyncio.TimeoutError, aiohttp.ServerTimeoutError dans _raw_encrypted_request.
2. Reconnexion transparente et incrémentation de génération sur timeout unique.
3. Encapsulation stricte en SevenhugsConnectionError en cas de timeout persistant (zéro exception brute).
4. Résistance aux timeouts en cascade (timeout pendant le re-handshake).
5. Résistance sous salve concurrente avec injection chaotique de timeouts et coupures.
6. Encapsulation des timeouts sur Pair-Setup (M1, M3, M5) et Push-Webhooks.
"""

import asyncio
import os
import sys
import unittest
from typing import Any

# Configuration du PYTHONPATH
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.conftest
from tests.conftest import MockSmartRemoteServer, aiohttp
from cryptography.hazmat.primitives.asymmetric import ed25519

from custom_components.sevenhugs_smart_remote.client import (
    AsyncSmartRemoteClient,
    SevenhugsAuthError,
    SevenhugsConnectionError,
)


class AdversarialTimeoutServer(MockSmartRemoteServer):
    """Serveur mock avec capacités d'injection fine de timeouts et coupures."""

    def __init__(self) -> None:
        super().__init__(pair_setup_delay=0.001)
        self.stall_api_duration: float = 0.0
        self.stall_m1_duration: float = 0.0
        self.stall_m3_duration: float = 0.0

    async def _handle_api(self, path: str, body: bytes, conn_id: Any, writer: asyncio.StreamWriter):
        if self.stall_api_duration > 0:
            delay = self.stall_api_duration
            self.stall_api_duration = 0.0
            await asyncio.sleep(delay)
            return 200, {}, b""
        return await super()._handle_api(path, body, conn_id, writer)


class TestAdversarialTimeouts(unittest.IsolatedAsyncioTestCase):
    """Banc d'épreuves contradictoires pour la gestion des timeouts et exceptions."""

    async def asyncSetUp(self) -> None:
        self.server = AdversarialTimeoutServer()
        await self.server.start()

        self.client_sk = ed25519.Ed25519PrivateKey.generate()
        self.keys = {
            "controller_id": "ctrl-adv-timeout",
            "client_private_key": self.client_sk.private_bytes_raw().hex(),
            "remote_public_key": self.server.remote_pk.hex(),
        }
        self.connector = aiohttp.TCPConnector(
            force_close=False, limit=1, limit_per_host=1, keepalive_timeout=60.0
        )
        self.session = aiohttp.ClientSession(connector=self.connector)
        self.client = AsyncSmartRemoteClient(
            host=f"{self.server.host}:{self.server.port}",
            session=self.session,
            keys=self.keys,
        )

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.session.close()
        await self.server.stop()

    async def test_01_raw_request_catches_all_timeout_variants(self) -> None:
        """Vérifie que _raw_encrypted_request encapsule toutes les variantes d'exceptions de timeout."""
        await self.client.async_pair_verify()
        self.assertTrue(self.client.is_verified)

        timeout_exceptions_to_test = [
            asyncio.TimeoutError("Timeout asyncio simulé"),
            TimeoutError("Timeout built-in simulé"),
            aiohttp.ClientError("ClientError générique aiohttp"),
            aiohttp.ClientConnectionError("Connection closed/reset aiohttp"),
            aiohttp.ClientPayloadError("Payload read error aiohttp"),
        ]

        orig_request = self.session.request

        for injected_exc in timeout_exceptions_to_test:
            # Ré-établir la session si elle a été invalidée
            self.session.request = orig_request
            if not self.client.is_verified:
                await self.client.async_pair_verify()

            def make_mock(exc_to_raise: Exception):
                def mock_req(*args: Any, **kwargs: Any) -> Any:
                    raise exc_to_raise
                return mock_req

            self.session.request = make_mock(injected_exc)

            with self.assertRaises(SevenhugsConnectionError) as cm:
                await self.client._raw_encrypted_request("GET", "/api/settings")

            exc = cm.exception
            # Vérifications strictes
            self.assertIsInstance(exc, SevenhugsConnectionError)
            self.assertFalse(self.client.is_verified, "L'état de session doit être réinitialisé à False")
            self.assertIs(exc.__cause__, injected_exc, "La cause d'origine doit être préservée via 'from err'")
            self.assertIn("Erreur réseau lors de GET /api/settings", str(exc))

        self.session.request = orig_request

    async def test_02_async_request_transparent_recovery_on_timeout(self) -> None:
        """Vérifie la reprise transparente lors d'un timeout sur la première tentative."""
        await self.client.async_pair_verify()
        initial_gen = self.client.session_generation
        self.assertEqual(self.server.m1_verify_count, 1)

        orig_request = self.session.request
        injected = False

        def mock_req(method: str, url: str, **kwargs: Any) -> Any:
            nonlocal injected
            if "/api/" in str(url) and not injected:
                injected = True
                raise asyncio.TimeoutError("Timeout simulé sur tentative 0")
            return orig_request(method, url, **kwargs)

        self.session.request = mock_req

        # Appel async_request : la tentative 0 subit le timeout, ré-exécute le handshake, tentative 1 réussit
        resp = await self.client.async_request("GET", "/api/settings")
        self.assertEqual(resp.get("battery"), 95)
        self.assertTrue(self.client.is_verified)
        self.assertEqual(self.server.m1_verify_count, 2, "Un nouveau handshake Pair-Verify doit avoir été exécuté")
        self.assertEqual(self.client.session_generation, initial_gen + 1)

        self.session.request = orig_request

    async def test_03_async_request_persistent_timeout_encapsulation(self) -> None:
        """Vérifie qu'un timeout persistant lève SevenhugsConnectionError et jamais un TimeoutError brut."""
        await self.client.async_pair_verify()

        orig_request = self.session.request

        def mock_req(method: str, url: str, **kwargs: Any) -> Any:
            if "/api/" in str(url):
                raise TimeoutError("Timeout persistant matériel")
            return orig_request(method, url, **kwargs)

        self.session.request = mock_req

        # L'appelant NE DOIT JAMAIS recevoir TimeoutError
        with self.assertRaises(SevenhugsConnectionError) as cm:
            await self.client.async_request("GET", "/api/settings")

        exc = cm.exception
        self.assertIsInstance(exc, SevenhugsConnectionError)
        self.assertNotEqual(type(exc), TimeoutError)
        self.assertNotEqual(type(exc), asyncio.TimeoutError)
        self.assertIn("Erreur réseau lors de GET /api/settings", str(exc))
        self.assertFalse(self.client.is_verified)

        self.session.request = orig_request

    async def test_04_cascading_timeout_during_rehandshake(self) -> None:
        """Vérifie le comportement en cas de timeout lors du handshake de reconnexion."""
        await self.client.async_pair_verify()

        orig_request = self.session.request
        api_failed = False

        def mock_req(method: str, url: str, **kwargs: Any) -> Any:
            nonlocal api_failed
            if "/api/" in str(url) and not api_failed:
                api_failed = True
                raise asyncio.TimeoutError("Timeout API initial")
            if "/pair-verify" in str(url) and api_failed:
                # Le handshake de reconnexion time out également
                raise asyncio.TimeoutError("Timeout réseau lors de Pair-Verify M1")
            return orig_request(method, url, **kwargs)

        self.session.request = mock_req

        with self.assertRaises(SevenhugsConnectionError) as cm:
            await self.client.async_request("GET", "/api/settings")

        self.assertIsInstance(cm.exception, SevenhugsConnectionError)
        self.assertIn("Pair-Verify M1", str(cm.exception))
        self.assertFalse(self.client.is_verified)

        # Rétablissement : réinitialisation du mock
        self.session.request = orig_request
        # Vérification qu'aucun verrou n'est resté bloqué (deadlock check)
        resp = await self.client.async_request("GET", "/api/settings")
        self.assertEqual(resp.get("model"), "SR1A")
        self.assertTrue(self.client.is_verified)

    async def test_05_chaotic_concurrent_timeouts_and_reconnects(self) -> None:
        """Stress-test : 30 requêtes simultanées sous injection chaotique de timeouts et coupures."""
        await self.client.async_pair_verify()
        initial_gen = self.client.session_generation

        orig_request = self.session.request
        req_counter = 0

        def chaotic_request(method: str, url: str, **kwargs: Any) -> Any:
            nonlocal req_counter
            req_counter += 1
            # Injection de pannes uniquement sur la génération initiale (tentative 0)
            if "/api/" in str(url) and self.client.session_generation == initial_gen:
                if req_counter % 3 == 1:
                    raise asyncio.TimeoutError("Chaotic asyncio Timeout")
                elif req_counter % 3 == 2:
                    raise TimeoutError("Chaotic built-in Timeout")
                else:
                    raise aiohttp.ClientConnectionError("Chaotic Connection Drop")
            return orig_request(method, url, **kwargs)

        self.session.request = chaotic_request

        async def worker(idx: int) -> dict:
            return await self.client.async_request("GET", "/api/settings")

        # Salve concurrente de 30 coroutines
        results = await asyncio.gather(*(worker(i) for i in range(30)), return_exceptions=True)

        self.session.request = orig_request

        successes = [r for r in results if isinstance(r, dict) and r.get("battery") == 95]
        raw_timeouts = [r for r in results if isinstance(r, (TimeoutError, asyncio.TimeoutError))]
        conn_errors = [r for r in results if isinstance(r, SevenhugsConnectionError)]

        self.assertEqual(len(raw_timeouts), 0, "AUCUN TimeoutError brut ne doit fuiter vers l'appelant")
        self.assertEqual(len(successes), 30, f"Les 30 requêtes doivent aboutir après reconnexion (succès: {len(successes)}, err: {len(conn_errors)})")
        self.assertEqual(self.client.session_generation, initial_gen + 1, "Un seul re-handshake doit être effectué pour toute la salve")
        print(f"\n  [CHAOS TEST] 30 requêtes chaotiques -> {len(successes)}/30 réussies, 0 fuites de TimeoutError, génération={self.client.session_generation}")

    async def test_06_pair_setup_and_webhooks_timeouts(self) -> None:
        """Vérifie l'encapsulation de TimeoutError sur Pair-Setup (M1, M3, M5) et Push-Webhooks."""
        orig_request = self.session.request

        # 1. Timeout sur Pair-Setup M1
        def timeout_m1(*args: Any, **kwargs: Any) -> Any:
            raise asyncio.TimeoutError("Timeout M1")

        self.session.request = timeout_m1
        with self.assertRaises(SevenhugsConnectionError) as cm:
            await self.client.async_pair_setup_m1()
        self.assertIn("Pair-Setup M1", str(cm.exception))

        # 2. Timeout sur Pair-Setup Finish M3
        def timeout_m3(*args: Any, **kwargs: Any) -> Any:
            raise asyncio.TimeoutError("Timeout M3")

        self.session.request = timeout_m3
        with self.assertRaises(SevenhugsConnectionError) as cm:
            await self.client.async_pair_setup_finish("1234", b"\x11" * 16, b"\x22" * 384)
        self.assertIn("Pair-Setup M3", str(cm.exception))

        # 3. Timeout sur Push-Webhooks
        def timeout_webhooks(*args: Any, **kwargs: Any) -> Any:
            raise asyncio.TimeoutError("Timeout Webhooks")

        self.session.request = timeout_webhooks
        with self.assertRaises(SevenhugsConnectionError) as cm:
            await self.client.async_push_webhooks(["Trigger|POST|http://ha:8123/webhook"])
        self.assertIsInstance(cm.exception, SevenhugsConnectionError)

        self.session.request = orig_request


if __name__ == "__main__":
    unittest.main()
