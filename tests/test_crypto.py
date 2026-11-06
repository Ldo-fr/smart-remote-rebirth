"""Tests unitaires pour custom_components/sevenhugs_smart_remote/crypto.py.

Valide :
- TLV8 : types primitifs, découpage automatique (> 255 octets), réassemblage et rejet des paquets tronqués.
- NimbusDS BigInteger : conversions grand-boutistes, suppression du zéro non signé de tête sur hash SHA-512.
- SRP-6a 3072 bits (RFC 5054) : groupe MODP, calculs déterministes M1 et vérification de la preuve M2.
- HKDF-SHA512 : dérivation symétrique dans les 5 contextes Sevenhugs.
- ChaCha20-Poly1305 : chiffrement monolithique avec API_NONCE et détection stricte d'altération du tag de 16 octets.
"""

from __future__ import annotations

import hashlib
import os
import sys

# Injection de l'environnement de test
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.conftest  # Configure le path et les shims si besoin

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from custom_components.sevenhugs_smart_remote.crypto import (
    API_NONCE,
    NONCE_PS_MSG05,
    NONCE_PS_MSG06,
    NONCE_PV_MSG02,
    NONCE_PV_MSG03,
    SRP_G,
    SRP_N,
    big_integer_from_bytes,
    big_integer_to_bytes,
    compute_srp_m1_sync,
    decode_tlv,
    decrypt_api_payload,
    encode_tlv,
    encrypt_api_payload,
    hkdf_sha512,
    pad_to_n,
    verify_srp_m2,
)


def test_tlv8_primitive_types() -> None:
    """Valide la sérialisation et la désérialisation de tous les types primitifs supportés."""
    items = [
        (0x01, "Salon"),
        (0x02, 42),
        (0x03, b"\xde\xad\xbe\xef"),
        (0x04, ""),
        (0x05, b""),
    ]
    encoded = encode_tlv(items)
    decoded = decode_tlv(encoded)

    assert decoded[0x01] == b"Salon"
    assert decoded[0x02] == b"\x2a"  # 42 en 1 octet
    assert decoded[0x03] == b"\xde\xad\xbe\xef"
    assert decoded[0x04] == b""
    assert decoded[0x05] == b""


def test_tlv8_chunking_large_payload() -> None:
    """Valide le découpage automatique en tranches de 255 octets pour les charges > 255 octets."""
    payload_384 = b"\xaa" * 384
    encoded = encode_tlv([(0x03, payload_384)])

    # Doit contenir exactement deux segments : Tag(1) + Len(255) + 255 octets + Tag(1) + Len(129) + 129 octets
    # Longueur totale attendue = (1 + 1 + 255) + (1 + 1 + 129) = 257 + 131 = 388 octets
    assert len(encoded) == 388
    assert encoded[0] == 0x03
    assert encoded[1] == 255
    assert encoded[257] == 0x03
    assert encoded[258] == 129

    # Vérification avec un payload de 1000 octets (4 tranches : 255, 255, 255, 235)
    payload_1000 = b"\xbb" * 1000
    encoded_1000 = encode_tlv([(0x07, payload_1000)])
    assert len(encoded_1000) == 1000 + (4 * 2)  # 4 en-têtes TLV de 2 octets
    assert encoded_1000[0] == 0x07
    assert encoded_1000[1] == 255
    assert encoded_1000[257] == 0x07
    assert encoded_1000[258] == 255
    assert encoded_1000[514] == 0x07
    assert encoded_1000[515] == 255
    assert encoded_1000[771] == 0x07
    assert encoded_1000[772] == 235


def test_tlv8_reassembly_roundtrip() -> None:
    """Valide que decode_tlv réassemble fidèlement les fragments successifs d'un même tag."""
    payload_384 = b"\x12\x34\x56\x78" * 96  # 384 octets
    payload_1000 = bytes(range(256)) * 3 + b"\x99" * 232  # 1000 octets

    items = [
        (0x01, "Test Reassembly"),
        (0x03, payload_384),
        (0x06, 1),
        (0x07, payload_1000),
    ]
    encoded = encode_tlv(items)
    decoded = decode_tlv(encoded)

    assert decoded[0x01] == b"Test Reassembly"
    assert decoded[0x03] == payload_384
    assert decoded[0x06] == b"\x01"
    assert decoded[0x07] == payload_1000


def test_tlv8_malformed_headers_raise_value_error() -> None:
    """Valide qu'un paquet TLV tronqué lève une ValueError explicite."""
    # En-tête incomplet (1 seul octet)
    with pytest.raises(ValueError, match="En-tête TLV incomplet"):
        decode_tlv(b"\x01")

    # Données tronquées (longueur déclarée 10, seulement 5 octets disponibles)
    with pytest.raises(ValueError, match="Données TLV tronquées"):
        decode_tlv(b"\x01\x0a\x01\x02\x03\x04\x05")


def test_big_integer_conversion_roundtrip() -> None:
    """Valide l'invariance de conversion grand-boutiste big_integer_from_bytes(big_integer_to_bytes(x)) == x."""
    test_values = [
        0,
        1,
        127,
        128,
        255,
        256,
        65535,
        65536,
        (1 << 512) - 1,
        (1 << 3072) - 1,
    ]
    for val in test_values:
        b = big_integer_to_bytes(val)
        res = big_integer_from_bytes(b)
        assert res == val, f"Échec d'invariance pour val={val}"


def test_big_integer_to_bytes_high_bit_set() -> None:
    """Valide qu'un entier avec bit de poids fort 1 n'a pas d'octet 0x00 non signé de tête artificiel."""
    # 0x80 = 128 : to_bytes(signed=True) donne b"\x00\x80"
    # big_integer_to_bytes doit supprimer ce 0x00 et renvoyer b"\x80"
    b_128 = big_integer_to_bytes(128)
    assert b_128 == b"\x80"
    assert len(b_128) == 1

    # 0xFF = 255 : to_bytes(signed=True) donne b"\x00\xFF" -> doit renvoyer b"\xFF"
    b_255 = big_integer_to_bytes(255)
    assert b_255 == b"\xff"
    assert len(b_255) == 1

    # Valeur zéro
    assert big_integer_to_bytes(0) == b"\x00"


def test_big_integer_to_bytes_leading_zero_hash() -> None:
    """Valide le cas critique NimbusDS : un hash SHA-512 débutant par 0x00 doit produire 63 octets."""
    # Simulation d'un digest SHA-512 de 64 octets dont le premier octet est 0x00
    fake_sha512 = b"\x00" + b"\x55" * 63
    assert len(fake_sha512) == 64

    # Conversion conforme NimbusDS (Java: BigIntegerUtils.bigIntegerToBytes(new BigInteger(1, bytes)))
    int_val = big_integer_from_bytes(fake_sha512)
    normalized_bytes = big_integer_to_bytes(int_val)

    assert len(normalized_bytes) == 63
    assert normalized_bytes[0] == 0x55
    assert normalized_bytes == b"\x55" * 63


def test_pad_to_n() -> None:
    """Valide le bourrage grand-boutiste à 384 octets pour le groupe 3072-bit."""
    short_data = b"\x01\x02\x03"
    padded = pad_to_n(short_data, 384)
    assert len(padded) == 384
    assert padded.startswith(b"\x00" * 381)
    assert padded.endswith(b"\x01\x02\x03")

    # Données déjà à 384 octets ou plus : inchangées
    exact_data = b"\xaa" * 384
    assert pad_to_n(exact_data, 384) == exact_data

    longer_data = b"\xbb" * 400
    assert pad_to_n(longer_data, 384) == longer_data


def test_rfc5054_group_parameters() -> None:
    """Valide les paramètres du groupe MODP SRP-6a 3072-bit (RFC 5054)."""
    assert SRP_G == 5
    assert SRP_N.bit_length() == 3072
    n_bytes = big_integer_to_bytes(SRP_N)
    assert len(n_bytes) == 384

    # Calcul invariant du multiplicateur k = SHA512(pad384(N) || pad384(g))
    n_padded = pad_to_n(n_bytes, 384)
    g_padded = pad_to_n(big_integer_to_bytes(SRP_G), 384)
    k_digest = hashlib.sha512(n_padded + g_padded).digest()
    assert len(k_digest) == 64


def test_srp_m1_m2_proof_calculation() -> None:
    """Valide le calcul déterministe de M1 et la vérification de M2 avec des vecteurs fixes."""
    salt = bytes.fromhex("0123456789ABCDEF0123456789ABCDEF")
    pin = "123-45-678"
    a_override = b"\x01" * 128
    b_dummy = pad_to_n(b"\x02" * 100, 384)

    a_bytes, m1_proof, shared_key_k = compute_srp_m1_sync(
        salt_bytes=salt,
        b_bytes=b_dummy,
        pin_code=pin,
        a_bytes_override=a_override,
    )

    # Clé publique client A = g^a mod N doit mesurer 384 octets
    assert len(a_bytes) == 384
    # Preuve M1 (SHA-512) normalisée
    assert len(m1_proof) in (63, 64)
    # Clé de session partagée K
    assert len(shared_key_k) == 64

    # Validation de la preuve M2 correspondante
    expected_m2_raw = hashlib.sha512(a_bytes + m1_proof + shared_key_k).digest()
    assert verify_srp_m2(a_bytes, m1_proof, shared_key_k, expected_m2_raw) is True

    # Tolérance du zéro de tête NimbusDS dans la preuve M2 reçue
    assert verify_srp_m2(a_bytes, m1_proof, shared_key_k, b"\x00" + expected_m2_raw) is True

    # Rejet d'une preuve falsifiée
    corrupted_m2 = bytearray(expected_m2_raw)
    corrupted_m2[0] ^= 0xFF
    assert verify_srp_m2(a_bytes, m1_proof, shared_key_k, bytes(corrupted_m2)) is False

    # Rejet en cas de preuve vide
    assert verify_srp_m2(a_bytes, m1_proof, shared_key_k, b"") is False


def test_srp_invalid_server_b_mod_n_raises() -> None:
    """Valide que compute_srp_m1_sync rejette formellement une clé serveur B invalide (B mod N == 0)."""
    salt = b"\x00" * 16
    with pytest.raises(ValueError, match="Clé publique du serveur B invalide"):
        compute_srp_m1_sync(salt_bytes=salt, b_bytes=b"\x00", pin_code="123-45-678")


def test_hkdf_sha512_derivation() -> None:
    """Valide la dérivation symétrique de clés via HKDF-SHA512."""
    ikm = b"\x42" * 32
    salt = b"Test-Salt"
    info = b"Test-Info"

    key1 = hkdf_sha512(ikm, salt, info, length=32)
    key2 = hkdf_sha512(ikm, salt, info, length=32)
    assert len(key1) == 32
    assert key1 == key2

    # Changement d'info -> clé différente
    key3 = hkdf_sha512(ikm, salt, b"Other-Info", length=32)
    assert key1 != key3


def test_chacha20_encrypt_decrypt_roundtrip() -> None:
    """Valide le chiffrement et déchiffrement authentifié avec API_NONCE."""
    key = ChaCha20Poly1305.generate_key()
    plaintext = b'{"status": "ok", "battery": 95, "model": "SR1A"}'

    ciphertext = encrypt_api_payload(key, plaintext)
    # Longueur = len(plaintext) + 16 octets de tag Poly1305
    assert len(ciphertext) == len(plaintext) + 16

    decrypted = decrypt_api_payload(key, ciphertext)
    assert decrypted == plaintext


def test_chacha20_tampered_ciphertext_raises_invalid_tag() -> None:
    """Valide que la modification d'un seul octet de charge utile ou de tag Poly1305 lève InvalidTag."""
    key = ChaCha20Poly1305.generate_key()
    plaintext = b"Payload critique a proteger"
    ciphertext = bytearray(encrypt_api_payload(key, plaintext))

    # Altération d'un octet de données chiffrées
    ciphertext[0] ^= 0x01
    with pytest.raises(InvalidTag):
        decrypt_api_payload(key, bytes(ciphertext))

    # Restauration puis altération d'un octet du tag Poly1305 final (16 derniers octets)
    ciphertext[0] ^= 0x01
    ciphertext[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        decrypt_api_payload(key, bytes(ciphertext))

    # Ciphertext de longueur inférieure à 16 octets
    with pytest.raises(InvalidTag):
        decrypt_api_payload(key, b"\x00" * 15)


def test_session_nonces() -> None:
    """Valide l'utilisation des différents nonces de handshake Pair-Setup et Pair-Verify."""
    key = ChaCha20Poly1305.generate_key()
    data = b"Handshake payload"

    for nonce in (NONCE_PS_MSG05, NONCE_PS_MSG06, NONCE_PV_MSG02, NONCE_PV_MSG03, API_NONCE):
        assert len(nonce) == 12
        chacha = ChaCha20Poly1305(key)
        enc = chacha.encrypt(nonce, data, None)
        dec = chacha.decrypt(nonce, enc, None)
        assert dec == data


def run_all_tests() -> int:
    """Exécute l'ensemble des tests unitaires de crypto.py et rapporte le bilan."""
    tests = [
        test_tlv8_primitive_types,
        test_tlv8_chunking_large_payload,
        test_tlv8_reassembly_roundtrip,
        test_tlv8_malformed_headers_raise_value_error,
        test_big_integer_conversion_roundtrip,
        test_big_integer_to_bytes_high_bit_set,
        test_big_integer_to_bytes_leading_zero_hash,
        test_pad_to_n,
        test_rfc5054_group_parameters,
        test_srp_m1_m2_proof_calculation,
        test_srp_invalid_server_b_mod_n_raises,
        test_hkdf_sha512_derivation,
        test_chacha20_encrypt_decrypt_roundtrip,
        test_chacha20_tampered_ciphertext_raises_invalid_tag,
        test_session_nonces,
    ]
    passed = 0
    failed = 0
    print(f"\n=== Lancement de {len(tests)} tests pour test_crypto.py ===")
    for test_fn in tests:
        try:
            test_fn()
            print(f"  [PASS] {test_fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {test_fn.__name__}: {e}")
            failed += 1

    print(f"=== Bilan test_crypto.py : {passed} réussis, {failed} échoués ===\n")
    return failed


def load_tests(loader: Any, standard_tests: Any, pattern: Any) -> Any:
    """Protocole de découverte unittest pour exécuter les tests sans dépendance externe."""
    import unittest
    suite = unittest.TestSuite()
    tests = [
        test_tlv8_primitive_types,
        test_tlv8_chunking_large_payload,
        test_tlv8_reassembly_roundtrip,
        test_tlv8_malformed_headers_raise_value_error,
        test_big_integer_conversion_roundtrip,
        test_big_integer_to_bytes_high_bit_set,
        test_big_integer_to_bytes_leading_zero_hash,
        test_pad_to_n,
        test_rfc5054_group_parameters,
        test_srp_m1_m2_proof_calculation,
        test_srp_invalid_server_b_mod_n_raises,
        test_hkdf_sha512_derivation,
        test_chacha20_encrypt_decrypt_roundtrip,
        test_chacha20_tampered_ciphertext_raises_invalid_tag,
        test_session_nonces,
    ]
    for fn in tests:
        suite.addTest(unittest.FunctionTestCase(fn))
    return suite


if __name__ == "__main__":
    sys.exit(run_all_tests())
