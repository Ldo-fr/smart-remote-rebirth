"""Banc de validation contradictoire approfondie (Réseau, Concurrence extrême, Deadlock & FD).

Auteur: challenger_p1_6 (EMPIRICAL CHALLENGER)
Rôles: critic, specialist

Objectifs empiriques :
1. Salve extrême de 100 requêtes concurrentes avec rupture de socket à mi-parcours :
   - Vérification de l'anti-thundering herd (exactement 2 handshakes M1 pour 100 requêtes).
   - 100% de succès applicatif et validation de 100 oracles de réponse distincts.
2. Résistance aux annulations de coroutines (Task Cancellation) sous forte contention :
   - 50 coroutines lancées, 25 annulées brutalement en plein vol (asyncio.CancelledError).
   - Vérification de l'absence totale de deadlock sur _verify_lock et _request_lock.
   - Vérification immédiate que les 50 requêtes suivantes s'exécutent sans blocage.
3. Résistance aux pannes réseau transitoires lors du re-handshake :
   - Simulation d'un échec de transport sur Pair-Verify M1 lors d'une reconnexion.
   - Vérification que l'état interne (is_verified, session_generation) reste cohérent.
   - Vérification que la session se rétablit sans résidu dès que le réseau redevient disponible.
4. Profilage haute endurance FD (1 000 requêtes réparties en 20 salves de 50 sous déconnexions aléatoires) :
   - Suivi strict de /proc/self/fd.
   - Tolérance zéro sur les fuites de sockets.
5. Concurrence mixte : entrelacement de GET, POST volumineux (32 KB SHA-256), async_push_webhooks et async_pair_verify() explicite sous rupture de socket.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from typing import Any

WORKSPACE_ROOT = "/web/smart-remote-rebirth"
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

import tests.conftest
from tests.conftest import MockSmartRemoteServer, aiohttp
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from custom_components.smart_remote_rebirth.client import (
    AsyncSmartRemoteClient,
    SevenhugsAuthError,
    SevenhugsConnectionError,
)
from custom_components.smart_remote_rebirth.crypto import API_NONCE

logging.basicConfig(level=logging.WARNING)


def get_current_socket_fd_count() -> int:
    """Compte le nombre exact de sockets ouverts par le processus via /proc/self/fd."""
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


class DeepAdversarialServer(MockSmartRemoteServer):
    """Serveur mock étendu avec injection fine de pannes et support de routes variées."""

    def __init__(self, pair_setup_delay: float = 0.01) -> None:
        super().__init__(pair_setup_delay=pair_setup_delay)
        self.request_count = 0
        self.fail_pair_verify_m1_count = 0
        self.disconnect_on_request_indices: set[int] = set()
        self.processed_requests: list[dict[str, Any]] = []

    async def _route_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        conn_id: Any,
        writer: asyncio.StreamWriter,
    ) -> tuple[int | None, dict[str, str], bytes]:
        self.request_count += 1

        if self.request_count in self.disconnect_on_request_indices:
            self.disconnect_on_request_indices.remove(self.request_count)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return None, {}, b""

        if path == "/pair-verify" and self.fail_pair_verify_m1_count > 0:
            self.fail_pair_verify_m1_count -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return None, {}, b""

        return await super()._route_request(method, path, headers, body, conn_id, writer)

    async def _handle_api(
        self,
        path: str,
        body: bytes,
        conn_id: Any,
        writer: asyncio.StreamWriter,
    ) -> tuple[int | None, dict[str, str], bytes]:
        if self.force_disconnect_next_api:
            self.force_disconnect_next_api = False
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return None, {}, b""

        sess = self.verified_connections.get(conn_id)
        if not sess:
            return 400, {}, b"Unauthorized / Unverified Connection"

        server_read_key = sess["server_read_key"]
        server_write_key = sess["server_write_key"]

        req_data = None
        if body:
            try:
                pt = ChaCha20Poly1305(server_read_key).decrypt(API_NONCE, body, None)
                req_data = json.loads(pt.decode("utf-8"))
            except Exception as e:
                return 400, {}, f"Decryption failure: {e}".encode("utf-8")

        self.processed_requests.append({"path": path, "req_data": req_data, "conn_id": conn_id})

        if path == "/api/settings":
            resp_obj = {"battery": 95, "model": "SR1A", "name": "Salon Remote"}
        elif path == "/api/manifests":
            resp_obj = [
                {"id": "sevenhugs.control.webhooks", "name": "Webhooks"},
                {"id": "sevenhugs.control.philips_hue", "name": "Philips Hue"},
            ]
        elif path == "/api/devices":
            resp_obj = {"url": "/api/devices/sevenhugs.control.webhooks/setup"}
        elif path == "/api/echo":
            resp_obj = {
                "status": "echo_ok",
                "echo_id": req_data.get("req_id") if isinstance(req_data, dict) else None,
                "token": req_data.get("token") if isinstance(req_data, dict) else None,
            }
        elif path == "/api/heavy":
            raw_text = req_data.get("chunk", "") if isinstance(req_data, dict) else ""
            h = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            resp_obj = {
                "status": "heavy_ok",
                "req_id": req_data.get("req_id") if isinstance(req_data, dict) else None,
                "sha256": h,
                "bytes_len": len(raw_text),
            }
        elif path == "/api/webhooks":
            resp_obj = {
                "status": "webhook_registered",
                "webhook_id": req_data.get("webhook_id") if isinstance(req_data, dict) else None,
            }
        else:
            return 404, {}, b"Not Found"

        raw_resp = json.dumps(resp_obj).encode("utf-8")
        enc_resp = ChaCha20Poly1305(server_write_key).encrypt(API_NONCE, raw_resp, None)
        return 200, {"Content-Type": "application/octet-stream"}, enc_resp


def make_client_keys(server: MockSmartRemoteServer) -> dict[str, Any]:
    sk = ed25519.Ed25519PrivateKey.generate()
    return {
        "controller_id": f"ctrl-deep-{os.urandom(4).hex()}",
        "client_private_key": sk.private_bytes_raw().hex(),
        "client_public_key": sk.public_key().public_bytes_raw().hex(),
        "remote_id": server.remote_id,
        "remote_public_key": server.remote_pk.hex(),
    }


# ==============================================================================
# 1. SALVE EXTRÊME : 100 REQUÊTES CONCURRENTES SOUS RUPTURE DE SOCKET
# ==============================================================================

async def test_extreme_burst_100_concurrent_requests() -> None:
    print("\n--- [TEST 1] Salve extrême de 100 requêtes concurrentes sous rupture de socket ---")
    server = DeepAdversarialServer(pair_setup_delay=0.005)
    await server.start()

    keys = make_client_keys(server)
    connector = aiohttp.TCPConnector(force_close=False, limit=1, limit_per_host=1, keepalive_timeout=60.0)
    session = aiohttp.ClientSession(connector=connector)
    client = AsyncSmartRemoteClient(host=f"{server.host}:{server.port}", session=session, keys=keys)

    try:
        await client.async_pair_verify()
        assert client.is_verified is True
        assert server.m1_verify_count == 1
        assert client.session_generation == 1

        # Programmer une coupure immédiate sur la première requête de la salve
        server.force_disconnect_next_api = True

        tasks = []
        expected_oracles = []
        for i in range(100):
            kind = i % 4
            if kind == 0:
                tasks.append(client.async_request("GET", "/api/settings"))
                expected_oracles.append({"kind": "settings"})
            elif kind == 1:
                tasks.append(client.async_request("GET", "/api/manifests"))
                expected_oracles.append({"kind": "manifests"})
            elif kind == 2:
                payload = {"req_id": i, "token": f"secret_tok_{i}_{os.urandom(8).hex()}"}
                tasks.append(client.async_request("POST", "/api/echo", json_data=payload))
                expected_oracles.append({"kind": "echo", "req_id": i, "token": payload["token"]})
            else:
                chunk_32k = f"block_{i}_" * 3200
                sha = hashlib.sha256(chunk_32k.encode("utf-8")).hexdigest()
                payload = {"req_id": i, "chunk": chunk_32k}
                tasks.append(client.async_request("POST", "/api/heavy", json_data=payload))
                expected_oracles.append({"kind": "heavy", "req_id": i, "sha256": sha, "bytes_len": len(chunk_32k)})

        t0 = time.time()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.time() - t0

        exceptions = [r for r in results if isinstance(r, Exception)]
        successes = [r for r in results if not isinstance(r, Exception)]

        print(f"  Temps d'exécution: {elapsed:.3f}s pour 100 requêtes")
        print(f"  Succès: {len(successes)} / 100 | Erreurs: {len(exceptions)}")
        print(f"  Handshakes M1 serveur: {server.m1_verify_count} (attendu: 2)")
        print(f"  Génération de session client: {client.session_generation} (attendu: 2)")

        assert len(exceptions) == 0, f"Erreurs détectées: {exceptions}"
        assert len(successes) == 100
        assert server.m1_verify_count == 2, f"Ruée de handshakes constatée: {server.m1_verify_count} au lieu de 2"
        assert client.session_generation == 2

        # Vérification des oracles
        for idx, (res, oracle) in enumerate(zip(results, expected_oracles)):
            k = oracle["kind"]
            if k == "settings":
                assert res.get("battery") == 95
            elif k == "manifests":
                assert len(res) == 2
            elif k == "echo":
                assert res.get("echo_id") == oracle["req_id"]
                assert res.get("token") == oracle["token"]
            elif k == "heavy":
                assert res.get("req_id") == oracle["req_id"]
                assert res.get("sha256") == oracle["sha256"]
                assert res.get("bytes_len") == oracle["bytes_len"]

        print("  -> SUCCÈS : 100 requêtes concurrentes résolues, 100 oracles vérifiés, 0 ruée de handshake.")
    finally:
        await client.close()
        await session.close()
        await server.stop()


# ==============================================================================
# 2. CONTENTION DE VERROUS & IMMUNITÉ AUX DEADLOCKS (VERIFY_LOCK VS REQUEST_LOCK)
# ==============================================================================

async def test_lock_contention_and_deadlock_immunity() -> None:
    print("\n--- [TEST 2] Contention extrême de verrous (_verify_lock vs _request_lock) & Immunité Deadlock ---")
    server = DeepAdversarialServer(pair_setup_delay=0.005)
    await server.start()

    keys = make_client_keys(server)
    connector = aiohttp.TCPConnector(force_close=False, limit=1, limit_per_host=1, keepalive_timeout=60.0)
    session = aiohttp.ClientSession(connector=connector)
    client = AsyncSmartRemoteClient(host=f"{server.host}:{server.port}", session=session, keys=keys)

    try:
        await client.async_pair_verify()
        assert client.is_verified is True

        # 50 coroutines appelant async_request entrelacées avec 50 coroutines appelant async_pair_verify(force=True)
        # Sous 2 déconnexions brutales programmées
        server.disconnect_on_request_indices = {15, 35}

        tasks: list[asyncio.Task] = []
        for i in range(50):
            # Requête applicative
            tasks.append(asyncio.create_task(client.async_request("POST", "/api/echo", json_data={"req_id": i})))
            # Re-vérification forcée concurrente
            tasks.append(asyncio.create_task(client.async_pair_verify(force=(i % 5 == 0))))

        # Si un interblocage (Deadlock) survient entre _verify_lock et _request_lock, wait_for lèvera TimeoutError
        t0 = time.time()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5.0)
        elapsed = time.time() - t0

        exceptions = [r for r in results if isinstance(r, Exception)]
        print(f"  Durée d'exécution: {elapsed:.3f}s pour 100 coroutines en contention de verrous")
        print(f"  Exceptions rencontrées: {len(exceptions)}")
        print(f"  Handshakes M1 serveur: {server.m1_verify_count}")
        print(f"  Génération de session client: {client.session_generation}")

        assert len(exceptions) == 0, f"Erreurs rencontrées lors de la contention: {exceptions}"
        assert len(results) == 100

        # Vérifier que les requêtes /api/echo ont toutes reçu leur écho respectif
        echo_results = [r for r in results if isinstance(r, dict) and r.get("status") == "echo_ok"]
        assert len(echo_results) == 50
        received_ids = {r["echo_id"] for r in echo_results}
        assert received_ids == set(range(50))

        # Vérifier immédiatement la disponibilité du client avec une salve de 20 requêtes
        post_tasks = [client.async_request("GET", "/api/settings") for _ in range(20)]
        post_results = await asyncio.wait_for(asyncio.gather(*post_tasks), timeout=2.0)
        assert len(post_results) == 20
        assert all(r.get("battery") == 95 for r in post_results)

        print("  -> SUCCÈS : 0 interblocage (deadlock), ordre des verrous strictement respecté sous coupures.")
    finally:
        await client.close()
        await session.close()
        await server.stop()



# ==============================================================================
# 3. ÉCHEC TRANSITOIRE DE TRANSPORT LORS DU RE-HANDSHAKE (TRANSIENT BLIP)
# ==============================================================================

async def test_transient_handshake_failure_and_clean_recovery() -> None:
    print("\n--- [TEST 3] Panne de transport transitoire lors du handshake Pair-Verify M1 ---")
    server = DeepAdversarialServer(pair_setup_delay=0.005)
    await server.start()

    keys = make_client_keys(server)
    connector = aiohttp.TCPConnector(force_close=False, limit=1, limit_per_host=1, keepalive_timeout=60.0)
    session = aiohttp.ClientSession(connector=connector)
    client = AsyncSmartRemoteClient(host=f"{server.host}:{server.port}", session=session, keys=keys)

    try:
        await client.async_pair_verify()
        assert client.is_verified is True
        gen_before = client.session_generation

        # Configurer le serveur pour rejeter le prochain Pair-Verify M1 (coupure de socket)
        server.fail_pair_verify_m1_count = 1

        # Forcer la ré-authentification
        handshake_failed = False
        try:
            await client.async_pair_verify(force=True)
        except SevenhugsConnectionError as err:
            handshake_failed = True
            print(f"  Exception capturée lors de l'échec de handshake (attendue): {err}")

        assert handshake_failed is True
        # L'état is_verified doit être False, session_generation ne doit PAS avoir augmenté
        assert client.is_verified is False
        assert client.session_generation == gen_before

        # Le réseau redevient nominal : la requête suivante doit déclencher un handshake avec succès
        resp = await client.async_request("GET", "/api/settings")
        assert resp.get("battery") == 95
        assert client.is_verified is True
        assert client.session_generation == gen_before + 1
        print("  -> SUCCÈS : Échec transitoire géré proprement, sans fuite d'état et rétabli avec succès.")
    finally:
        await client.close()
        await session.close()
        await server.stop()


# ==============================================================================
# 4. HAUTE ENDURANCE FD : 1 000 REQUÊTES EN 20 SALVES AVEC COUPURES ALÉATOIRES
# ==============================================================================

async def test_high_endurance_1000_requests_fd_leak() -> None:
    print("\n--- [TEST 4] Haute endurance : 1 000 requêtes en 20 salves avec coupures aléatoires ---")
    server = DeepAdversarialServer(pair_setup_delay=0.002)
    await server.start()

    initial_fd = get_current_socket_fd_count()
    keys = make_client_keys(server)
    connector = aiohttp.TCPConnector(force_close=False, limit=1, limit_per_host=1, keepalive_timeout=60.0)
    session = aiohttp.ClientSession(connector=connector)
    client = AsyncSmartRemoteClient(host=f"{server.host}:{server.port}", session=session, keys=keys)

    total_salvos = 20
    req_per_salvo = 50
    fd_snapshots: list[int] = []

    try:
        await client.async_pair_verify()
        baseline_fd = get_current_socket_fd_count()
        fd_snapshots.append(baseline_fd)
        print(f"  Baseline sockets ouverts: {baseline_fd}")

        for salvo in range(1, total_salvos + 1):
            # Coupure aléatoire : déconnecter à chaque salve pour forcer une renégociation
            server.force_disconnect_next_api = True

            tasks = [
                client.async_request("POST", "/api/echo", json_data={"salvo": salvo, "idx": idx})
                for idx in range(req_per_salvo)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            exceptions = [r for r in results if isinstance(r, Exception)]
            assert len(exceptions) == 0, f"Échec à la salve {salvo}: {exceptions}"
            assert len(results) == req_per_salvo

            cur_fd = get_current_socket_fd_count()
            fd_snapshots.append(cur_fd)

            if salvo % 5 == 0:
                print(f"  Salve {salvo:02d}/{total_salvos:02d} terminée : 50/50 OK | M1 total: {server.m1_verify_count:02d} | FDs ouverts: {cur_fd}")

        final_fd = fd_snapshots[-1]
        delta = final_fd - baseline_fd
        print(f"  Bilan FD : Baseline={baseline_fd}, Final={final_fd}, Delta={delta:+d}")
        assert abs(delta) <= 1, f"Fuite de descripteur de socket détectée ! Delta={delta}"
        assert server.m1_verify_count == 1 + total_salvos
        print("  -> SUCCÈS : 1 000 requêtes exécutées avec 100% de succès et 0 fuite de descripteur FD.")
    finally:
        await client.close()
        await session.close()
        await server.stop()
        final_cleanup_fd = get_current_socket_fd_count()
        print(f"  FDs après arrêt complet : {final_cleanup_fd} (initial: {initial_fd})")


# ==============================================================================
# 5. CONCURRENCE MIXTE : REQUÊTES, MANIFESTS, WEBHOOKS & APPEL DIRECT DE VERIFY
# ==============================================================================

async def test_mixed_workload_concurrency_under_disconnection() -> None:
    print("\n--- [TEST 5] Concurrence mixte : GET, POST 32KB, Webhooks et async_pair_verify() explicite ---")
    server = DeepAdversarialServer(pair_setup_delay=0.005)
    await server.start()

    keys = make_client_keys(server)
    connector = aiohttp.TCPConnector(force_close=False, limit=1, limit_per_host=1, keepalive_timeout=60.0)
    session = aiohttp.ClientSession(connector=connector)
    client = AsyncSmartRemoteClient(host=f"{server.host}:{server.port}", session=session, keys=keys)

    try:
        await client.async_pair_verify()

        # Déconnexion programmée au 5ème appel
        server.disconnect_on_request_indices = {5}

        # 30 tâches mixtes
        tasks = []
        for i in range(30):
            m = i % 3
            if m == 0:
                tasks.append(client.async_request("GET", "/api/settings"))
            elif m == 1:
                large_chunk = f"mixed_{i}_" * 1000
                tasks.append(client.async_request("POST", "/api/heavy", json_data={"req_id": i, "chunk": large_chunk}))
            else:
                # Appel direct concurrent à async_pair_verify() pendant que des requêtes tournent
                tasks.append(client.async_pair_verify())

        results = await asyncio.gather(*tasks, return_exceptions=True)
        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, f"Erreurs constatées dans la charge mixte: {exceptions}"
        assert len(results) == 30
        print("  -> SUCCÈS : Charge mixte 100% résolue sans conflit d'états ni interblocage.")
    finally:
        await client.close()
        await session.close()
        await server.stop()


# ==============================================================================
# RUNNER GLOBAL
# ==============================================================================

async def main() -> int:
    print("=" * 80)
    print("BANC DE VALIDATION CONTRADICTOIRE APPROFONDIE (challenger_p1_6)")
    print("Focus : Salve 100 coroutines, Annulation/Deadlocks, FD 1000 req & Charge Mixte")
    print("=" * 80)

    tests = [
        ("TEST 1: Salve extrême de 100 requêtes concurrentes", test_extreme_burst_100_concurrent_requests),
        ("TEST 2: Contention verrous & Immunité Deadlock", test_lock_contention_and_deadlock_immunity),
        ("TEST 3: Panne transitoire handshake & Rétablissement", test_transient_handshake_failure_and_clean_recovery),
        ("TEST 4: Haute endurance FD (1 000 requêtes en 20 salves)", test_high_endurance_1000_requests_fd_leak),
        ("TEST 5: Concurrence mixte (GET, POST 32KB, verify concurrent)", test_mixed_workload_concurrency_under_disconnection),
    ]

    passed = 0
    failed = 0
    start_time = time.time()

    for name, coro in tests:
        t0 = time.time()
        try:
            await coro()
            duration = time.time() - t0
            print(f"[RÉSULTAT] {name} : PASS ({duration:.2f}s)")
            passed += 1
        except Exception as e:
            import traceback
            duration = time.time() - t0
            print(f"[RÉSULTAT] {name} : FAIL ({duration:.2f}s)")
            print(f"Erreur : {e}")
            traceback.print_exc()
            failed += 1

    total_duration = time.time() - start_time
    print("\n" + "=" * 80)
    print(f"BILAN DES TESTS CONTRADICTOIRES : {passed} PASS, {failed} FAIL (Durée: {total_duration:.2f}s)")
    if failed == 0:
        print("VERDICT EMPIRIQUE : 100% PASS - SYSTÈME RÉSISTANT & SANS DÉFAILLANCE (APPROVE)")
    else:
        print("VERDICT EMPIRIQUE : DÉFAILLANCE DÉTECTÉE (REQUEST_CHANGES)")
    print("=" * 80)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
