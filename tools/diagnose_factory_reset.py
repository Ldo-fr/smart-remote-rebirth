#!/usr/bin/env python3
"""Diagnostic & local MQTT sniffer toolkit for factory-reset Sevenhugs Smart Remotes (SR1A).

Designed to assist with Issue #17 (older API versions such as X-Version: 21 / X-Min-Version: 17
and factory-reset units where /api/config returns 400 "Configuration does not exist" / "Disconnected").

Features:
  1. Automatic X-Version / X-Min-Version negotiation using AsyncSmartRemoteClient.
  2. Full endpoint audit (/api/hello, /api/config, /api/settings, /api/devices, /api/manifests,
     and unencrypted /api/devices/sevenhugs.control.webhooks/setup).
  3. Optional official APK payload tests (--try-create-config / --try-restore-config).
  4. Built-in pure-Python asyncio MQTT 3.1.1 sniffer/mock broker (--mqtt-sniffer) listening on
     TCP 1883 (and optional TLS 8883 with on-the-fly self-signed cert) to capture config.mqtt
     traffic when local DNS redirects the remote's cloud broker hostname to this machine.

Usage examples:
  # Audit an already-paired remote (keys in tools/smartremote_keys.json):
  python3 tools/diagnose_factory_reset.py --ip 192.168.1.50

  # Pair a factory-reset remote from scratch (auto-negotiates X-Version 21/17) & audit:
  python3 tools/diagnose_factory_reset.py --ip 192.168.1.50 --pair

  # Test POST /api/config with the exact APK HomeInfo payload:
  python3 tools/diagnose_factory_reset.py --ip 192.168.1.50 --try-create-config

  # Run the local MQTT 3.1.1 sniffer on port 1883 (and optional TLS 8883):
  python3 tools/diagnose_factory_reset.py --mqtt-sniffer --mqtt-port 1883 --mqtt-tls-port 8883
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import ssl
import struct
import sys
import tempfile
import uuid
from typing import Any

# Ensure repository root and optional .pylib are importable
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_PYLIB_DIR = os.path.join(_REPO_ROOT, ".pylib")
if os.path.isdir(_PYLIB_DIR) and _PYLIB_DIR not in sys.path:
    sys.path.insert(0, _PYLIB_DIR)

# Hardcoded Dev/Beta fallback key found in APK PushHomePresenterImpl.b() (classes2.dex)
APK_FALLBACK_CONFIG_KEY_HEX = (
    "690a6fca035a83837daf16d22cc67e78dfa8b120c9b418f4c89cf86b7256cfd9"
    "5e7ef1b22cf21451dcd82129ceb0cca674ce4971d8fda96981f0a78f60f820cc"
)


def build_apk_create_config_payload(
    home_name: str = "Home",
    first_name: str = "User",
    last_name: str = "Sevenhugs",
    email: str = "user@sevenhugs.com",
    timezone: str = "UTC",
) -> dict[str, Any]:
    """Build the exact snake_case HomeInfo JSON sent by APK PushHomePresenterImpl.createNewHome()."""
    return {
        "metadata": {
            "name": home_name,
            "lang": "en_US",
        },
        "user": {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
        },
        "smartphone": {
            "model": "Pixel XL",
            "os": "Android",
            "version": "10",
            "public_key": "NO-PUBLIC-KEY",
        },
        "location": {
            "city": None,
            "state": None,
            "country": None,
            "timezone": timezone,
            "pos": None,
        },
        "settings": {
            "time_convention": "24-hour",
            "units": {
                "temperature": "C",
            },
        },
    }


def build_apk_restore_config_payload(
    config_id: str | None = None,
    config_key_hex: str | None = None,
    config_name: str = "Home",
) -> dict[str, str]:
    """Build the ConfigInfo JSON sent by APK PushHomePresenterImpl.restoreConfig() (PUT /api/config).

    The remote's config.mqtt expects a 64-byte Ed25519 secret key encoded as 128 hex characters
    (seed || public_key, standard libsodium/NaCl 64-byte secret key format).
    """
    if not config_key_hex:
        from cryptography.hazmat.primitives.asymmetric import ed25519

        sk = ed25519.Ed25519PrivateKey.generate()
        raw_64 = sk.private_bytes_raw() + sk.public_key().public_bytes_raw()
        config_key_hex = raw_64.hex()
    return {
        "config_id": config_id or str(uuid.uuid4()),
        "config_key": config_key_hex,
        "config_name": config_name,
    }


# ============================================================================
# Pure-Python asyncio MQTT 3.1.1 Sniffer / Mock Broker
# ============================================================================


def _decode_mqtt_remaining_length(data: bytes, offset: int) -> tuple[int, int] | None:
    """Decode MQTT variable-length integer starting at offset. Returns (value, next_offset) or None."""
    multiplier = 1
    value = 0
    pos = offset
    while pos < len(data):
        encoded_byte = data[pos]
        pos += 1
        value += (encoded_byte & 0x7F) * multiplier
        if (encoded_byte & 0x80) == 0:
            return value, pos
        multiplier *= 128
        if multiplier > 128 * 128 * 128:
            raise ValueError("Malformed MQTT Remaining Length")
    return None


def parse_mqtt_packet(packet_type: int, flags: int, payload: bytes) -> tuple[str, bytes | None]:
    """Parse a single MQTT 3.1.1 packet and return (human_summary, optional_response_bytes)."""
    if packet_type == 1:  # CONNECT
        proto_name = ""
        client_id = ""
        pos = 0
        if len(payload) >= 2:
            plen = struct.unpack_from("!H", payload, pos)[0]
            pos += 2
            proto_name = payload[pos : pos + plen].decode("utf-8", errors="replace")
            pos += plen
        proto_level = payload[pos] if pos < len(payload) else 0
        connect_flags = payload[pos + 1] if pos + 1 < len(payload) else 0
        keep_alive = struct.unpack_from("!H", payload, pos + 2)[0] if pos + 4 <= len(payload) else 0
        pos += 4
        if pos + 2 <= len(payload):
            cid_len = struct.unpack_from("!H", payload, pos)[0]
            pos += 2
            client_id = payload[pos : pos + cid_len].decode("utf-8", errors="replace")
            pos += cid_len
        summary = (
            f"CONNECT proto={proto_name!r} level={proto_level} flags=0x{connect_flags:02x} "
            f"keepalive={keep_alive}s client_id={client_id!r} remaining={payload[pos:]!r}"
        )
        # CONNACK: session_present=0, return_code=0 (Connection Accepted)
        return summary, b"\x20\x02\x00\x00"

    if packet_type == 3:  # PUBLISH
        qos = (flags >> 1) & 0x03
        retain = flags & 0x01
        pos = 0
        topic = ""
        if len(payload) >= 2:
            tlen = struct.unpack_from("!H", payload, pos)[0]
            pos += 2
            topic = payload[pos : pos + tlen].decode("utf-8", errors="replace")
            pos += tlen
        packet_id = None
        if qos > 0 and pos + 2 <= len(payload):
            packet_id = struct.unpack_from("!H", payload, pos)[0]
            pos += 2
        body = payload[pos:]
        try:
            body_repr = body.decode("utf-8")
        except UnicodeDecodeError:
            body_repr = f"hex:{body.hex()}"
        summary = f"PUBLISH topic={topic!r} qos={qos} retain={retain} body={body_repr!r}"
        resp = struct.pack("!BBH", 0x40, 2, packet_id) if (qos == 1 and packet_id is not None) else None
        return summary, resp

    if packet_type == 8:  # SUBSCRIBE
        packet_id = struct.unpack_from("!H", payload, 0)[0] if len(payload) >= 2 else 0
        pos = 2
        topics: list[tuple[str, int]] = []
        while pos + 2 <= len(payload):
            tlen = struct.unpack_from("!H", payload, pos)[0]
            pos += 2
            topic = payload[pos : pos + tlen].decode("utf-8", errors="replace")
            pos += tlen
            req_qos = payload[pos] if pos < len(payload) else 0
            pos += 1
            topics.append((topic, req_qos))
        summary = f"SUBSCRIBE packet_id={packet_id} topics={topics}"
        # SUBACK: grant QoS 0 for each requested topic
        suback_payload = struct.pack("!H", packet_id) + bytes([0] * len(topics))
        resp = bytes([0x90, len(suback_payload)]) + suback_payload
        return summary, resp

    if packet_type == 12:  # PINGREQ
        return "PINGREQ", b"\xD0\x00"  # PINGRESP

    if packet_type == 14:  # DISCONNECT
        return "DISCONNECT", None

    return f"TYPE_{packet_type} flags=0x{flags:x} len={len(payload)} hex={payload.hex()}", None


def _build_self_signed_ssl_context(common_name: str = "*.sevenhugs.com") -> ssl.SSLContext:
    """Generate an ephemeral self-signed TLS server context for testing port 8883."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("*.sevenhugs.com"),
                x509.DNSName("*.cobra.sevenhugs.com"),
                x509.DNSName("*.prod.cobra.sevenhugs.com"),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with tempfile.NamedTemporaryFile(delete=False) as cf, tempfile.NamedTemporaryFile(delete=False) as kf:
        cf.write(cert_pem)
        kf.write(key_pem)
        cert_path, key_path = cf.name, kf.name
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        return ctx
    finally:
        for p in (cert_path, key_path):
            try:
                os.unlink(p)
            except OSError:
                pass


async def _handle_mqtt_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    label: str,
) -> None:
    peer = writer.get_extra_info("peername")
    print(f"[MQTT {label}] Client connected from {peer}", flush=True)
    buf = bytearray()
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            buf.extend(chunk)
            while len(buf) >= 2:
                first_byte = buf[0]
                pkt_type = (first_byte >> 4) & 0x0F
                flags = first_byte & 0x0F
                decoded = _decode_mqtt_remaining_length(bytes(buf), 1)
                if decoded is None:
                    break
                rem_len, header_end = decoded
                if len(buf) < header_end + rem_len:
                    break
                payload = bytes(buf[header_end : header_end + rem_len])
                del buf[: header_end + rem_len]

                summary, resp = parse_mqtt_packet(pkt_type, flags, payload)
                print(f"[MQTT {label}] {peer} -> {summary}", flush=True)
                if resp:
                    writer.write(resp)
                    await writer.drain()
    except Exception as err:
        print(f"[MQTT {label}] {peer} connection error: {err}", flush=True)
    finally:
        print(f"[MQTT {label}] Client disconnected: {peer}", flush=True)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def run_mqtt_sniffer(host: str, port: int, tls_port: int | None) -> None:
    """Start the local MQTT 3.1.1 sniffer on plain TCP (and optional TLS)."""
    servers: list[asyncio.Server] = []
    plain_srv = await asyncio.start_server(
        lambda r, w: _handle_mqtt_client(r, w, f"TCP:{port}"),
        host,
        port,
    )
    servers.append(plain_srv)
    print(f"[*] MQTT 3.1.1 plain TCP sniffer listening on {host}:{port}", flush=True)

    if tls_port:
        ssl_ctx = _build_self_signed_ssl_context()
        tls_srv = await asyncio.start_server(
            lambda r, w: _handle_mqtt_client(r, w, f"TLS:{tls_port}"),
            host,
            tls_port,
            ssl=ssl_ctx,
        )
        servers.append(tls_srv)
        print(
            f"[*] MQTT 3.1.1 TLS sniffer (self-signed *.sevenhugs.com) listening on {host}:{tls_port}",
            flush=True,
        )

    print("[*] Waiting for Smart Remote connections... (Ctrl+C to stop)", flush=True)
    try:
        await asyncio.gather(*(s.serve_forever() for s in servers))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for s in servers:
            s.close()
            await s.wait_closed()


# ============================================================================
# Remote HTTP / API Diagnostic Flow
# ============================================================================


async def run_remote_diagnostic(args: argparse.Namespace) -> int:
    """Probe, pair/verify, and audit a Smart Remote SR1A unit."""
    try:
        import aiohttp
    except ImportError:
        print("ERROR: aiohttp is required for --ip remote diagnostics (pip install aiohttp).", file=sys.stderr)
        return 1

    from custom_components.smart_remote_rebirth.client import (
        AsyncSmartRemoteClient,
        SmartRemoteError,
    )

    target = f"{args.ip}:{args.port}" if args.port != 80 else args.ip
    keys_path = args.keys
    keys: dict[str, Any] = {}
    if not args.pair and os.path.isfile(keys_path):
        with open(keys_path, encoding="utf-8") as fh:
            keys = json.load(fh)
        print(f"[*] Loaded keys from {keys_path}")

    async with aiohttp.ClientSession() as session:
        # 1. Unencrypted probe on /api/hello
        print(f"\n=== 1. Probing http://{target}/api/hello ===")
        try:
            async with session.get(
                f"http://{target}/api/hello",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                print(f"  Status: {resp.status}")
                print(f"  X-Version: {resp.headers.get('X-Version')}")
                print(f"  X-Min-Version: {resp.headers.get('X-Min-Version')}")
                body_preview = (await resp.read())[:200]
                print(f"  Body: {body_preview!r}")
        except Exception as err:
            print(f"  Probe error: {err}")

        # 2. Unencrypted Lua setup check (tests whether setup.lua works without /api/config)
        print(f"\n=== 2. Testing unencrypted Lua setup endpoint ===")
        try:
            async with session.get(
                f"http://{target}/api/devices/sevenhugs.control.webhooks/setup",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                print(f"  GET /api/devices/sevenhugs.control.webhooks/setup -> HTTP {resp.status}")
                print(f"  Set-Cookie: {resp.headers.get('Set-Cookie')}")
                print(f"  WWW-Authenticate: {resp.headers.get('WWW-Authenticate')}")
        except Exception as err:
            print(f"  Lua setup GET error: {err}")

        # 3. Pair-Setup (if requested) + Pair-Verify with dynamic X-Version negotiation
        client = AsyncSmartRemoteClient(host=target, session=session, keys=keys)
        if args.pair or not keys.get("client_private_key"):
            print("\n=== 3. Running Pair-Setup with dynamic X-Version negotiation ===")
            print("  Sending M1 (remote takes ~16s to compute SRP-6a 3072-bit)...")
            salt, b_bytes = await client.async_pair_setup_m1()
            print(
                f"  M2 received! Negotiated headers: X-Version={client.api_version}, "
                f"X-Min-Version={client.min_api_version}"
            )
            pin = input("  Enter 8-digit PIN shown on remote (XXX-XX-XXX): ").strip()
            remote_id, _ = await client.async_pair_setup_finish(pin, salt, b_bytes)
            print(f"  Pair-Setup complete! remote_id={remote_id}")
            with open(keys_path, "w", encoding="utf-8") as fh:
                json.dump(client._keys, fh, indent=2)
            print(f"  Saved keys to {keys_path}")

        print("\n=== 4. Running Pair-Verify ===")
        await client.async_pair_verify()
        print(
            f"  Pair-Verify OK! Active headers: X-Version={client.api_version}, "
            f"X-Min-Version={client.min_api_version}"
        )

        # 5. Audit core encrypted endpoints
        print("\n=== 5. Auditing encrypted /api/* endpoints ===")
        for path in ("/api/config", "/api/settings", "/api/devices", "/api/manifests"):
            try:
                res = await client.async_request("GET", path)
                preview = json.dumps(res)[:240]
                print(f"  GET {path} -> OK: {preview}")
            except SmartRemoteError as err:
                print(f"  GET {path} -> ERROR: {err}")

        # 6. Optional POST /api/config test (APK HomeInfo payload)
        if args.try_create_config:
            print("\n=== 6. Testing POST /api/config (exact APK HomeInfo snake_case payload) ===")
            payload = build_apk_create_config_payload()
            print(f"  Payload: {json.dumps(payload)}")
            try:
                res = await client.async_request("POST", "/api/config", json_data=payload)
                print(f"  POST /api/config -> SUCCESS: {json.dumps(res)}")
            except SmartRemoteError as err:
                print(f"  POST /api/config -> ERROR: {err}")

        # 7. Optional PUT /api/config test (APK ConfigInfo payload)
        if args.try_restore_config:
            print("\n=== 7. Testing PUT /api/config (APK ConfigInfo with 64-byte Ed25519 key) ===")
            payload = build_apk_restore_config_payload(
                config_key_hex=APK_FALLBACK_CONFIG_KEY_HEX if args.use_apk_dev_key else None
            )
            print(f"  Payload: {json.dumps(payload)}")
            try:
                res = await client.async_request("PUT", "/api/config", json_data=payload)
                print(f"  PUT /api/config -> SUCCESS: {json.dumps(res)}")
            except SmartRemoteError as err:
                print(f"  PUT /api/config -> ERROR: {err}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnostic & MQTT sniffer tool for factory-reset Sevenhugs Smart Remotes (SR1A)."
    )
    parser.add_argument("--ip", help="Local IP address of the Smart Remote (e.g. 192.168.1.50)")
    parser.add_argument("--port", type=int, default=80, help="HTTP port of the remote (default: 80)")
    parser.add_argument(
        "--keys",
        default=os.path.join(_REPO_ROOT, "tools", "smartremote_keys.json"),
        help="Path to keys JSON file (default: tools/smartremote_keys.json)",
    )
    parser.add_argument("--pair", action="store_true", help="Run interactive Pair-Setup (PIN)")
    parser.add_argument(
        "--try-create-config",
        action="store_true",
        help="Test POST /api/config with the exact APK HomeInfo payload",
    )
    parser.add_argument(
        "--try-restore-config",
        action="store_true",
        help="Test PUT /api/config with a valid 64-byte Ed25519 ConfigInfo payload",
    )
    parser.add_argument(
        "--use-apk-dev-key",
        action="store_true",
        help="Use the hardcoded Dev/Beta 64-byte Ed25519 key from APK PushHomePresenterImpl.b()",
    )
    parser.add_argument(
        "--mqtt-sniffer",
        action="store_true",
        help="Run the pure-Python local MQTT 3.1.1 sniffer/mock broker",
    )
    parser.add_argument("--mqtt-host", default="0.0.0.0", help="Bind address for MQTT sniffer (default: 0.0.0.0)")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="Plain TCP port for MQTT sniffer (default: 1883)")
    parser.add_argument(
        "--mqtt-tls-port",
        type=int,
        default=None,
        help="Optional TLS port for MQTT sniffer with self-signed *.sevenhugs.com cert (e.g. 8883)",
    )

    args = parser.parse_args()
    if args.mqtt_sniffer:
        asyncio.run(run_mqtt_sniffer(args.mqtt_host, args.mqtt_port, args.mqtt_tls_port))
        return 0
    if not args.ip:
        parser.error("Provide --ip <REMOTE_IP> for remote diagnostics, or --mqtt-sniffer for MQTT capture.")
    return asyncio.run(run_remote_diagnostic(args))


if __name__ == "__main__":
    sys.exit(main())
