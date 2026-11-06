#!/usr/bin/env python3
"""
Sevenhugs Smart Remote - Pair-Verify & Dump de l'API REST chiffrée
==================================================================
Établit une session Pair-Verify à partir d'un fichier smartremote_keys.json existant
(sans code PIN) et sauvegarde l'état complet des routes GET principales dans
remote_state_dump.json.

Usage :
  python3 pair_verify.py [--keys smartremote_keys.json] [--ip 192.168.X.X]
"""
import argparse
import http.client
import json
import os
import sys

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


def encode_tlv(items):
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


def decode_tlv(data):
    pos = 0
    res = {}
    while pos < len(data):
        tag = data[pos]
        length = data[pos + 1]
        val = data[pos + 2 : pos + 2 + length]
        res[tag] = res.get(tag, b"") + val
        pos += 2 + length
    return res


def main():
    parser = argparse.ArgumentParser(
        description="Vérification de session Pair-Verify et dump de l'état de la Smart Remote"
    )
    parser.add_argument(
        "--keys",
        default=DEFAULT_KEYS_FILE,
        help="Chemin vers smartremote_keys.json",
    )
    parser.add_argument(
        "--ip",
        default=None,
        help="Surcharger l'adresse IP de la télécommande",
    )
    parser.add_argument(
        "--out",
        default=os.path.join(BASE_DIR, "remote_state_dump.json"),
        help="Fichier de sortie JSON",
    )
    args = parser.parse_args()

    with open(args.keys, "r", encoding="utf-8") as f:
        keys = json.load(f)

    target_ip = args.ip or keys["ip"]
    controller_id = keys["controller_id"]
    client_sk = ed25519.Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(keys["client_private_key"])
    )
    remote_pk = bytes.fromhex(keys["remote_public_key"])

    conn = http.client.HTTPConnection(target_ip, 80, timeout=15)
    client_eph_sk = x25519.X25519PrivateKey.generate()
    client_eph_pk = client_eph_sk.public_key().public_bytes_raw()
    m1_tlv = encode_tlv([(0x03, client_eph_pk), (0x06, 1)])

    headers = {
        "Content-Type": "application/pairing+tlv8",
        "X-UUID": controller_id,
        "X-Version": "31",
        "X-Min-Version": "24",
        "Connection": "Keep-Alive",
        "User-Agent": "okhttp/3.12.0",
    }

    print(f"[*] Envoi de M1 à /pair-verify ({target_ip})...")
    conn.request("POST", "/pair-verify", body=m1_tlv, headers=headers)
    resp1 = conn.getresponse()
    m2_raw = resp1.read()
    if resp1.status != 200:
        raise RuntimeError(f"M1 failed with HTTP {resp1.status}: {decode_tlv(m2_raw)}")

    m2 = decode_tlv(m2_raw)
    remote_eph_pk = m2[0x03]
    enc_m2 = m2[0x05]

    shared_secret = client_eph_sk.exchange(
        x25519.X25519PublicKey.from_public_bytes(remote_eph_pk)
    )
    verify_key = HKDF(
        algorithm=hashes.SHA512(),
        length=32,
        salt=b"Pair-Verify-Encrypt-Salt",
        info=b"Pair-Verify-Encrypt-Info",
    ).derive(shared_secret)

    cipher = ChaCha20Poly1305(verify_key)
    dec_m2 = decode_tlv(cipher.decrypt(b"\x00\x00\x00\x00PV-Msg02", enc_m2, None))
    remote_id_m2 = dec_m2[0x01]
    remote_sig = dec_m2[0x0A]

    ed25519.Ed25519PublicKey.from_public_bytes(remote_pk).verify(
        remote_sig, remote_eph_pk + remote_id_m2 + client_eph_pk
    )
    print(
        f"[+] M2 déchiffré et signature Ed25519 vérifiée ! Remote ID: {remote_id_m2.decode()}"
    )

    cid_bytes = controller_id.encode("utf-8")
    to_sign = client_eph_pk + cid_bytes + remote_eph_pk
    sig = client_sk.sign(to_sign)

    sub_m3 = encode_tlv([(0x0A, sig), (0x01, cid_bytes)])
    enc_m3 = cipher.encrypt(b"\x00\x00\x00\x00PV-Msg03", sub_m3, None)
    m3_tlv = encode_tlv([(0x05, enc_m3), (0x06, 3)])

    print("[*] Envoi de M3 à /pair-verify sur la même connexion TCP...")
    conn.request("POST", "/pair-verify", body=m3_tlv, headers=headers)
    resp3 = conn.getresponse()
    m4_raw = resp3.read()
    m4 = decode_tlv(m4_raw)
    if resp3.status != 200 or m4.get(0x06) != b"\x04" or 0x07 in m4:
        raise RuntimeError(f"M3 failed with HTTP {resp3.status}: {m4}")

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

    keys["ip"] = target_ip
    keys["shared_secret"] = shared_secret.hex()
    keys["session_write_key"] = write_key.hex()
    keys["session_read_key"] = read_key.hex()
    with open(args.keys, "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=2)
    print(f"[+] Clés de session enregistrées dans {args.keys}")

    api_nonce = b"\x00\x00\x00\x00HuMiThFr"
    read_cipher = ChaCha20Poly1305(read_key)

    endpoints = [
        "/api/config",
        "/api/devices",
        "/api/settings",
        "/api/custom/widgets",
        "/api/av-scenes",
        "/api/home-scenes",
        "/api/components-groups",
        "/api/manifests",
        "/api/homekit/pairing",
    ]

    api_headers = {
        "X-UUID": controller_id,
        "X-Version": "31",
        "X-Min-Version": "24",
        "Connection": "Keep-Alive",
        "User-Agent": "okhttp/3.12.0",
    }

    dump_data = {}
    for ep in endpoints:
        print(f"\n[*] GET {ep} ...")
        conn.request("GET", ep, headers=api_headers)
        r = conn.getresponse()
        raw_body = r.read()
        print(f"    Status: {r.status} | Encrypted bytes: {len(raw_body)}")
        if len(raw_body) >= 16:
            try:
                dec = read_cipher.decrypt(api_nonce, raw_body, None)
                text = dec.decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(text)
                    dump_data[ep] = parsed
                except Exception:
                    dump_data[ep] = text
            except Exception as e:
                print(f"    Decryption error: {e}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(dump_data, f, indent=2, ensure_ascii=False)
    print(f"\n[✓] État complet sauvegardé dans {args.out}")
    conn.close()


if __name__ == "__main__":
    main()
