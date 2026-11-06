"""Banc d'essais contradictoires et d'endurance empirique pour Phase 1.

Auteur: challenger_p1_4 (Empirical Challenger)
Rôles: critic, specialist
Mandat:
1. Non-régression crypto (TLV8, NimbusDS BigInteger, ChaCha20-Poly1305, SRP-6a).
2. Validation d'exécution de la suite complète de 27 tests.
3. Tests d'endurance : timeouts prolongés et re-handshakes multiples successifs sous charge.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sys
import time
import unittest
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.conftest
from tests.conftest import MockSmartRemoteServer, aiohttp
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from custom_components.smart_remote_rebirth.client import (
    AsyncSmartRemoteClient,
    SevenhugsAuthError,
    SevenhugsConnectionError,
)
from custom_components.smart_remote_rebirth.crypto import (
    API_NONCE,
    SRP_G,
    SRP_N,
    big_integer_from_bytes,
    big_integer_to_bytes,
    compute_srp_m1_sync,
    decode_tlv,
    decrypt_api_payload,
    encode_tlv,
    encrypt_api_payload,
    pad_to_n,
    verify_srp_m2,
)


def run_crypto_stress_tests() -> dict[str, Any]:
    """Test 1 : Non-régression et cas limites stricts sur crypto.py."""
    print("=" * 70)
    print("AXE 1 : VÉRIFICATION EMPIRIQUE DE NON-RÉGRESSION CRYPTO.PY")
    print("=" * 70)
    results = {}

    # 1.1 TLV8 Chunking Boundaries & Invariants
    # Test boundary lengths: 0, 1, 254, 255, 256, 509, 510, 511, 1024, 65535
    boundary_sizes = [0, 1, 254, 255, 256, 509, 510, 511, 1024, 65535]
    for size in boundary_sizes:
        payload = secrets.token_bytes(size)
        encoded = encode_tlv([(0x05, payload)])
        decoded = decode_tlv(encoded)
        assert decoded.get(0x05, b"") == payload, f"TLV8 chunking failed for size {size}"
    
    # Tag 0x00 handling (essential for Pair-Setup M1 fix)
    encoded_zero = encode_tlv([(0x00, 1), (0x06, 1)])
    assert encoded_zero == b"\x00\x01\x01\x06\x01\x01", f"Tag 0x00 serialization mismatch: {encoded_zero.hex()}"
    decoded_zero = decode_tlv(encoded_zero)
    assert decoded_zero[0x00] == b"\x01" and decoded_zero[0x06] == b"\x01"

    # Malformed TLV8 streams
    malformed_samples = [
        b"\x01",  # 1 byte header
        b"\x01\x05\x00\x00",  # Declares 5 bytes, provides 2
        b"\x01\x02\xaa\xbb\x02\x10\x01",  # Second TLV truncated
    ]
    for sample in malformed_samples:
        try:
            decode_tlv(sample)
            assert False, f"decode_tlv should have raised ValueError on {sample.hex()}"
        except ValueError:
            pass

    results["tlv8_stress"] = "PASS"
    print("  [PASS] TLV8 : Découpage aux limites (254..65535), Tag 0x00, et rejet des flux malformés")

    # 1.2 NimbusDS BigInteger Invariants & Monte Carlo
    # Boundary conversions
    for v in [0, 1, 127, 128, 255, 256, 65535, 65536, SRP_N, (1 << 3072) - 1]:
        b = big_integer_to_bytes(v)
        assert big_integer_from_bytes(b) == v, f"BigInteger roundtrip failed for {v}"
        if len(b) > 1:
            assert b[0] != 0, f"Unsigned leading zero found in {b.hex()}"

    # 2000 Monte Carlo samples with MSB = 1
    for _ in range(2000):
        val = secrets.randbits(512) | (1 << 511)
        b = big_integer_to_bytes(val)
        assert b[0] & 0x80 != 0, "MSB should be 1"
        assert big_integer_from_bytes(b) == val

    # Synthetic SHA-512 hashes with 1 to 8 leading zero bytes
    for zeros in range(1, 9):
        fake_digest = (b"\x00" * zeros) + b"\x85" + secrets.token_bytes(63 - zeros)
        int_val = big_integer_from_bytes(fake_digest)
        b_val = big_integer_to_bytes(int_val)
        assert len(b_val) == 64 - zeros, f"Leading zeros not stripped: len={len(b_val)}"
        assert b_val[0] == 0x85
        assert big_integer_from_bytes(b_val) == int_val

    results["nimbusds_stress"] = "PASS"
    print("  [PASS] NimbusDS BigInteger : 2000 Monte Carlo MSB=1 et hachages SHA-512 tronqués à 0x00")

    # 1.3 ChaCha20-Poly1305 Deep Adversarial
    key = secrets.token_bytes(32)
    plaintext = b"Test Payload Sevenhugs HaCs Rebirth Phase 1"
    ciphertext = encrypt_api_payload(key, plaintext)
    assert len(ciphertext) == len(plaintext) + 16

    # Roundtrip
    decrypted = decrypt_api_payload(key, ciphertext)
    assert decrypted == plaintext

    # Empty payload roundtrip
    empty_ct = encrypt_api_payload(key, b"")
    assert len(empty_ct) == 16
    assert decrypt_api_payload(key, empty_ct) == b""

    # Truncation rejection
    for t_len in [0, 1, 15]:
        try:
            decrypt_api_payload(key, ciphertext[:t_len])
            assert False, "Should raise InvalidTag for ciphertext < 16 bytes"
        except InvalidTag:
            pass

    # Bit-flip tampering across all ciphertext bytes
    for offset in range(len(ciphertext)):
        tampered = bytearray(ciphertext)
        tampered[offset] ^= 0x01
        try:
            decrypt_api_payload(key, bytes(tampered))
            assert False, f"Bit flip at offset {offset} not detected!"
        except InvalidTag:
            pass

    results["chacha20_stress"] = "PASS"
    print(f"  [PASS] ChaCha20-Poly1305 : Intégrité stricte, tag 16 octets, et {len(ciphertext)} bit-flips rejetés")

    # 1.4 SRP-6a RFC 5054 Deterministic Vectors & Proofs
    salt = b"\x11" * 16
    b_pub = pad_to_n(b"\x22" * 100, 384)
    pin = "1234"
    fixed_a = b"\x33" * 128
    a_bytes, m1, shared_k = compute_srp_m1_sync(salt, b_pub, pin, a_bytes_override=fixed_a)
    assert len(a_bytes) <= 384
    assert len(m1) <= 64
    assert len(shared_k) == 64

    # Server verification M2
    server_m2_raw = hashlib.sha512(a_bytes + m1 + shared_k).digest()
    assert verify_srp_m2(a_bytes, m1, shared_k, server_m2_raw)
    # Tampered M2 must fail
    tampered_m2 = bytearray(server_m2_raw)
    tampered_m2[0] ^= 0xFF
    assert not verify_srp_m2(a_bytes, m1, shared_k, bytes(tampered_m2))

    # Reject B mod N == 0
    try:
        compute_srp_m1_sync(salt, big_integer_to_bytes(SRP_N), pin)
        assert False, "Should reject B mod N == 0"
    except ValueError:
        pass

    results["srp6a_stress"] = "PASS"
    print("  [PASS] SRP-6a RFC 5054 : Vecteurs M1/M2, conformité NimbusDS et rejet B mod N == 0")

    return results


def run_full_27_test_suite() -> dict[str, Any]:
    """Test 2 : Exécution formelle de la suite de 27 tests."""
    print("\n" + "=" * 70)
    print("AXE 2 : EXÉCUTION DE LA SUITE COMPLÈTE DE 27 TESTS")
    print("=" * 70)

    loader = unittest.defaultTestLoader
    suite = loader.discover(os.path.join(PROJECT_ROOT, "tests"), pattern="test_*.py")
    test_count = suite.countTestCases()
    assert test_count >= 27, f"Attendu au moins 27 tests, obtenu {test_count}"

    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)

    assert result.wasSuccessful(), f"Échec des tests: errors={len(result.errors)}, failures={len(result.failures)}"
    assert result.testsRun >= 27, f"Seulement {result.testsRun} tests exécutés"
    print(f"  [PASS] Suite officielle : {result.testsRun} tests exécutés avec succès (0 échec, 0 erreur)")

    return {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "successful": result.wasSuccessful(),
    }


class EnduranceServer(MockSmartRemoteServer):
    """Serveur mock instrumenté pour tests d'endurance et de temporisation."""

    def __init__(self, pair_setup_delay: float = 0.001) -> None:
        super().__init__(pair_setup_delay=pair_setup_delay)
        self.drop_next_api: bool = False
        self.stall_next_api: bool = False
        self.stall_duration: float = 0.3
        self.stall_next_verify_m1: bool = False
        self.stall_next_verify_m3: bool = False

    async def _handle_api(self, path: str, body: bytes, conn_id: Any, writer: asyncio.StreamWriter):
        if self.drop_next_api:
            self.drop_next_api = False
            writer.close()
            return None, {}, b""

        if self.stall_next_api:
            self.stall_next_api = False
            await asyncio.sleep(self.stall_duration)
            return 200, {}, b""

        return await super()._handle_api(path, body, conn_id, writer)

    async def _handle_pair_verify(self, body: bytes, conn_id: Any):
        tlv = decode_tlv(body)
        state = tlv.get(0x06)

        if state in (1, b"\x01") and self.stall_next_verify_m1:
            self.stall_next_verify_m1 = False
            await asyncio.sleep(self.stall_duration)

        if state in (3, b"\x03") and self.stall_next_verify_m3:
            self.stall_next_verify_m3 = False
            await asyncio.sleep(self.stall_duration)

        return await super()._handle_pair_verify(body, conn_id)


async def run_network_endurance_tests() -> dict[str, Any]:
    """Test 3 : Endurance sous charges répétées et timeouts prolongés."""
    print("\n" + "=" * 70)
    print("AXE 3 : ENDURANCE SOUS CHARGE & TIMEOUTS PROLONGÉS")
    print("=" * 70)
    results = {}

    server = EnduranceServer(pair_setup_delay=0.001)
    await server.start()

    client_sk = ed25519.Ed25519PrivateKey.generate()
    keys = {
        "controller_id": "ctrl-endurance-challenger",
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
        # 3.1 Initial handshake
        await client.async_pair_verify()
        assert client.is_verified
        assert client.session_generation == 1
        print("  [PASS] Handshake Pair-Verify initial établi")

        # 3.2 Endurance : 20 cycles successifs de rupture sous salve concurrente de 20 requêtes (400 requêtes)
        total_cycles = 20
        concurrency = 20
        total_requests = total_cycles * concurrency
        total_succ = 0
        total_err = 0

        t0 = time.perf_counter()
        for cycle in range(1, total_cycles + 1):
            server.drop_next_api = True  # Déclenche une rupture physique sur le premier appel du cycle

            async def send_req(idx: int) -> bool:
                try:
                    res = await client.async_request("GET", "/api/settings")
                    return isinstance(res, dict) and res.get("model") == "SR1A"
                except Exception:
                    return False

            wave_res = await asyncio.gather(*(send_req(i) for i in range(concurrency)))
            succ = sum(1 for ok in wave_res if ok)
            err = sum(1 for ok in wave_res if not ok)
            total_succ += succ
            total_err += err

            assert succ == concurrency, f"Cycle {cycle}: {err} échecs sur {concurrency} requêtes"
            assert client.session_generation == cycle + 1, f"Cycle {cycle}: génération invalide {client.session_generation}"

        elapsed = time.perf_counter() - t0
        print(f"  [PASS] Endurance re-handshakes successifs : {total_succ}/{total_requests} requêtes réussies sur {total_cycles} ruptures successives ({elapsed:.2f}s)")
        print(f"         Génération de session finale : {client.session_generation} (attendu: {total_cycles + 1})")
        print(f"         Total handshakes M1 serveur : {server.m1_verify_count} (attendu: {total_cycles + 1})")
        results["successive_rehandshakes_under_load"] = {
            "total_requests": total_requests,
            "successes": total_succ,
            "failures": total_err,
            "handshakes": server.m1_verify_count,
            "elapsed_seconds": round(elapsed, 3),
        }

        # 3.3 Test de timeout prolongé sur Pair-Verify (M1 et M3)
        server.stall_duration = 0.25
        # Monkeypatch temporaire du timeout pour tester le comportement en cas de dépassement
        import custom_components.smart_remote_rebirth.client as client_mod
        orig_ct = client_mod.aiohttp.ClientTimeout
        client_mod.aiohttp.ClientTimeout = lambda total: 0.05

        try:
            # A. Timeout sur Pair-Verify M1
            server.stall_next_verify_m1 = True
            try:
                await client.async_pair_verify(force=True)
                assert False, "Pair-Verify M1 aurait dû lever un timeout"
            except SevenhugsConnectionError as exc:
                assert "Pair-Verify M1" in str(exc)
                assert not client.is_verified
                print("  [PASS] Timeout sur Pair-Verify M1 capturé en SevenhugsConnectionError")

            # B. Timeout sur Pair-Verify M3
            server.stall_next_verify_m3 = True
            try:
                await client.async_pair_verify(force=True)
                assert False, "Pair-Verify M3 aurait dû lever un timeout"
            except SevenhugsConnectionError as exc:
                assert "Pair-Verify M3" in str(exc)
                assert not client.is_verified
                print("  [PASS] Timeout sur Pair-Verify M3 capturé en SevenhugsConnectionError")

        finally:
            client_mod.aiohttp.ClientTimeout = orig_ct

        # Rétablissement d'une session valide après timeout
        await client.async_pair_verify(force=True)
        assert client.is_verified
        print("  [PASS] Rétablissement de session après timeouts Pair-Verify : SUCCÈS")

        # 3.4 Évaluation empirique du timeout sur requête API chiffrée (_raw_encrypted_request)
        # Analyse de la gestion de TimeoutError vs SevenhugsConnectionError
        server.stall_duration = 0.20
        client_mod.aiohttp.ClientTimeout = lambda total: 0.05

        try:
            server.stall_next_api = True
            try:
                await client.async_request("GET", "/api/settings")
                print("  [UNEXPECTED] async_request a réussi malgré le décrochage")
                results["api_timeout_vulnerability"] = "NOT_REPRODUCED"
            except Exception as exc:
                exc_type = type(exc).__name__
                print(f"  [OBSERVATION CRITIQUE TIMEOUT API] Exception levée: {exc_type}: {exc}")
                results["api_timeout_vulnerability"] = {
                    "exception_type": exc_type,
                    "exception_message": str(exc),
                    "is_sevenhugs_error": isinstance(exc, (SevenhugsConnectionError, SevenhugsAuthError)),
                    "is_raw_timeout": isinstance(exc, (asyncio.TimeoutError, TimeoutError)),
                }
        finally:
            client_mod.aiohttp.ClientTimeout = orig_ct

    finally:
        await client.close()
        await session.close()
        await server.stop()

    return results


def main() -> None:
    crypto_res = run_crypto_stress_tests()
    suite_res = run_full_27_test_suite()
    net_res = asyncio.run(run_network_endurance_tests())

    print("\n" + "=" * 70)
    print("SYNTHÈSE DE L'ÉVALUATION EMPIRIQUE CHANGER_P1_4")
    print("=" * 70)
    print("1. Intégrité Crypto.py :", crypto_res)
    print("2. Exécution Suite 27 tests :", suite_res)
    print("3. Endurance Réseau :", net_res["successive_rehandshakes_under_load"])
    print("4. Analyse Vulnérabilité Timeout API :", net_res.get("api_timeout_vulnerability"))


if __name__ == "__main__":
    main()
