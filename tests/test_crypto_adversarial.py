"""Banc d'essais empiriques et contradictoires pour crypto.py (Phase 1).

Suite de tests de résistance développée par challenger_p1_1 :
1. Émulation NimbusDS BigInteger (Monte Carlo MSB=1, cas limites, zéros de tête SHA-512)
2. Découpage et réassemblage TLV8 (0, 1, 254, 255, 256, 384, 510, 511, 1024, 5000 octets, flux malformés)
3. ChaCha20-Poly1305 (altérations de tag 16 octets, bit-flips ciphertext, troncatures)
4. SRP-6a RFC 5054 (Monte Carlo handshakes, vecteurs d'attaque B mod N == 0)
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sys
import time
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
import tests.conftest

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from custom_components.sevenhugs_smart_remote.crypto import (
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


def test_adversarial_nimbusds_biginteger() -> None:
    """Valide l'émulation NimbusDS BigInteger par Monte Carlo MSB=1 et digests SHA-512 tronqués."""
    boundary_values = [
        0,
        1,
        127,
        128,
        255,
        256,
        32767,
        32768,
        65535,
        65536,
        (1 << 64) - 1,
        (1 << 128) - 1,
        (1 << 256) - 1,
        (1 << 512) - 1,
        (1 << 1024) - 1,
        (1 << 2048) - 1,
        (1 << 3072) - 1,
        SRP_N,
    ]
    for val in boundary_values:
        b = big_integer_to_bytes(val)
        recovered = big_integer_from_bytes(b)
        assert recovered == val
        if len(b) > 1:
            assert b[0] != 0
        elif len(b) == 1 and val != 0:
            assert b[0] != 0

    # Monte Carlo 2000 entiers MSB=1
    monte_carlo_count = 2000
    byte_sizes = [1, 2, 3, 4, 6, 8, 16, 32, 48, 64, 128, 256, 384]

    for i in range(monte_carlo_count):
        size = byte_sizes[i % len(byte_sizes)]
        raw_bytes = bytearray(secrets.token_bytes(size))
        raw_bytes[0] |= 0x80
        val = int.from_bytes(raw_bytes, byteorder="big")

        b = big_integer_to_bytes(val)
        assert len(b) == size
        assert b[0] != 0 or len(b) == 1
        assert big_integer_from_bytes(b) == val

    # SHA-512 synthétiques avec 1..4 octets nuls de tête
    for leading_zeros in range(1, 5):
        for msb_second in [0x10, 0x7F, 0x80, 0xFF]:
            fake_hash = (b"\x00" * leading_zeros) + bytes([msb_second]) + (b"\xaa" * (64 - leading_zeros - 1))
            val = big_integer_from_bytes(fake_hash)
            b = big_integer_to_bytes(val)
            expected_len = 64 - leading_zeros
            assert len(b) == expected_len
            assert b[0] == msb_second
            assert big_integer_from_bytes(b) == val

    # Digests SHA-512 naturels commençant par 0x00
    natural_found = 0
    for seed in range(25000):
        digest = hashlib.sha512(f"test_seed_{seed}".encode()).digest()
        if digest[0] == 0:
            natural_found += 1
            val = big_integer_from_bytes(digest)
            normalized = big_integer_to_bytes(val)
            assert len(normalized) <= 63
            assert normalized[0] != 0
            assert big_integer_from_bytes(normalized) == val
    assert natural_found > 0


def test_adversarial_tlv8_chunking_and_malformed() -> None:
    """Valide les limites de découpage TLV8 et le rejet propre des flux malformés."""
    exact_boundary_sizes = [0, 1, 254, 255, 256, 384, 510, 511, 1024, 5000]
    extra_sizes = [255 * 2, 255 * 3, 255 * 4, 255 * 4 + 1, 255 * 4 - 1, 765, 10000]

    for size in exact_boundary_sizes + extra_sizes:
        payload = secrets.token_bytes(size)
        tag = 0x05
        encoded = encode_tlv([(tag, payload)])

        if size == 0:
            expected_encoded_len = 2
            expected_chunks = 1
        else:
            expected_chunks = (size + 254) // 255
            expected_encoded_len = size + (expected_chunks * 2)

        assert len(encoded) == expected_encoded_len

        decoded = decode_tlv(encoded)
        assert tag in decoded
        assert decoded[tag] == payload

    # Flux malformés
    # A. En-tête incomplet
    try:
        decode_tlv(b"\x01")
        assert False
    except ValueError as e:
        assert "En-tête TLV incomplet" in str(e)

    # B. Longueur supérieure aux données disponibles
    try:
        decode_tlv(b"\x01\x0A\x00\x01\x02\x03\x04")
        assert False
    except ValueError as e:
        assert "Données TLV tronquées" in str(e)

    # C. Troncature du second fragment
    enc_300 = encode_tlv([(0x02, b"\x42" * 300)])
    try:
        decode_tlv(enc_300[:-10])
        assert False
    except ValueError as e:
        assert "Données TLV tronquées" in str(e)

    # D. Troncature en-tête du second fragment
    try:
        decode_tlv(enc_300[:258])
        assert False
    except ValueError as e:
        assert "En-tête TLV incomplet" in str(e)

    # E. Types invalides
    for invalid in [3.14, None, [1, 2], {"a": 1}]:
        try:
            encode_tlv([(0x01, invalid)])
            assert False
        except TypeError as e:
            assert "Type de valeur TLV non supporté" in str(e)


def test_adversarial_chacha20_poly1305_tampering() -> None:
    """Valide le rejet strict de toute altération de tag Poly1305 ou corps chiffré."""
    key = ChaCha20Poly1305.generate_key()
    plaintext = b"Payload JSON confidentiel Sevenhugs SR1A : {\"action\": \"power_off\", \"id\": 999}"
    ciphertext = encrypt_api_payload(key, plaintext)
    c_len = len(ciphertext)
    p_len = len(plaintext)
    assert c_len == p_len + 16

    # 1. 128 bit-flips dans le tag de 16 octets
    tag_rejections = 0
    for byte_idx in range(p_len, c_len):
        for bit_pos in range(8):
            corrupted = bytearray(ciphertext)
            corrupted[byte_idx] ^= (1 << bit_pos)
            try:
                decrypt_api_payload(key, bytes(corrupted))
                assert False
            except InvalidTag:
                tag_rejections += 1
    assert tag_rejections == 128

    # 2. Bit-flips dans le corps
    body_rejections = 0
    for byte_idx in range(p_len):
        for bit_pos in range(8):
            corrupted = bytearray(ciphertext)
            corrupted[byte_idx] ^= (1 << bit_pos)
            try:
                decrypt_api_payload(key, bytes(corrupted))
                assert False
            except InvalidTag:
                body_rejections += 1
    assert body_rejections == p_len * 8

    # 3. Troncatures de 0 à c_len - 1
    trunc_rejections = 0
    for trunc_len in range(c_len):
        try:
            decrypt_api_payload(key, ciphertext[:trunc_len])
            assert False
        except InvalidTag:
            trunc_rejections += 1
    assert trunc_rejections == c_len

    # 4. Splicing de tag
    key2 = ChaCha20Poly1305.generate_key()
    c2 = encrypt_api_payload(key, "Autre message chiffré".encode("utf-8"))
    spliced = ciphertext[:p_len] + c2[-16:]
    try:
        decrypt_api_payload(key, spliced)
        assert False
    except InvalidTag:
        pass

    # 5. Clé altérée
    key_corrupted = bytearray(key)
    key_corrupted[0] ^= 0x01
    try:
        decrypt_api_payload(bytes(key_corrupted), ciphertext)
        assert False
    except InvalidTag:
        pass

    # 6. Plaintext vide (16 octets)
    empty_ct = encrypt_api_payload(key, b"")
    assert len(empty_ct) == 16
    assert decrypt_api_payload(key, empty_ct) == b""
    corrupted_empty = bytearray(empty_ct)
    corrupted_empty[3] ^= 0x04
    try:
        decrypt_api_payload(key, bytes(corrupted_empty))
        assert False
    except InvalidTag:
        pass


def test_adversarial_srp6a_rfc5054_stress() -> None:
    """Valide la robustesse SRP-6a : rejet B mod N == 0 et 30 handshakes complets avec NimbusDS."""
    for inv_b in [b"\x00", b"\x00" * 384, big_integer_to_bytes(SRP_N), big_integer_to_bytes(2 * SRP_N)]:
        try:
            compute_srp_m1_sync(salt_bytes=b"\x01" * 16, b_bytes=inv_b, pin_code="123-45-678")
            assert False
        except ValueError as e:
            assert "Clé publique du serveur B invalide" in str(e)

    pin_code = "456-78-901"
    for _ in range(30):
        salt = secrets.token_bytes(16)
        salt_int = big_integer_from_bytes(salt)

        h_i_p = hashlib.sha512(b"Pair-Setup:" + pin_code.encode()).digest()
        x_val = big_integer_from_bytes(hashlib.sha512(big_integer_to_bytes(salt_int) + h_i_p).digest())
        v_val = pow(SRP_G, x_val, SRP_N)

        n_padded = pad_to_n(big_integer_to_bytes(SRP_N), 384)
        g_padded = pad_to_n(big_integer_to_bytes(SRP_G), 384)
        k_val = big_integer_from_bytes(hashlib.sha512(n_padded + g_padded).digest())

        b_priv = big_integer_from_bytes(secrets.token_bytes(128))
        b_pub = (k_val * v_val + pow(SRP_G, b_priv, SRP_N)) % SRP_N
        b_bytes = pad_to_n(big_integer_to_bytes(b_pub), 384)

        a_bytes, m1_proof, client_k = compute_srp_m1_sync(
            salt_bytes=salt,
            b_bytes=b_bytes,
            pin_code=pin_code,
        )

        a_int = big_integer_from_bytes(a_bytes)
        u_digest = hashlib.sha512(pad_to_n(a_bytes, 384) + pad_to_n(b_bytes, 384)).digest()
        u_val = big_integer_from_bytes(u_digest)
        s_server = pow((a_int * pow(v_val, u_val, SRP_N)) % SRP_N, b_priv, SRP_N)
        server_k = hashlib.sha512(big_integer_to_bytes(s_server)).digest()

        assert client_k == server_k

        hn = hashlib.sha512(big_integer_to_bytes(SRP_N)).digest()
        hg = hashlib.sha512(big_integer_to_bytes(SRP_G)).digest()
        h_xor = big_integer_to_bytes(big_integer_from_bytes(hn) ^ big_integer_from_bytes(hg))
        hi = hashlib.sha512(b"Pair-Setup").digest()
        expected_m1_input = h_xor + hi + big_integer_to_bytes(salt_int) + a_bytes + big_integer_to_bytes(b_pub) + server_k
        expected_m1 = big_integer_to_bytes(big_integer_from_bytes(hashlib.sha512(expected_m1_input).digest()))

        assert m1_proof == expected_m1

        server_m2 = big_integer_to_bytes(big_integer_from_bytes(hashlib.sha512(a_bytes + m1_proof + server_k).digest()))
        assert verify_srp_m2(a_bytes, m1_proof, client_k, server_m2) is True
        assert verify_srp_m2(a_bytes, m1_proof, client_k, b"\x00" + server_m2) is True

        bad_m2 = bytearray(server_m2)
        bad_m2[0] ^= 0xFF
        assert verify_srp_m2(a_bytes, m1_proof, client_k, bytes(bad_m2)) is False


def run_all_adversarial_tests() -> int:
    tests = [
        test_adversarial_nimbusds_biginteger,
        test_adversarial_tlv8_chunking_and_malformed,
        test_adversarial_chacha20_poly1305_tampering,
        test_adversarial_srp6a_rfc5054_stress,
    ]
    passed = 0
    failed = 0
    print(f"\n=== Lancement de {len(tests)} bancs d'essais adversariaux pour crypto.py ===")
    for test_fn in tests:
        try:
            t0 = time.perf_counter()
            test_fn()
            t1 = time.perf_counter()
            print(f"  [PASS] {test_fn.__name__} ({t1 - t0:.3f} s)")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {test_fn.__name__}: {e}")
            failed += 1

    print(f"=== Bilan bancs d'essais adversariaux : {passed} réussis, {failed} échoués ===\n")
    return failed


def load_tests(loader: Any, standard_tests: Any, pattern: Any) -> Any:
    import unittest
    suite = unittest.TestSuite()
    tests = [
        test_adversarial_nimbusds_biginteger,
        test_adversarial_tlv8_chunking_and_malformed,
        test_adversarial_chacha20_poly1305_tampering,
        test_adversarial_srp6a_rfc5054_stress,
    ]
    for fn in tests:
        suite.addTest(unittest.FunctionTestCase(fn))
    return suite


if __name__ == "__main__":
    sys.exit(run_all_adversarial_tests())
