#!/usr/bin/env python3
"""
Sevenhugs Smart Remote (SR1A) - Jumelage initial complet (Pair-Setup SRP-6a + Pair-Verify)
==========================================================================================
Génère une identité de contrôleur Ed25519, effectue le jumelage SRP-6a 3072 bits avec le code
PIN affiché sur la télécommande, valide la session Pair-Verify et sauvegarde les clés dans
smartremote_keys.json.

Usage :
  python3 pair_and_verify.py --ip 192.168.1.XX [--keys smartremote_keys.json]
"""

import argparse
import hashlib
import http.client
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
PYLIB_DIR = os.path.join(PROJECT_ROOT, ".pylib")
if os.path.isdir(PYLIB_DIR) and PYLIB_DIR not in sys.path:
    sys.path.insert(0, PYLIB_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DEFAULT_KEYS_FILE = os.path.join(PROJECT_ROOT, "smartremote_keys.json")
if not os.path.exists(DEFAULT_KEYS_FILE) and os.path.exists(os.path.join(BASE_DIR, "smartremote_keys.json")):
    DEFAULT_KEYS_FILE = os.path.join(BASE_DIR, "smartremote_keys.json")

# Paramètres du groupe SRP-6a 3072-bit (RFC 5054)
N_STR = (
    "5809605995369958062791915965639201402176612226902900533702900882779736177890990861472094774477339581147373410185646"
    "37832804372980075047009821092448786693505916437158816804754094398164451663275506750162643455639819318662899007124866"
    "08193612051197936939854332970361182329144101718768075364573912778570118498974102075191053333558011211093568974594262"
    "71845471397952675959440793493071628394122780510124618488232602464649876850458861245784240929258426287699705312584509"
    "62541951346360515542801716571446536309402160929056108402589366256122257320208286579782186527099114508220065697817719"
    "28270245389902399691755461907706456858934380117144304264093386763147435711545371420315730042764287014330363818017053"
    "08659830751190352946025482059931306571004727362479688415574702596946457770284148435989129632853918392117997472632693"
    "078113129886487399347796982772784615865232621289656944284216824611318709764535152507354116344703769998514148343807"
)
N = int(N_STR)
g = 5


def sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def big_integer_to_bytes(val: int) -> bytes:
    length = (val.bit_length() + 8) // 8
    b = val.to_bytes(length, byteorder="big", signed=True)
    if len(b) > 0 and b[0] == 0:
        b = b[1:]
    return b


def big_integer_from_bytes(b: bytes) -> int:
    return int.from_bytes(b, byteorder="big")


def pad_to_n(b: bytes, target_len: int = 384) -> bytes:
    if len(b) < target_len:
        return b.rjust(target_len, b"\x00")
    return b


def encode_tlv(items: list) -> bytes:
    out = bytearray()
    for tag, val in items:
        if isinstance(val, int):
            val = bytes([val])
        elif isinstance(val, str):
            val = val.encode("utf-8")
        pos = 0
        while pos < len(val):
            chunk = val[pos : pos + 255]
            out.append(tag)
            out.append(len(chunk))
            out.extend(chunk)
            pos += len(chunk)
    return bytes(out)


def decode_tlv(data: bytes) -> dict:
    pos = 0
    res = {}
    while pos < len(data):
        tag = data[pos]
        length = data[pos + 1]
        val = data[pos + 2 : pos + 2 + length]
        res[tag] = res.get(tag, b"") + val
        pos += 2 + length
    return res


def post_tlv(url: str, tlv_bytes: bytes, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(
        url,
        data=tlv_bytes,
        headers={
            "Content-Type": "application/pairing+tlv8",
            "X-Version": "31",
            "X-Min-Version": "24",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def run_pair_and_verify(target_ip: str, keys_file: str = DEFAULT_KEYS_FILE):
    print("=" * 65)
    print("   SEVENHUGS SMART REMOTE - PAIR-SETUP & PAIR-VERIFY")
    print(f"   Target IP : {target_ip}")
    print("=" * 65)

    controller_id = str(uuid.uuid4())
    client_sk = ed25519.Ed25519PrivateKey.generate()
    client_pk = client_sk.public_key().public_bytes_raw()

    init_keys = {
        "controller_id": controller_id,
        "client_private_key": client_sk.private_bytes_raw().hex(),
        "client_public_key": client_pk.hex(),
        "ip": target_ip,
    }
    with open(keys_file, "w", encoding="utf-8") as f:
        json.dump(init_keys, f, indent=2)

    print(f"\n[*] Identité du contrôleur générée :")
    print(f"    - Controller UUID : {controller_id}")
    print(f"    - Ed25519 PK      : {client_pk.hex()}")

    print("\n" + "=" * 50)
    print(" >>> ÉTAPE 1 : DÉMARRAGE DU PAIR-SETUP (SRP-6a) <<<")
    print("=" * 50)

    m1_tlv = encode_tlv([(0x00, 1), (0x06, 1)])
    print("[*] Envoi de M1 à /pair-setup...")
    print("[!] Calcul SRP-6a 3072-bit en cours sur la télécommande (~16 secondes)...")
    print("[!] REGARDEZ L'ÉCRAN DE LA TÉLÉCOMMANDE !")

    m2_raw = post_tlv(f"http://{target_ip}/pair-setup", m1_tlv, timeout=60.0)
    m2_tlv = decode_tlv(m2_raw)
    salt_bytes = m2_tlv[0x02]
    B_bytes = m2_tlv[0x03]
    salt_int = big_integer_from_bytes(salt_bytes)
    B_int = big_integer_from_bytes(B_bytes)
    print(f"[+] M2 reçu avec succès ! Salt: {salt_bytes.hex()[:16]}...")

    print("\n" + "=" * 50)
    print(" >>> REGARDEZ L'ÉCRAN DE VOTRE SMART REMOTE MAINTENANT ! <<<")
    print("=" * 50 + "\n")

    pin_code = input(
        "Entrez le code de sécurité affiché sur la télécommande (format XXX-XX-XXX) : "
    ).strip()

    a_bytes = secrets.token_bytes(128)
    a = big_integer_from_bytes(a_bytes)
    A = pow(g, a, N)
    A_bytes = big_integer_to_bytes(A)

    N_padded = pad_to_n(big_integer_to_bytes(N), 384)
    g_padded = pad_to_n(big_integer_to_bytes(g), 384)
    k = big_integer_from_bytes(sha512(N_padded + g_padded))

    A_padded = pad_to_n(A_bytes, 384)
    B_padded = pad_to_n(big_integer_to_bytes(B_int), 384)
    u = big_integer_from_bytes(sha512(A_padded + B_padded))

    username = b"Pair-Setup"
    h_I_P = sha512(username + b":" + pin_code.encode("utf-8"))
    s_bytes = big_integer_to_bytes(salt_int)
    x = big_integer_from_bytes(sha512(s_bytes + h_I_P))

    val1 = (B_int - k * pow(g, x, N)) % N
    val2 = a + u * x
    S = pow(val1, val2, N)
    K = sha512(big_integer_to_bytes(S))

    hN = sha512(big_integer_to_bytes(N))
    hN_int = big_integer_from_bytes(hN)
    hg = sha512(big_integer_to_bytes(g))
    hg_int = big_integer_from_bytes(hg)
    h_xor = big_integer_to_bytes(hN_int ^ hg_int)
    hI = sha512(username)

    m1_digest_input = h_xor + hI + s_bytes + A_bytes + big_integer_to_bytes(B_int) + K
    M1_proof = big_integer_to_bytes(big_integer_from_bytes(sha512(m1_digest_input)))
    expected_M2 = big_integer_to_bytes(
        big_integer_from_bytes(sha512(A_bytes + M1_proof + K))
    )

    print("[*] Envoi de la vérification M3...")
    m3_tlv = encode_tlv([(0x06, 3), (0x04, M1_proof), (0x03, A_bytes)])
    m4_raw = post_tlv(f"http://{target_ip}/pair-setup", m3_tlv)
    m4_tlv = decode_tlv(m4_raw)
    server_M2 = big_integer_to_bytes(big_integer_from_bytes(m4_tlv.get(0x04)))
    if server_M2 != expected_M2:
        raise RuntimeError("Preuve du serveur M2 invalide !")
    print("[+] Preuve M4 validée avec succès par la télécommande !")

    session_key = HKDF(
        algorithm=hashes.SHA512(),
        length=32,
        salt=b"Pair-Setup-Encrypt-Salt",
        info=b"Pair-Setup-Encrypt-Info",
    ).derive(K)

    controller_sign_key = HKDF(
        algorithm=hashes.SHA512(),
        length=32,
        salt=b"Pair-Setup-Controller-Sign-Salt",
        info=b"Pair-Setup-Controller-Sign-Info",
    ).derive(K)

    msg_to_sign = controller_sign_key + controller_id.encode("utf-8") + client_pk
    signature = client_sk.sign(msg_to_sign)

    sub_tlv_m5 = encode_tlv(
        [(0x03, client_pk), (0x01, controller_id), (0x0A, signature)]
    )
    nonce_m5 = b"\x00\x00\x00\x00PS-Msg05"
    chacha_setup = ChaCha20Poly1305(session_key)
    ciphertext_m5 = chacha_setup.encrypt(nonce_m5, sub_tlv_m5, None)

    print("[*] Envoi de l'identité du contrôleur (M5)...")
    m5_tlv = encode_tlv([(0x06, 5), (0x05, ciphertext_m5)])
    m6_raw = post_tlv(f"http://{target_ip}/pair-setup", m5_tlv)
    m6_tlv = decode_tlv(m6_raw)

    nonce_m6 = b"\x00\x00\x00\x00PS-Msg06"
    decrypted_m6 = chacha_setup.decrypt(nonce_m6, m6_tlv[0x05], None)
    sub_m6 = decode_tlv(decrypted_m6)

    remote_id = sub_m6[0x01].decode("utf-8", errors="ignore")
    remote_pk = sub_m6[0x03]

    print("[✓] PAIR-SETUP RÉUSSI !")
    print(f"    - Remote Identifier : {remote_id}")
    print(f"    - Remote Public Key : {remote_pk.hex()}")

    print("\n" + "=" * 50)
    print(" >>> ÉTAPE 2 : VÉRIFICATION DE SESSION (PAIR-VERIFY) <<<")
    print("=" * 50)

    cid_bytes = controller_id.encode("utf-8")
    conn = http.client.HTTPConnection(target_ip, 80, timeout=15)
    pv_headers = {
        "Content-Type": "application/pairing+tlv8",
        "X-UUID": controller_id,
        "X-Version": "31",
        "X-Min-Version": "24",
        "Connection": "Keep-Alive",
        "User-Agent": "okhttp/3.12.0",
    }

    client_eph_sk = x25519.X25519PrivateKey.generate()
    client_eph_pk = client_eph_sk.public_key().public_bytes_raw()
    m1_data = encode_tlv([(0x03, client_eph_pk), (0x06, b"\x01")])

    conn.request("POST", "/pair-verify", body=m1_data, headers=pv_headers)
    resp = conn.getresponse()
    m2_raw = resp.read()
    m2 = decode_tlv(m2_raw)
    remote_eph_pk = m2[0x03]
    shared_secret = client_eph_sk.exchange(
        x25519.X25519PublicKey.from_public_bytes(remote_eph_pk)
    )

    verify_key = HKDF(
        algorithm=hashes.SHA512(),
        length=32,
        salt=b"Pair-Verify-Encrypt-Salt",
        info=b"Pair-Verify-Encrypt-Info",
    ).derive(shared_secret)

    to_sign = client_eph_pk + cid_bytes + remote_eph_pk
    client_sig = client_sk.sign(to_sign)
    sub_m3 = encode_tlv([(0x0A, client_sig), (0x01, cid_bytes)])

    cipher = ChaCha20Poly1305(verify_key)
    enc_m3 = cipher.encrypt(b"\x00\x00\x00\x00PV-Msg03", sub_m3, None)
    m3_data = encode_tlv([(0x05, enc_m3), (0x06, b"\x03")])

    conn.request("POST", "/pair-verify", body=m3_data, headers=pv_headers)
    resp3 = conn.getresponse()
    m4_raw = resp3.read()
    conn.close()

    m4 = decode_tlv(m4_raw)
    if resp3.status != 200 or m4.get(0x06) != b"\x04" or 0x07 in m4:
        raise RuntimeError(f"Échec Pair-Verify M3 ({resp3.status}): {m4}")

    print("[✓] PAIR-VERIFY RÉUSSI (HTTP 200, State=M4) !")
    write_key = HKDF(
        algorithm=hashes.SHA512(),
        length=32,
        salt=b"Control-Salt",
        info=b"Control-Write-Encryption-Key",
    ).derive(shared_secret)
    read_key = HKDF(
        algorithm=hashes.SHA512(),
        length=32,
        salt=b"Control-Salt",
        info=b"Control-Read-Encryption-Key",
    ).derive(shared_secret)

    solved_keys = {
        "controller_id": controller_id,
        "client_private_key": client_sk.private_bytes_raw().hex(),
        "client_public_key": client_pk.hex(),
        "remote_id": remote_id,
        "remote_public_key": remote_pk.hex(),
        "shared_secret": shared_secret.hex(),
        "session_write_key": write_key.hex(),
        "session_read_key": read_key.hex(),
        "ip": target_ip,
    }

    with open(keys_file, "w", encoding="utf-8") as f:
        json.dump(solved_keys, f, indent=2)
    print(f"[+] Clés enregistrées dans : {keys_file}")
    return solved_keys


def main():
    parser = argparse.ArgumentParser(
        description="Jumelage initial (Pair-Setup + Pair-Verify) pour Sevenhugs Smart Remote"
    )
    parser.add_argument(
        "--ip",
        required=True,
        help="Adresse IP locale de la Smart Remote (ex: 192.168.1.50)",
    )
    parser.add_argument(
        "--keys",
        default=DEFAULT_KEYS_FILE,
        help="Chemin du fichier JSON où sauvegarder les clés (défaut: smartremote_keys.json)",
    )
    args = parser.parse_args()
    run_pair_and_verify(args.ip, args.keys)


if __name__ == "__main__":
    main()
