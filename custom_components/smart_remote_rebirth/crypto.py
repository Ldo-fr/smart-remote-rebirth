"""Primitives cryptographiques pour Sevenhugs Smart Remote (RFC 5054 SRP-6a, Ed25519, X25519, TLV8).

Ce module implémente le moteur cryptographique complet sans dépendance réseau
ni dépendance directe vers Home Assistant Core.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sys

# Assure la disponibilité des dépendances vendorisées dans .pylib si présent
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYLIB_DIR = os.path.join(_BASE_DIR, ".pylib")
if os.path.isdir(_PYLIB_DIR) and _PYLIB_DIR not in sys.path:
    sys.path.insert(0, _PYLIB_DIR)

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Nonce universel pour le chiffrement des corps de requêtes/réponses API
API_NONCE: bytes = b"\x00\x00\x00\x00HuMiThFr"

# Nonces statiques pour les handshakes Pair-Setup et Pair-Verify
NONCE_PS_MSG05: bytes = b"\x00\x00\x00\x00PS-Msg05"
NONCE_PS_MSG06: bytes = b"\x00\x00\x00\x00PS-Msg06"
NONCE_PV_MSG02: bytes = b"\x00\x00\x00\x00PV-Msg02"
NONCE_PV_MSG03: bytes = b"\x00\x00\x00\x00PV-Msg03"

# Modulo premier du groupe SRP-6a 3072-bit (RFC 5054 / RFC 3526 MODP Groupe 15)
N_STR: str = (
    "5809605995369958062791915965639201402176612226902900533702900882779736177890990861472094774477339581147373410185646"
    "37832804372980075047009821092448786693505916437158816804754094398164451663275506750162643455639819318662899007124866"
    "08193612051197936939854332970361182329144101718768075364573912778570118498974102075191053333558011211093568974594262"
    "71845471397952675959440793493071628394122780510124618488232602464649876850458861245784240929258426287699705312584509"
    "62541951346360515542801716571446536309402160929056108402589366256122257320208286579782186527099114508220065697817719"
    "28270245389902399691755461907706456858934380117144304264093386763147435711545371420315730042764287014330363818017053"
    "08659830751190352946025482059931306571004727362479688415574702596946457770284148435989129632853918392117997472632693"
    "078113129886487399347796982772784615865232621289656944284216824611318709764535152507354116344703769998514148343807"
)
SRP_N: int = int(N_STR)
N: int = SRP_N
SRP_G: int = 5
g: int = SRP_G


def big_integer_to_bytes(val: int) -> bytes:
    """Sérialisation grand-boutiste conforme à NimbusDS BigIntegerUtils.bigIntegerToBytes.

    Supprime le zéro de tête non signé lorsque le bit de poids fort est à 1 en représentation signée.
    """
    length = (val.bit_length() + 8) // 8
    b = val.to_bytes(max(1, length), byteorder="big", signed=True)
    if len(b) > 1 and b[0] == 0:
        b = b[1:]
    return b


def big_integer_from_bytes(b: bytes) -> int:
    """Désérialise un grand entier positif grand-boutiste (équivalent Java: new BigInteger(1, b))."""
    return int.from_bytes(b, byteorder="big")


def pad_to_n(b: bytes, target_len: int = 384) -> bytes:
    """Bourrage grand-boutiste (justification à droite par des zéros 0x00) jusqu'à target_len octets."""
    if len(b) < target_len:
        return b.rjust(target_len, b"\x00")
    return b


def encode_tlv(items: list[tuple[int, bytes | int | str]]) -> bytes:
    """Sérialise en binaire TLV8 avec découpage automatique des morceaux > 255 octets.

    Pour chaque élément (tag, valeur), si la charge utile excède 255 octets, elle est
    automatiquement scindée en segments consécutifs de 255 octets avec le même identifiant
    de tag, conformément à la spécification Apple HAP / Sevenhugs.
    """
    out = bytearray()
    for tag, val in items:
        if isinstance(val, int):
            val_bytes = bytes([val])
        elif isinstance(val, str):
            val_bytes = val.encode("utf-8")
        elif isinstance(val, (bytes, bytearray)):
            val_bytes = bytes(val)
        else:
            raise TypeError(f"Type de valeur TLV non supporté: {type(val)}")

        if not val_bytes:
            out.append(tag)
            out.append(0)
            continue

        pos = 0
        while pos < len(val_bytes):
            chunk = val_bytes[pos : pos + 255]
            out.append(tag)
            out.append(len(chunk))
            out.extend(chunk)
            pos += len(chunk)
    return bytes(out)


def decode_tlv(data: bytes) -> dict[int, bytes]:
    """Désérialise un flux binaire TLV8 en réassemblant les fragments consécutifs de même tag.

    Concatène automatiquement les fragments de valeur consécutifs portant le même identifiant de tag
    pour reconstituer fidèlement les charges utiles > 255 octets (clés publiques SRP, etc.).
    Lève une ValueError explicite si le flux est tronqué ou malformé.
    """
    pos = 0
    res: dict[int, bytes] = {}
    while pos < len(data):
        if pos + 2 > len(data):
            raise ValueError(f"En-tête TLV incomplet à la position {pos}")
        tag = data[pos]
        length = data[pos + 1]
        if pos + 2 + length > len(data):
            raise ValueError(
                f"Données TLV tronquées à la position {pos}: attendu {length} octets, reste {len(data) - pos - 2}"
            )
        val = data[pos + 2 : pos + 2 + length]
        res[tag] = res.get(tag, b"") + val
        pos += 2 + length
    return res


def compute_srp_m1_sync(
    salt_bytes: bytes,
    b_bytes: bytes,
    pin_code: str,
    n_int: int = SRP_N,
    g_int: int = SRP_G,
    a_bytes_override: bytes | None = None,
) -> tuple[bytes, bytes, bytes]:
    """Exécute les calculs intensifs SRP-6a client 3072 bits (RFC 5054).

    Retourne le triplet: (A_bytes, M1_proof, shared_key_k).
    Garantit la compatibilité stricte NimbusDS sur la suppression des zéros de tête non signés.
    """
    b_int = big_integer_from_bytes(b_bytes)
    if b_int % n_int == 0:
        raise ValueError("Clé publique du serveur B invalide (B mod N == 0)")

    salt_int = big_integer_from_bytes(salt_bytes)

    # Clé privée éphémère client a (1024 bits / 128 octets aléatoires)
    if a_bytes_override is not None:
        a_bytes_raw = a_bytes_override
    else:
        a_bytes_raw = secrets.token_bytes(128)
    a = big_integer_from_bytes(a_bytes_raw)
    a_pub = pow(g_int, a, n_int)
    if a_pub % n_int == 0:
        raise ValueError("Clé publique du client A invalide (A mod N == 0)")
    a_bytes = big_integer_to_bytes(a_pub)

    # Multiplicateur k = SHA512(pad384(N) || pad384(g))
    n_padded = pad_to_n(big_integer_to_bytes(n_int), 384)
    g_padded = pad_to_n(big_integer_to_bytes(g_int), 384)
    k = big_integer_from_bytes(hashlib.sha512(n_padded + g_padded).digest())

    # Paramètre de brouillage u = SHA512(pad384(A) || pad384(B))
    a_padded = pad_to_n(a_bytes, 384)
    b_padded = pad_to_n(big_integer_to_bytes(b_int), 384)
    u_digest = hashlib.sha512(a_padded + b_padded).digest()
    u = big_integer_from_bytes(u_digest)
    if u == 0:
        raise ValueError("Paramètre de brouillage u invalide (u == 0)")

    # Dérivation mot de passe x = SHA512(s || SHA512("Pair-Setup:" || PIN))
    username = b"Pair-Setup"
    h_i_p = hashlib.sha512(username + b":" + pin_code.encode("utf-8")).digest()
    s_bytes = big_integer_to_bytes(salt_int)
    x = big_integer_from_bytes(hashlib.sha512(s_bytes + h_i_p).digest())

    # Secret partagé S = (B - k * (g^x mod N))^(a + u*x) mod N
    val1 = (b_int - k * pow(g_int, x, n_int)) % n_int
    val2 = a + u * x
    s_int = pow(val1, val2, n_int)
    shared_key_k = hashlib.sha512(big_integer_to_bytes(s_int)).digest()

    # Preuve M1 = SHA512((H(N) ^ H(g)) || H(I) || s || A || B || K)
    hn = hashlib.sha512(big_integer_to_bytes(n_int)).digest()
    hg = hashlib.sha512(big_integer_to_bytes(g_int)).digest()
    h_xor = big_integer_to_bytes(big_integer_from_bytes(hn) ^ big_integer_from_bytes(hg))
    hi = hashlib.sha512(username).digest()

    m1_input = h_xor + hi + s_bytes + a_bytes + big_integer_to_bytes(b_int) + shared_key_k
    m1_hash = hashlib.sha512(m1_input).digest()
    m1_proof = big_integer_to_bytes(big_integer_from_bytes(m1_hash))

    return a_bytes, m1_proof, shared_key_k


def verify_srp_m2(a_bytes: bytes, m1_proof: bytes, k_key: bytes, server_m2_raw: bytes) -> bool:
    """Valide la preuve M2 renvoyée par la télécommande en M4.

    Applique la normalisation grand-boutiste NimbusDS pour tolérer et éliminer
    les éventuels zéros non signés de tête.
    """
    if not server_m2_raw:
        return False
    expected_m2_hash = hashlib.sha512(a_bytes + m1_proof + k_key).digest()
    expected_m2 = big_integer_to_bytes(big_integer_from_bytes(expected_m2_hash))
    server_m2 = big_integer_to_bytes(big_integer_from_bytes(server_m2_raw))
    return expected_m2 == server_m2


def hkdf_sha512(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """Dérive une clé cryptographique symétrique via HKDF-SHA512."""
    return HKDF(
        algorithm=hashes.SHA512(),
        length=length,
        salt=salt,
        info=info,
    ).derive(ikm)


def encrypt_api_payload(key: bytes, plaintext: bytes) -> bytes:
    """Chiffre un payload JSON monolithique avec ChaCha20-Poly1305 et le nonce universel HuMiThFr."""
    chacha = ChaCha20Poly1305(key)
    return chacha.encrypt(API_NONCE, plaintext, None)


def decrypt_api_payload(key: bytes, ciphertext: bytes) -> bytes:
    """Déchiffre un payload monolithique et valide strictement l'intégrité du tag Poly1305 de 16 octets.

    Lève cryptography.exceptions.InvalidTag si le ciphertext est tronqué ou altéré.
    """
    if len(ciphertext) < 16:
        raise InvalidTag("Le ciphertext est trop court pour contenir le tag Poly1305 de 16 octets")
    chacha = ChaCha20Poly1305(key)
    return chacha.decrypt(API_NONCE, ciphertext, None)
