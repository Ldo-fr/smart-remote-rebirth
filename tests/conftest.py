"""Configuration du banc d'essais et fixtures pour Sevenhugs Smart Remote (Phase 1).

Fournit les fixtures d'isolation, l'émulateur asynchrone Keep-Alive (MockSmartRemoteServer),
et garantit la compatibilité complète avec pytest et l'Event Loop asyncio.
"""

from __future__ import annotations

import asyncio
import email.parser
import hashlib
import json
import os
import secrets
import sys
import types
from typing import Any, Callable

# 1. Configuration des chemins d'importation
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYLIB_DIR = os.path.join(PROJECT_ROOT, ".pylib")

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if os.path.isdir(PYLIB_DIR) and PYLIB_DIR not in sys.path:
    sys.path.insert(0, PYLIB_DIR)

# 2. Fourniture d'un module pytest compatible si non installé sur l'hôte
try:
    import pytest
except ImportError:
    class _RaisesContext:
        def __init__(self, expected_exc: type[Exception] | tuple[type[Exception], ...], match: str | None = None) -> None:
            self.expected_exc = expected_exc
            self.match = match
            self.value: Exception | None = None

        def __enter__(self) -> _RaisesContext:
            return self

        def __exit__(self, exc_type: type[Exception] | None, exc_val: Exception | None, exc_tb: Any) -> bool:
            if exc_type is None:
                exc_name = (
                    tuple(e.__name__ for e in self.expected_exc)
                    if isinstance(self.expected_exc, tuple)
                    else getattr(self.expected_exc, "__name__", str(self.expected_exc))
                )
                raise AssertionError(f"Exception {exc_name} attendue mais non levée")
            if not issubclass(exc_type, self.expected_exc):
                return False
            self.value = exc_val
            if self.match is not None and exc_val is not None:
                if self.match not in str(exc_val):
                    raise AssertionError(f"Le message '{exc_val}' ne contient pas le motif '{self.match}'")
            return True

    def _raises(expected_exc: type[Exception], *args: Any, **kwargs: Any) -> Any:
        match_str = kwargs.get("match")
        if args and callable(args[0]):
            fn = args[0]
            try:
                fn(*args[1:], **kwargs)
            except expected_exc as e:
                return e
            raise AssertionError(f"Exception {expected_exc.__name__} attendue mais non levée")
        return _RaisesContext(expected_exc, match=match_str)

    def _fixture(*args: Any, **kwargs: Any) -> Callable:
        def decorator(fn: Callable) -> Callable:
            fn.__pytest_fixture__ = True
            return fn
        if args and callable(args[0]):
            return decorator(args[0])
        return decorator

    class _Mark:
        def asyncio(self, fn: Callable) -> Callable:
            fn.__pytest_asyncio__ = True
            return fn

        def parametrize(self, argnames: str | list[str], argvalues: list[Any]) -> Callable:
            def decorator(fn: Callable) -> Callable:
                fn.__pytest_parametrize__ = (argnames, argvalues)
                return fn
            return decorator

    pytest_shim = types.ModuleType("pytest")
    pytest_shim.raises = _raises
    pytest_shim.fixture = _fixture
    pytest_shim.mark = _Mark()
    pytest_shim.fail = lambda msg="": (_ for _ in ()).throw(AssertionError(msg))
    pytest_shim.skip = lambda msg="": (_ for _ in ()).throw(Exception(f"Skipped: {msg}"))
    sys.modules["pytest"] = pytest_shim
    pytest = pytest_shim

# 3. Fourniture d'un aiohttp compatible Keep-Alive si non installé sur l'hôte
try:
    import aiohttp
    import aiohttp.web
except ImportError:
    class ClientError(Exception):
        pass

    class ClientConnectionError(ClientError):
        pass

    class ClientPayloadError(ClientError):
        pass

    class ClientTimeout:
        def __init__(self, total: float | None = None) -> None:
            self.total = total

    class TCPConnector:
        def __init__(
            self,
            force_close: bool = False,
            limit: int = 1,
            limit_per_host: int = 1,
            enable_cleanup_closed: bool = True,
            keepalive_timeout: float = 60.0,
        ) -> None:
            self.force_close = force_close
            self.limit = limit
            self.limit_per_host = limit_per_host
            self.enable_cleanup_closed = enable_cleanup_closed
            self.keepalive_timeout = keepalive_timeout
            self.active_connections: dict[tuple[str, int], tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}

        async def get_connection(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            key = (host, port)
            if not self.force_close and key in self.active_connections:
                r, w = self.active_connections[key]
                if not w.is_closing():
                    return r, w

            r, w = await asyncio.open_connection(host, port)
            if not self.force_close:
                self.active_connections[key] = (r, w)
            return r, w

        def release_connection(self, host: str, port: int, close: bool = False) -> None:
            key = (host, port)
            if close and key in self.active_connections:
                r, w = self.active_connections.pop(key)
                try:
                    w.close()
                    loop = asyncio.get_running_loop()
                    if loop.is_running():
                        loop.create_task(w.wait_closed())
                except Exception:
                    pass

        def close(self) -> None:
            for r, w in list(self.active_connections.values()):
                try:
                    w.close()
                    loop = asyncio.get_running_loop()
                    if loop.is_running():
                        loop.create_task(w.wait_closed())
                except Exception:
                    pass
            self.active_connections.clear()

    class _ClientResponseContext:
        def __init__(self, coro: Any) -> None:
            self.coro = coro
            self.response: Any = None

        async def __aenter__(self) -> Any:
            self.response = await self.coro
            return self.response

        async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            pass

    class FakeClientResponse:
        def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
            self.status = status
            self.headers = headers
            self._body = body
            self.cookies = {}
            if "Set-Cookie" in headers:
                cookie_val = headers["Set-Cookie"].split(";")[0]
                if "=" in cookie_val:
                    cname, cval = cookie_val.split("=", 1)
                    obj = types.SimpleNamespace()
                    obj.value = cval
                    self.cookies[cname] = obj

        async def read(self) -> bytes:
            return self._body

        async def text(self) -> str:
            return self._body.decode("utf-8", errors="replace")

        async def json(self) -> Any:
            return json.loads(self._body.decode("utf-8"))

    class ClientSession:
        def __init__(self, connector: TCPConnector | None = None) -> None:
            self.connector = connector or TCPConnector()
            self._closed = False

        async def close(self) -> None:
            self._closed = True
            self.connector.close()

        def request(
            self,
            method: str,
            url: str,
            data: bytes | str | None = None,
            headers: dict[str, str] | None = None,
            timeout: Any = None,
        ) -> _ClientResponseContext:
            return _ClientResponseContext(self._async_request(method, url, data, headers, timeout))

        def get(self, url: str, headers: dict[str, str] | None = None, timeout: Any = None) -> _ClientResponseContext:
            return self.request("GET", url, headers=headers, timeout=timeout)

        def post(
            self,
            url: str,
            data: bytes | str | None = None,
            headers: dict[str, str] | None = None,
            timeout: Any = None,
        ) -> _ClientResponseContext:
            return self.request("POST", url, data=data, headers=headers, timeout=timeout)

        async def _async_request(
            self,
            method: str,
            url: str,
            data: bytes | str | None = None,
            headers: dict[str, str] | None = None,
            timeout: Any = None,
        ) -> FakeClientResponse:
            if self._closed:
                raise ClientConnectionError("ClientSession is closed")

            # Extraction de l'hôte et du chemin
            clean_url = url.replace("http://", "")
            host_port, _, path = clean_url.partition("/")
            path = "/" + path
            if ":" in host_port:
                h, p = host_port.split(":")
                port = int(p)
            else:
                h = host_port
                port = 80

            timeout_sec = 15.0
            if timeout is not None:
                if isinstance(timeout, (int, float)):
                    timeout_sec = float(timeout)
                elif hasattr(timeout, "total") and timeout.total is not None:
                    timeout_sec = float(timeout.total)

            async def _perform_io() -> FakeClientResponse:
                r, w = await self.connector.get_connection(h, port)
                req_headers = dict(headers or {})
                if "Host" not in req_headers:
                    req_headers["Host"] = host_port
                if "Connection" not in req_headers and not self.connector.force_close:
                    req_headers["Connection"] = "Keep-Alive"

                body_bytes = b""
                if data is not None:
                    if isinstance(data, str):
                        body_bytes = data.encode("utf-8")
                    elif isinstance(data, (bytes, bytearray)):
                        body_bytes = bytes(data)
                req_headers["Content-Length"] = str(len(body_bytes))

                req_lines = [f"{method} {path} HTTP/1.1"]
                for k, v in req_headers.items():
                    req_lines.append(f"{k}: {v}")
                req_lines.append("")
                req_lines.append("")
                header_bytes = "\r\n".join(req_lines).encode("latin-1")

                try:
                    w.write(header_bytes + body_bytes)
                    await w.drain()
                except Exception as e:
                    self.connector.release_connection(h, port, close=True)
                    raise ClientConnectionError(f"Connection failed writing: {e}") from e

                # Lecture de la réponse HTTP
                try:
                    status_line = await r.readline()
                    if not status_line:
                        self.connector.release_connection(h, port, close=True)
                        raise ClientConnectionError("Server disconnected unexpectedly")

                    parts = status_line.decode("latin-1").split(" ", 2)
                    status_code = int(parts[1]) if len(parts) > 1 else 500

                    # Lecture des en-têtes
                    resp_headers: dict[str, str] = {}
                    while True:
                        hline = await r.readline()
                        if hline in (b"\r\n", b"\n", b""):
                            break
                        hstr = hline.decode("latin-1").strip()
                        if ":" in hstr:
                            hk, hv = hstr.split(":", 1)
                            resp_headers[hk.strip()] = hv.strip()

                    # Lecture du corps
                    cl_str = resp_headers.get("Content-Length")
                    if cl_str is not None:
                        body_len = int(cl_str)
                        resp_body = await r.readexactly(body_len) if body_len > 0 else b""
                    elif resp_headers.get("Transfer-Encoding", "").lower() == "chunked":
                        chunks = []
                        while True:
                            size_line = await r.readline()
                            chunk_size = int(size_line.strip(), 16)
                            if chunk_size == 0:
                                await r.readline()  # \r\n
                                break
                            chunk = await r.readexactly(chunk_size)
                            chunks.append(chunk)
                            await r.readline()  # \r\n
                        resp_body = b"".join(chunks)
                    else:
                        resp_body = b""

                    if self.connector.force_close or resp_headers.get("Connection", "").lower() == "close":
                        self.connector.release_connection(h, port, close=True)

                    return FakeClientResponse(status_code, resp_headers, resp_body)

                except Exception as e:
                    self.connector.release_connection(h, port, close=True)
                    if isinstance(e, ClientError):
                        raise
                    raise ClientConnectionError(f"HTTP request error: {e}") from e

            try:
                return await asyncio.wait_for(_perform_io(), timeout=timeout_sec)
            except asyncio.TimeoutError as err:
                self.connector.release_connection(h, port, close=True)
                raise asyncio.TimeoutError(f"HTTP timeout after {timeout_sec}s") from err

    aiohttp_shim = types.ModuleType("aiohttp")
    aiohttp_shim.ClientSession = ClientSession
    aiohttp_shim.TCPConnector = TCPConnector
    aiohttp_shim.ClientTimeout = ClientTimeout
    aiohttp_shim.ClientError = ClientError
    aiohttp_shim.ClientConnectionError = ClientConnectionError
    aiohttp_shim.ClientPayloadError = ClientPayloadError

    web_shim = types.ModuleType("aiohttp.web")
    aiohttp_shim.web = web_shim

    sys.modules["aiohttp"] = aiohttp_shim
    sys.modules["aiohttp.web"] = web_shim
    aiohttp = aiohttp_shim

# Chargement immédiat des mocks Home Assistant (Phase 2)
import tests.ha_mock

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from custom_components.smart_remote_rebirth.client import AsyncSmartRemoteClient
from custom_components.smart_remote_rebirth.crypto import (
    API_NONCE,
    NONCE_PS_MSG05,
    NONCE_PS_MSG06,
    NONCE_PV_MSG02,
    NONCE_PV_MSG03,
    SRP_G,
    SRP_N,
    big_integer_from_bytes,
    big_integer_to_bytes,
    decode_tlv,
    encode_tlv,
    hkdf_sha512,
    pad_to_n,
)


class MockSmartRemoteServer:
    """Serveur asynchrone émulant fidèlement la télécommande Sevenhugs SR1A."""

    def __init__(self, pair_setup_delay: float = 0.05) -> None:
        self.pair_setup_delay = pair_setup_delay
        self.remote_sk = ed25519.Ed25519PrivateKey.generate()
        self.remote_pk = self.remote_sk.public_key().public_bytes_raw()
        self.remote_id = "5726"

        self.server: asyncio.Server | None = None
        self.port: int = 0
        self.host: str = "127.0.0.1"

        # Suivi de la socket TCP active Keep-Alive
        self.active_verify_sessions: dict[Any, dict[str, Any]] = {}
        self.verified_connections: dict[Any, dict[str, Any]] = {}
        self.active_writers: set[asyncio.StreamWriter] = set()

        # Compteurs d'appels pour la détection du Thundering Herd
        self.m1_verify_count: int = 0
        self.m3_verify_count: int = 0

        # Drapeaux de test de résilience
        self.force_disconnect_next_api: bool = False
        self.force_auth_error_next_api: bool = False
        self.last_api_request_data: dict | None = None
        self.lua_steps_called: list[str] = []
        self.serial_number: str = "a06fe048b48b260139"
        self.pin: str = "123-45-678"
        self.salt: bytes = b"\x11" * 16
        self.srp_b_secret: bytes | None = None
        self.srp_b_pub: bytes | None = None
        self.srp_v: int | None = None
        self.srp_shared_k: bytes | None = None
        self.fail_pair_setup_m1: str | None = None
        self.fail_hello: bool = False
        self.fail_srp_m3: bool = False
        self.fail_m5_disconnect: bool = False

    async def start(self) -> None:
        """Démarre le serveur mock sur un port dynamique local."""
        self.server = await asyncio.start_server(self._handle_client, self.host, 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Arrête le serveur mock et libère les sockets."""
        for w in list(self.active_writers):
            try:
                w.close()
            except Exception:
                pass
        for w in list(self.active_writers):
            try:
                await w.wait_closed()
            except Exception:
                pass
        self.active_writers.clear()

        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Gère les requêtes HTTP/1.1 séquentielles sur la même connexion TCP Keep-Alive."""
        peer = writer.get_extra_info("peername")
        conn_id = peer  # Tuple (client_ip, client_port) garantit l'unicité de la socket active
        self.active_writers.add(writer)

        try:
            while True:
                req_line = await reader.readline()
                if not req_line:
                    break
                line_str = req_line.decode("latin-1").strip()
                if not line_str:
                    continue

                parts = line_str.split(" ")
                if len(parts) < 2:
                    break
                method, path = parts[0], parts[1]

                # Lecture des en-têtes
                headers: dict[str, str] = {}
                while True:
                    hline = await reader.readline()
                    if hline in (b"\r\n", b"\n", b""):
                        break
                    hstr = hline.decode("latin-1").strip()
                    if ":" in hstr:
                        hk, hv = hstr.split(":", 1)
                        headers[hk.strip()] = hv.strip()

                # Lecture du corps
                body = b""
                if "Content-Length" in headers:
                    blen = int(headers["Content-Length"])
                    if blen > 0:
                        body = await reader.readexactly(blen)

                # Traitement de la route
                status, resp_headers, resp_body = await self._route_request(
                    method, path, headers, body, conn_id, writer
                )

                if status is None:
                    # Connexion brutalement interrompue par le test
                    return

                resp_lines = [f"HTTP/1.1 {status} OK"]
                resp_headers["Content-Length"] = str(len(resp_body))
                resp_headers["Connection"] = "keep-alive"
                for hk, hv in resp_headers.items():
                    resp_lines.append(f"{hk}: {hv}")
                resp_lines.append("")
                resp_lines.append("")

                header_bytes = "\r\n".join(resp_lines).encode("latin-1")
                writer.write(header_bytes + resp_body)
                await writer.drain()

        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            self.active_writers.discard(writer)
            self.active_verify_sessions.pop(conn_id, None)
            self.verified_connections.pop(conn_id, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _route_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        conn_id: Any,
        writer: asyncio.StreamWriter,
    ) -> tuple[int | None, dict[str, str], bytes]:
        """Achemine la requête vers l'endpoint émulé approprié."""
        if path == "/api/hello":
            if getattr(self, "fail_hello", False):
                return 500, {}, b"Error"
            return 200, {"Content-Type": "application/json"}, b'{"status": "ok"}'
        elif path == "/pair-setup":
            return await self._handle_pair_setup(body, writer)
        elif path == "/pair-verify":
            return await self._handle_pair_verify(body, conn_id)
        elif path == "/api/devices/sevenhugs.control.webhooks/setup":
            return await self._handle_lua_setup(method, body, headers)
        elif path.startswith("/api/"):
            return await self._handle_api(path, body, conn_id, writer)
        return 404, {}, b"Not Found"

    async def _handle_pair_setup(self, body: bytes, writer: asyncio.StreamWriter | None = None) -> tuple[int | None, dict[str, str], bytes]:
        tlv = decode_tlv(body)
        state = tlv.get(0x06)

        if state in (1, b"\x01"):
            if getattr(self, "fail_pair_setup_m1", None) == "500":
                return 500, {}, b"Internal Server Error (Remote Busy)"
            if getattr(self, "fail_pair_setup_m1", None) == "timeout":
                await asyncio.sleep(65.0)

            # Validation formelle du Tag 0x00 (kTLVType_Method = 1 pour SRP-6a Pair Setup)
            if tlv.get(0x00) not in (1, b"\x01"):
                err_tlv = encode_tlv([(0x06, 2), (0x07, 2)])
                return 400, {"Content-Type": "application/pairing+tlv8"}, err_tlv

            # Simulation de la latence matérielle de 16 secondes (non-bloquante)
            await asyncio.sleep(self.pair_setup_delay)

            # Calcul SRP serveur M1
            self.srp_b_secret = secrets.token_bytes(64)
            b_int = big_integer_from_bytes(self.srp_b_secret)

            username = b"Pair-Setup"
            pin_str = getattr(self, "pin", "123-45-678")
            h_i_p = hashlib.sha512(username + b":" + pin_str.encode("utf-8")).digest()
            s_bytes = big_integer_to_bytes(big_integer_from_bytes(self.salt))
            x = big_integer_from_bytes(hashlib.sha512(s_bytes + h_i_p).digest())
            self.srp_v = pow(SRP_G, x, SRP_N)

            n_padded = pad_to_n(big_integer_to_bytes(SRP_N), 384)
            g_padded = pad_to_n(big_integer_to_bytes(SRP_G), 384)
            k = big_integer_from_bytes(hashlib.sha512(n_padded + g_padded).digest())

            b_pub_val = (k * self.srp_v + pow(SRP_G, b_int, SRP_N)) % SRP_N
            self.srp_b_pub = pad_to_n(big_integer_to_bytes(b_pub_val), 384)

            resp = encode_tlv([(0x06, 2), (0x02, self.salt), (0x03, self.srp_b_pub)])
            return 200, {"Content-Type": "application/pairing+tlv8"}, resp

        elif state in (3, b"\x03"):
            if getattr(self, "fail_srp_m3", False):
                err_tlv = encode_tlv([(0x06, 4), (0x07, 2)])
                return 400, {"Content-Type": "application/pairing+tlv8"}, err_tlv

            a_bytes = tlv.get(0x03)
            m1_proof = tlv.get(0x04)

            a_int = big_integer_from_bytes(a_bytes)
            b_int = big_integer_from_bytes(self.srp_b_secret or secrets.token_bytes(64))

            # u = SHA512(pad384(A) || pad384(B))
            a_padded = pad_to_n(a_bytes, 384)
            b_padded = pad_to_n(self.srp_b_pub or (b"\x22" * 384), 384)
            u = big_integer_from_bytes(hashlib.sha512(a_padded + b_padded).digest())

            # S = (A * v^u mod N)^b mod N
            v = self.srp_v if self.srp_v is not None else 1
            val1 = (a_int * pow(v, u, SRP_N)) % SRP_N
            s_int = pow(val1, b_int, SRP_N)
            self.srp_shared_k = hashlib.sha512(big_integer_to_bytes(s_int)).digest()

            # Vérification de la preuve M1 du client
            username = b"Pair-Setup"
            hn = hashlib.sha512(big_integer_to_bytes(SRP_N)).digest()
            hg = hashlib.sha512(big_integer_to_bytes(SRP_G)).digest()
            h_xor = big_integer_to_bytes(big_integer_from_bytes(hn) ^ big_integer_from_bytes(hg))
            hi = hashlib.sha512(username).digest()
            s_bytes = big_integer_to_bytes(big_integer_from_bytes(self.salt))

            b_bytes_clean = big_integer_to_bytes(big_integer_from_bytes(self.srp_b_pub)) if self.srp_b_pub else b""
            expected_m1_input = h_xor + hi + s_bytes + a_bytes + b_bytes_clean + self.srp_shared_k
            expected_m1 = big_integer_to_bytes(big_integer_from_bytes(hashlib.sha512(expected_m1_input).digest()))
            client_m1 = big_integer_to_bytes(big_integer_from_bytes(m1_proof))

            if expected_m1 != client_m1:
                # Mauvais code PIN : retour HTTP 400 avec tag 0x07 = 2
                err_tlv = encode_tlv([(0x06, 4), (0x07, 2)])
                return 400, {"Content-Type": "application/pairing+tlv8"}, err_tlv

            # Preuve serveur M2 = SHA512(A || M1 || K)
            m2_hash = hashlib.sha512(a_bytes + m1_proof + self.srp_shared_k).digest()
            m2_proof = big_integer_to_bytes(big_integer_from_bytes(m2_hash))
            resp = encode_tlv([(0x06, 4), (0x04, m2_proof)])
            return 200, {"Content-Type": "application/pairing+tlv8"}, resp

        elif state in (5, b"\x05"):
            if getattr(self, "fail_m5_disconnect", False):
                if writer:
                    writer.close()
                return None, {}, b""

            session_key = hkdf_sha512(
                ikm=self.srp_shared_k or (b"\x33" * 64),
                salt=b"Pair-Setup-Encrypt-Salt",
                info=b"Pair-Setup-Encrypt-Info",
                length=32,
            )
            sub_m6 = encode_tlv([(0x01, self.remote_id), (0x03, self.remote_pk)])
            enc_m6 = ChaCha20Poly1305(session_key).encrypt(NONCE_PS_MSG06, sub_m6, None)
            resp = encode_tlv([(0x06, 6), (0x05, enc_m6)])
            return 200, {"Content-Type": "application/pairing+tlv8"}, resp

        return 400, {}, b"Bad Request"

    async def _handle_pair_verify(self, body: bytes, conn_id: Any) -> tuple[int, dict[str, str], bytes]:
        tlv = decode_tlv(body)
        state = tlv.get(0x06)

        if state in (1, b"\x01"):
            self.m1_verify_count += 1
            client_eph_pk = tlv[0x03]
            server_eph_sk = x25519.X25519PrivateKey.generate()
            server_eph_pk = server_eph_sk.public_key().public_bytes_raw()

            shared_secret = server_eph_sk.exchange(x25519.X25519PublicKey.from_public_bytes(client_eph_pk))
            verify_key = hkdf_sha512(
                shared_secret,
                salt=b"Pair-Verify-Encrypt-Salt",
                info=b"Pair-Verify-Encrypt-Info",
                length=32,
            )

            # Enregistrement de la session sur la socket active conn_id
            self.active_verify_sessions[conn_id] = {
                "verify_key": verify_key,
                "shared_secret": shared_secret,
                "server_eph_pk": server_eph_pk,
                "client_eph_pk": client_eph_pk,
            }

            sig = self.remote_sk.sign(server_eph_pk + self.remote_id.encode("utf-8") + client_eph_pk)
            sub_m2 = encode_tlv([(0x01, self.remote_id), (0x0A, sig)])
            enc_m2 = ChaCha20Poly1305(verify_key).encrypt(NONCE_PV_MSG02, sub_m2, None)
            m2_data = encode_tlv([(0x03, server_eph_pk), (0x05, enc_m2), (0x06, 2)])
            return 200, {"Content-Type": "application/pairing+tlv8"}, m2_data

        elif state in (3, b"\x03"):
            self.m3_verify_count += 1
            # CONTRÔLE CRITIQUE DE L'UNICITÉ DU DESCRIPTEUR DE SOCKET TCP
            if conn_id not in self.active_verify_sessions:
                # Socket différente ou fermée : HTTP 400 avec code d'erreur TLV 0x07 = 2
                err_tlv = encode_tlv([(0x06, 4), (0x07, 2)])
                return 400, {"Content-Type": "application/pairing+tlv8"}, err_tlv

            sess = self.active_verify_sessions.pop(conn_id)
            shared_secret = sess["shared_secret"]

            # Clés de session :
            # Client chiffre avec write_key et déchiffre avec read_key
            # Serveur déchiffre avec client_write_key et chiffre avec client_read_key
            client_write_key = hkdf_sha512(
                shared_secret,
                salt=b"Control-Salt",
                info=b"Control-Write-Encryption-Key",
                length=32,
            )
            client_read_key = hkdf_sha512(
                shared_secret,
                salt=b"Control-Salt",
                info=b"Control-Read-Encryption-Key",
                length=32,
            )

            self.verified_connections[conn_id] = {
                "server_read_key": client_write_key,
                "server_write_key": client_read_key,
            }

            m4_data = encode_tlv([(0x06, 4)])
            return 200, {"Content-Type": "application/pairing+tlv8"}, m4_data

        return 400, {}, b"Bad Request"

    async def _handle_api(
        self,
        path: str,
        body: bytes,
        conn_id: Any,
        writer: asyncio.StreamWriter,
    ) -> tuple[int | None, dict[str, str], bytes]:
        # Simulation d'une rupture brutale de socket (Deep Sleep de 60s)
        if self.force_disconnect_next_api:
            self.force_disconnect_next_api = False
            writer.close()
            return None, {}, b""

        # Simulation d'une expiration de session applicative
        if self.force_auth_error_next_api:
            self.force_auth_error_next_api = False
            return 400, {}, b"Session Expired"

        sess = self.verified_connections.get(conn_id)
        if not sess:
            return 400, {}, b"Unauthorized / Unverified Connection"

        server_read_key = sess["server_read_key"]
        server_write_key = sess["server_write_key"]

        req_data = None
        if body:
            pt = ChaCha20Poly1305(server_read_key).decrypt(API_NONCE, body, None)
            req_data = json.loads(pt.decode("utf-8"))
            self.last_api_request_data = req_data

        resp_obj: Any = {}
        if path == "/api/settings":
            sn = getattr(self, "serial_number", "a06fe048b48b260139")
            resp_obj = {
                "battery": 95,
                "model": "SR1A",
                "name": "Salon Remote",
                "serial_number": sn,
                "mac_address": "38:D7:CA:00:16:5E",
                "firmware_version": "v0.76.4",
                "remotes": [
                    {
                        "serial_number": sn,
                        "id": self.remote_id,
                        "firmware_version": "v0.76.4",
                        "mac_address": "38:D7:CA:00:16:5E",
                    }
                ],
            }
        elif path == "/api/manifests":
            resp_obj = [
                {"id": "sevenhugs.control.webhooks", "name": "Webhooks"},
                {"id": "sevenhugs.control.philips_hue", "name": "Philips Hue"},
            ]
        elif path == "/api/devices":
            resp_obj = {"url": "/api/devices/sevenhugs.control.webhooks/setup"}
        else:
            return 404, {}, b"Not Found"

        raw_resp = json.dumps(resp_obj).encode("utf-8")
        enc_resp = ChaCha20Poly1305(server_write_key).encrypt(API_NONCE, raw_resp, None)
        return 200, {"Content-Type": "application/octet-stream"}, enc_resp

    async def _handle_lua_setup(
        self,
        method: str,
        body: bytes,
        headers: dict[str, str],
    ) -> tuple[int, dict[str, str], bytes]:
        if method == "GET":
            # Transmission du cookie 7HUGS_SID
            return 200, {"Set-Cookie": "7HUGS_SID=mock_sid_session_99; Path=/"}, b"OK"

        # POST : traitement de la machine à états Lua
        data = json.loads(body.decode("utf-8")) if body else {}
        state = data.get("state", "connect")
        self.lua_steps_called.append(state)

        resp_data = {"status": "success", "state": state}
        return 200, {"Content-Type": "application/json"}, json.dumps(resp_data).encode("utf-8")


@pytest.fixture
def test_keys() -> dict[str, Any]:
    """Fixture fournissant un jeu de clés déterministe valide."""
    client_sk = ed25519.Ed25519PrivateKey.generate()
    client_pk = client_sk.public_key().public_bytes_raw()

    remote_sk = ed25519.Ed25519PrivateKey.generate()
    remote_pk = remote_sk.public_key().public_bytes_raw()

    return {
        "controller_id": "test-controller-uuid-1234",
        "client_private_key": client_sk.private_bytes_raw().hex(),
        "client_public_key": client_pk.hex(),
        "remote_id": "5726",
        "remote_public_key": remote_pk.hex(),
    }


@pytest.fixture
async def mock_server():
    """Fixture démarrant le serveur mock asynchrone."""
    server = MockSmartRemoteServer(pair_setup_delay=0.05)
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def client(mock_server, test_keys):
    """Fixture instanciant le client connecté au serveur mock."""
    connector = aiohttp.TCPConnector(
        force_close=False,
        limit=1,
        limit_per_host=1,
        enable_cleanup_closed=True,
        keepalive_timeout=60.0,
    )
    session = aiohttp.ClientSession(connector=connector)
    # Aligner la clé publique du serveur mock avec les clés de test
    test_keys["remote_public_key"] = mock_server.remote_pk.hex()
    client_obj = AsyncSmartRemoteClient(
        host=f"{mock_server.host}:{mock_server.port}",
        session=session,
        keys=test_keys,
    )
    yield client_obj
    await client_obj.close()
    await session.close()
