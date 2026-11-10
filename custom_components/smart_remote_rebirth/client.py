"""Client asynchrone Keep-Alive pour Sevenhugs Smart Remote (SR1A).

Gère la session HTTP/1.1 persistante sur une socket TCP unique,
l'anti-thundering herd via double verrouillage, le handshake Pair-Verify (M1 à M4),
la reconnexion et le re-handshake automatiques transparents lors des sorties de veille,
et l'automatisation de l'assistant Lua Webhooks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid

# Assure la disponibilité des dépendances vendorisées dans .pylib si présent
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYLIB_DIR = os.path.join(_BASE_DIR, ".pylib")
if os.path.isdir(_PYLIB_DIR) and _PYLIB_DIR not in sys.path:
    sys.path.insert(0, _PYLIB_DIR)

try:
    import aiohttp
except ImportError:
    # Stubs de compatibilité pour environnements sans aiohttp installé
    class _ClientError(Exception):
        pass

    class _ClientConnectionError(_ClientError):
        pass

    class _ClientPayloadError(_ClientError):
        pass

    class _ClientTimeout:
        def __init__(self, total: float | None = None) -> None:
            self.total = total

    class _TCPConnector:
        def __init__(self, *args: any, **kwargs: any) -> None:
            pass

    class _AiohttpModule:
        ClientError = _ClientError
        ClientConnectionError = _ClientConnectionError
        ClientPayloadError = _ClientPayloadError
        ClientTimeout = _ClientTimeout
        TCPConnector = _TCPConnector

    aiohttp = _AiohttpModule()  # type: ignore[assignment]

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from .crypto import (
    NONCE_PS_MSG05,
    NONCE_PS_MSG06,
    NONCE_PV_MSG02,
    NONCE_PV_MSG03,
    compute_srp_m1_sync,
    decode_tlv,
    decrypt_api_payload,
    encode_tlv,
    encrypt_api_payload,
    hkdf_sha512,
    verify_srp_m2,
)

_LOGGER = logging.getLogger(__name__)


class SevenhugsAuthError(Exception):
    """Exception levée en cas d'échec d'authentification ou de rejet de handshake."""


class SevenhugsConnectionError(Exception):
    """Exception levée en cas de défaillance réseau ou de rupture de communication."""


class AsyncSmartRemoteClient:
    """Client HTTP asynchrone Keep-Alive pour Sevenhugs Smart Remote."""

    # Nombre maximal de requêtes par paire de clés de session avant renouvellement
    # proactif Pair-Verify (atténuation du nonce fixe matériel HuMiThFr du firmware v0.76.4).
    MAX_REQUESTS_PER_SESSION_KEY: int = 50

    def __init__(
        self,
        host: str,
        session: aiohttp.ClientSession,
        keys: dict,
    ) -> None:
        """Initialise le client avec l'adresse IP, la session aiohttp dédiée et les clés."""
        self.host = host
        self._session = session
        self._keys = keys

        # Clés et identifiants du contrôleur
        self._controller_id = keys.get("controller_id")
        self._client_sk: ed25519.Ed25519PrivateKey | None = None
        raw_sk = keys.get("client_private_key")
        if raw_sk:
            if isinstance(raw_sk, str):
                self._client_sk = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw_sk))
            elif isinstance(raw_sk, (bytes, bytearray)):
                self._client_sk = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(raw_sk))

        # Clés et identifiants de la télécommande
        self._remote_id = keys.get("remote_id")
        self._remote_pk: ed25519.Ed25519PublicKey | None = None
        raw_remote_pk = keys.get("remote_public_key")
        if raw_remote_pk:
            if isinstance(raw_remote_pk, str):
                self._remote_pk = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw_remote_pk))
            elif isinstance(raw_remote_pk, (bytes, bytearray)):
                self._remote_pk = ed25519.Ed25519PublicKey.from_public_bytes(bytes(raw_remote_pk))

        # Clés de session symétriques ChaCha20-Poly1305
        self._session_write_key: bytes | None = None
        raw_write_k = keys.get("session_write_key")
        if raw_write_k:
            self._session_write_key = bytes.fromhex(raw_write_k) if isinstance(raw_write_k, str) else bytes(raw_write_k)

        self._session_read_key: bytes | None = None
        raw_read_k = keys.get("session_read_key")
        if raw_read_k:
            self._session_read_key = bytes.fromhex(raw_read_k) if isinstance(raw_read_k, str) else bytes(raw_read_k)

        # Indicateur d'état de session active : toujours False à l'instanciation
        # car une nouvelle connexion TCP Keep-Alive nécessite son propre handshake Pair-Verify
        self._is_verified: bool = False

        # Double verrouillage :
        # _verify_lock : protection contre la ruée d'appels (thundering herd) sur Pair-Verify
        # _request_lock : sérialisation des échanges HTTP physiques sur la socket active unique
        self._verify_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._session_generation: int = 0
        self._requests_since_verify: int = 0

    @property
    def session_generation(self) -> int:
        """Retourne le numéro de génération de la session courante."""
        return self._session_generation

    @property
    def is_verified(self) -> bool:
        """Retourne True si une session chiffrée Pair-Verify est établie et valide."""
        return self._is_verified

    async def close(self) -> None:
        """Nettoie l'état interne du client sans fermer la session partagée gérée par Home Assistant."""
        self._is_verified = False

    async def async_pair_verify(
        self,
        force: bool = False,
        from_generation: int | None = None,
    ) -> bool:
        """Exécute atomiquement le handshake Pair-Verify (M1 à M4) avec double-check locking.

        Garantit qu'en cas d'appels concurrents, une seule négociation physique
        est exécutée sur la socket active.
        """
        if self._is_verified and not force and from_generation is None:
            return True

        async with self._verify_lock:
            # Vérification après obtention du verrou (anti-thundering herd)
            if from_generation is not None:
                if self._session_generation > from_generation and self._is_verified:
                    return True
            elif self._is_verified and not force:
                return True

            async with self._request_lock:
                return await self._execute_pair_verify_handshake()

    async def _execute_pair_verify_handshake(self) -> bool:
        """Déroule les étapes M1 à M4 de Pair-Verify sur le même descripteur de socket TCP."""
        self._is_verified = False
        try:
            return await self._do_execute_pair_verify_handshake()
        except Exception:
            self._is_verified = False
            raise

    async def _do_execute_pair_verify_handshake(self) -> bool:
        """Déroule les étapes M1 à M4 de Pair-Verify sur le même descripteur de socket TCP."""
        if not self._client_sk:
            raise SevenhugsAuthError("Clé privée contrôleur (Ed25519) absente dans les clés")
        if not self._controller_id:
            raise SevenhugsAuthError("Identifiant contrôleur (controller_id) absent dans les clés")

        # 1. Génération de la paire éphémère X25519 client
        client_eph_sk = x25519.X25519PrivateKey.generate()
        client_eph_pk = client_eph_sk.public_key().public_bytes_raw()

        headers = {
            "Content-Type": "application/pairing+tlv8",
            "X-UUID": str(self._controller_id),
            "X-Version": "31",
            "X-Min-Version": "24",
            "Connection": "Keep-Alive",
            "User-Agent": "okhttp/3.12.0",
        }

        # 2. Envoi de M1 : Tag 0x03 (client_eph_pk) et Tag 0x06 (state=1)
        m1_data = encode_tlv([(0x03, client_eph_pk), (0x06, 1)])
        url = f"http://{self.host}/pair-verify"

        try:
            timeout_param = (
                aiohttp.ClientTimeout(total=15)
                if hasattr(aiohttp, "ClientTimeout")
                else 15
            )
            async with self._session.post(
                url, data=m1_data, headers=headers, timeout=timeout_param
            ) as resp1:
                if resp1.status != 200:
                    raise SevenhugsAuthError(f"Pair-Verify M1 a échoué avec le statut HTTP {resp1.status}")
                raw_m2 = await resp1.read()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SevenhugsConnectionError(f"Erreur réseau lors de Pair-Verify M1: {err}") from err

        # 3. Traitement de M2
        m2_tlv = decode_tlv(raw_m2)
        if 0x07 in m2_tlv:
            raise SevenhugsAuthError(f"Pair-Verify M2 a renvoyé l'erreur tag 0x07 = {m2_tlv[0x07]}")

        remote_eph_pk = m2_tlv.get(0x03)
        enc_m2 = m2_tlv.get(0x05)
        if not remote_eph_pk or not enc_m2:
            raise SevenhugsAuthError("Pair-Verify M2 incomplet : clé éphémère ou données chiffrées manquantes")

        # Calcul du secret partagé Diffie-Hellman X25519
        remote_eph_pubkey = x25519.X25519PublicKey.from_public_bytes(remote_eph_pk)
        shared_secret = client_eph_sk.exchange(remote_eph_pubkey)

        # Dérivation de verify_key via HKDF-SHA512
        verify_key = hkdf_sha512(
            ikm=shared_secret,
            salt=b"Pair-Verify-Encrypt-Salt",
            info=b"Pair-Verify-Encrypt-Info",
            length=32,
        )

        # Déchiffrement de M2 avec ChaCha20-Poly1305 et NONCE_PV_MSG02
        try:
            dec_m2 = ChaCha20Poly1305(verify_key).decrypt(NONCE_PV_MSG02, enc_m2, None)
        except Exception as err:
            raise SevenhugsAuthError(f"Échec de déchiffrement de M2 : {err}") from err

        sub_m2 = decode_tlv(dec_m2)
        remote_id_bytes = sub_m2.get(0x01, b"")
        remote_sig = sub_m2.get(0x0A, b"")
        if not remote_sig or len(remote_sig) != 64:
            raise SevenhugsAuthError("Pair-Verify M2 invalide : signature Ed25519 manquante ou malformée")

        # Vérification de la signature Ed25519 de la télécommande si sa clé publique est connue
        if self._remote_pk:
            msg_remote = remote_eph_pk + remote_id_bytes + client_eph_pk
            try:
                self._remote_pk.verify(remote_sig, msg_remote)
            except Exception as err:
                raise SevenhugsAuthError(f"Signature Ed25519 de la télécommande en M2 invalide : {err}") from err

        # 4. Préparation et envoi de M3
        cid_bytes = str(self._controller_id).encode("utf-8")
        msg_client = client_eph_pk + cid_bytes + remote_eph_pk
        client_sig = self._client_sk.sign(msg_client)

        sub_m3 = encode_tlv([(0x0A, client_sig), (0x01, cid_bytes)])
        enc_m3 = ChaCha20Poly1305(verify_key).encrypt(NONCE_PV_MSG03, sub_m3, None)
        m3_data = encode_tlv([(0x05, enc_m3), (0x06, 3)])

        try:
            async with self._session.post(
                url, data=m3_data, headers=headers, timeout=timeout_param
            ) as resp3:
                if resp3.status != 200:
                    raise SevenhugsAuthError(f"Pair-Verify M3 rejeté avec statut HTTP {resp3.status}")
                raw_m4 = await resp3.read()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SevenhugsConnectionError(f"Erreur réseau lors de Pair-Verify M3: {err}") from err

        # 5. Traitement de M4
        m4_tlv = decode_tlv(raw_m4)
        if 0x07 in m4_tlv:
            raise SevenhugsAuthError(f"Pair-Verify M4 a retourné l'erreur tag 0x07 = {m4_tlv[0x07]}")

        state_m4 = m4_tlv.get(0x06)
        if state_m4 not in (4, b"\x04"):
            raise SevenhugsAuthError(f"État M4 inattendu : {state_m4}")

        # 6. Dérivation des clés symétriques de contrôle
        self._session_write_key = hkdf_sha512(
            ikm=shared_secret,
            salt=b"Control-Salt",
            info=b"Control-Write-Encryption-Key",
            length=32,
        )
        self._session_read_key = hkdf_sha512(
            ikm=shared_secret,
            salt=b"Control-Salt",
            info=b"Control-Read-Encryption-Key",
            length=32,
        )

        self._keys["session_write_key"] = self._session_write_key.hex()
        self._keys["session_read_key"] = self._session_read_key.hex()
        self._session_generation += 1
        self._requests_since_verify = 0
        self._is_verified = True
        _LOGGER.debug(
            "Handshake Pair-Verify réussi avec succès pour %s (génération %d)",
            self.host,
            self._session_generation,
        )
        return True

    async def async_request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
    ) -> dict:
        """Émet une requête chiffrée avec reconnexion et ré-authentification transparentes."""
        if not self._is_verified:
            await self.async_pair_verify()
        elif self._requests_since_verify >= self.MAX_REQUESTS_PER_SESSION_KEY:
            _LOGGER.debug(
                "Rotation proactive des clés de session Pair-Verify après %d requêtes sur %s",
                self._requests_since_verify,
                self.host,
            )
            await self.async_pair_verify(force=True)

        for attempt in range(2):
            start_gen = self._session_generation
            try:
                async with self._request_lock:
                    return await self._raw_encrypted_request(method, path, json_data)
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
                SevenhugsAuthError,
                SevenhugsConnectionError,
            ) as err:
                if attempt == 1:
                    _LOGGER.error("Échec persistant de la requête %s %s après tentative de reconnexion: %s", method, path, err)
                    raise
                _LOGGER.info("Rupture de socket ou expiration de session sur %s. Reconnexion automatique...", path)
                await self.async_pair_verify(from_generation=start_gen)

        raise SevenhugsConnectionError(f"Requête {method} {path} a échoué")

    async def _raw_encrypted_request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
    ) -> dict:
        """Exécute l'émission brute HTTP chiffrée avec ChaCha20-Poly1305."""
        if not self._session_write_key or not self._session_read_key:
            raise SevenhugsAuthError("Clés de session manquantes. Handshake Pair-Verify requis.")

        headers = {
            "X-UUID": str(self._controller_id),
            "X-Version": "31",
            "X-Min-Version": "24",
            "Connection": "Keep-Alive",
            "User-Agent": "okhttp/3.12.0",
        }

        body: bytes | None = None
        if json_data is not None:
            headers["Content-Type"] = "application/json; charset=UTF-8"
            plaintext = json.dumps(json_data).encode("utf-8")
            body = encrypt_api_payload(self._session_write_key, plaintext)

        url = f"http://{self.host}{path}"
        timeout_param = (
            aiohttp.ClientTimeout(total=15)
            if hasattr(aiohttp, "ClientTimeout")
            else 15
        )

        try:
            async with self._session.request(
                method, url, data=body, headers=headers, timeout=timeout_param
            ) as resp:
                resp_bytes = await resp.read()
                if resp.status == 400:
                    err_detail = self._try_decode_error_bytes(resp_bytes)
                    raise SevenhugsAuthError(
                        f"Session expirée ou rejetée par la télécommande (HTTP 400){err_detail}"
                    )
                if resp.status not in (200, 204):
                    # /api/hello atteste de la présence active du serveur HTTP même s'il renvoie 404
                    if path == "/api/hello" and resp.status == 404:
                        return {"status": "ok", "code": 404}
                    err_detail = self._try_decode_error_bytes(resp_bytes)
                    raise SevenhugsConnectionError(
                        f"Requête HTTP {path} a retourné le statut {resp.status}{err_detail}"
                    )
                if resp.status == 204 or not resp_bytes:
                    return {}

                try:
                    decrypted = decrypt_api_payload(self._session_read_key, resp_bytes)
                    self._requests_since_verify += 1
                    return json.loads(decrypted.decode("utf-8"))
                except InvalidTag as err:
                    _LOGGER.warning(
                        "Échec de déchiffrement Poly1305 sur %s (longueur=%d) avec session_read_key: %s",
                        path,
                        len(resp_bytes),
                        err,
                    )
                    self._is_verified = False
                    raise SevenhugsAuthError(
                        f"Tag Poly1305 invalide lors du déchiffrement de la réponse {path} (session expirée ou réponse non authentifiée)"
                    ) from err
                except (json.JSONDecodeError, UnicodeDecodeError) as err:
                    _LOGGER.warning("Réponse déchiffrée invalide sur %s: %s", path, err)
                    self._is_verified = False
                    raise SevenhugsAuthError(f"Contenu déchiffré invalide sur {path}: {err}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            self._is_verified = False
            raise SevenhugsConnectionError(f"Erreur réseau lors de {method} {path}: {err}") from err

    def _try_decode_error_bytes(self, resp_bytes: bytes) -> str:
        """Tente de déchiffrer ou décoder en clair le corps d'une réponse d'erreur HTTP."""
        if not resp_bytes:
            return ""
        if self._session_read_key:
            try:
                dec = decrypt_api_payload(self._session_read_key, resp_bytes).decode(
                    "utf-8", errors="replace"
                )
                return f" - {dec}"
            except Exception:
                pass
        try:
            text = resp_bytes.decode("utf-8")
            if text.isprintable():
                return f" - {text}"
        except Exception:
            pass
        return f" (payload {len(resp_bytes)} octets)"

    async def async_pair_setup_m1(self) -> tuple[bytes, bytes]:
        """Émet la requête M1 de Pair-Setup vers /pair-setup et extrait Salt et clé publique B."""
        headers = {
            "Content-Type": "application/pairing+tlv8",
            "Connection": "Keep-Alive",
            "User-Agent": "okhttp/3.12.0",
        }
        # M1 : method = 1 (SRP-6a Pair Setup), state = 1 (M1)
        m1_data = encode_tlv([(0x00, 1), (0x06, 1)])
        url = f"http://{self.host}/pair-setup"

        timeout_param = (
            aiohttp.ClientTimeout(total=60)
            if hasattr(aiohttp, "ClientTimeout")
            else 60
        )

        try:
            async with self._session.post(
                url, data=m1_data, headers=headers, timeout=timeout_param
            ) as resp:
                if resp.status != 200:
                    raise SevenhugsConnectionError(f"Pair-Setup M1 a retourné le statut HTTP {resp.status}")
                raw_m2 = await resp.read()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SevenhugsConnectionError(f"Erreur réseau lors de Pair-Setup M1: {err}") from err

        m2_tlv = decode_tlv(raw_m2)
        if 0x07 in m2_tlv:
            raise SevenhugsAuthError(f"Pair-Setup M2 a retourné l'erreur code {m2_tlv[0x07]}")

        salt = m2_tlv.get(0x02)
        b_bytes = m2_tlv.get(0x03)
        if not salt or not b_bytes:
            raise SevenhugsAuthError("Réponse M2 incomplète : sel ou clé B manquants")
        return salt, b_bytes

    async def async_pair_setup_finish(
        self,
        pin: str,
        salt: bytes,
        b_bytes: bytes,
    ) -> tuple[str, bytes]:
        """Exécute les étapes M3 à M6 de Pair-Setup pour valider le PIN et échanger les identités."""
        # 1. Calculs SRP-6a client
        a_bytes, m1_proof, shared_key_k = compute_srp_m1_sync(salt, b_bytes, pin)

        headers = {
            "Content-Type": "application/pairing+tlv8",
            "Connection": "Keep-Alive",
            "User-Agent": "okhttp/3.12.0",
        }
        url = f"http://{self.host}/pair-setup"
        timeout_param = (
            aiohttp.ClientTimeout(total=30)
            if hasattr(aiohttp, "ClientTimeout")
            else 30
        )

        # 2. Envoi de M3 (A et preuve M1)
        m3_data = encode_tlv([(0x06, 3), (0x03, a_bytes), (0x04, m1_proof)])
        try:
            async with self._session.post(
                url, data=m3_data, headers=headers, timeout=timeout_param
            ) as resp3:
                if resp3.status != 200:
                    raise SevenhugsAuthError(f"Pair-Setup M3 rejeté avec statut HTTP {resp3.status}")
                raw_m4 = await resp3.read()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SevenhugsConnectionError(f"Erreur réseau lors de Pair-Setup M3: {err}") from err

        m4_tlv = decode_tlv(raw_m4)
        if 0x07 in m4_tlv:
            raise SevenhugsAuthError(f"Pair-Setup M4 a rejeté le code PIN (erreur {m4_tlv[0x07]})")

        server_m2 = m4_tlv.get(0x04)
        if not server_m2 or not verify_srp_m2(a_bytes, m1_proof, shared_key_k, server_m2):
            raise SevenhugsAuthError("Preuve serveur M2 invalide ou falsifiée")

        # 3. Dérivation des clés de session Pair-Setup
        session_key = hkdf_sha512(
            ikm=shared_key_k,
            salt=b"Pair-Setup-Encrypt-Salt",
            info=b"Pair-Setup-Encrypt-Info",
            length=32,
        )
        controller_sign_key = hkdf_sha512(
            ikm=shared_key_k,
            salt=b"Pair-Setup-Controller-Sign-Salt",
            info=b"Pair-Setup-Controller-Sign-Info",
            length=32,
        )

        # 4. Génération ou réutilisation de la clé contrôleur Ed25519
        if not self._client_sk:
            self._client_sk = ed25519.Ed25519PrivateKey.generate()
            self._keys["client_private_key"] = self._client_sk.private_bytes_raw().hex()
        client_pk_bytes = self._client_sk.public_key().public_bytes_raw()
        self._keys["client_public_key"] = client_pk_bytes.hex()

        if not self._controller_id:
            self._controller_id = str(uuid.uuid4())
            self._keys["controller_id"] = self._controller_id

        # 5. Envoi de M5
        cid_bytes = str(self._controller_id).encode("utf-8")
        msg_to_sign = controller_sign_key + cid_bytes + client_pk_bytes
        client_sig = self._client_sk.sign(msg_to_sign)

        sub_m5 = encode_tlv([(0x01, cid_bytes), (0x03, client_pk_bytes), (0x0A, client_sig)])
        enc_m5 = ChaCha20Poly1305(session_key).encrypt(NONCE_PS_MSG05, sub_m5, None)
        m5_data = encode_tlv([(0x06, 5), (0x05, enc_m5)])

        try:
            async with self._session.post(
                url, data=m5_data, headers=headers, timeout=timeout_param
            ) as resp5:
                if resp5.status != 200:
                    raise SevenhugsAuthError(f"Pair-Setup M5 rejeté avec statut HTTP {resp5.status}")
                raw_m6 = await resp5.read()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SevenhugsConnectionError(f"Erreur réseau lors de Pair-Setup M5: {err}") from err

        m6_tlv = decode_tlv(raw_m6)
        if 0x07 in m6_tlv:
            raise SevenhugsAuthError(f"Pair-Setup M6 a retourné une erreur {m6_tlv[0x07]}")

        enc_m6 = m6_tlv.get(0x05)
        if not enc_m6:
            raise SevenhugsAuthError("M6 incomplet : données chiffrées absentes")

        try:
            dec_m6 = ChaCha20Poly1305(session_key).decrypt(NONCE_PS_MSG06, enc_m6, None)
        except Exception as err:
            raise SevenhugsAuthError(f"Échec de déchiffrement de M6 : {err}") from err
        sub_m6 = decode_tlv(dec_m6)

        remote_id = sub_m6.get(0x01, b"").decode("utf-8", errors="replace")
        remote_pk_bytes = sub_m6.get(0x03, b"")
        self._remote_id = remote_id
        self._remote_pk = ed25519.Ed25519PublicKey.from_public_bytes(remote_pk_bytes)

        self._keys["remote_id"] = self._remote_id
        self._keys["remote_public_key"] = remote_pk_bytes.hex()
        return remote_id, remote_pk_bytes

    @staticmethod
    def build_lua_payload(
        state: str | None,
        item_id: str | None = None,
        list_id: str | None = None,
        inputs: dict | None = None,
    ) -> dict:
        """Construit un payload JSON universel pour l'assistant /rom/webapp/resources/setup.lua."""
        if state is None:
            return {}
        payload: dict = {"state": state}
        sub: dict = {}
        if inputs:
            for k, v in inputs.items():
                payload[k] = v
                sub[k] = v
        if item_id is not None:
            for key in ("id", "item", "item_id", "value", "selected", "choice", "method", "target"):
                payload[key] = item_id
            sub["id"] = item_id
        if list_id is not None and item_id is not None:
            payload[list_id] = item_id
            sub[list_id] = item_id
        if sub:
            for container in (
                "values", "inputs", "params", "fields", "form",
                "data", "result", "results", "selection", "list", "items",
            ):
                payload[container] = dict(sub)
        return payload

    async def async_push_webhooks(self, triggers: list[dict | str]) -> bool:
        """Automatise l'assistant Lua /api/devices/sevenhugs.control.webhooks/setup avec cookie 7HUGS_SID."""
        manifests = await self.async_request("GET", "/api/manifests")
        if not isinstance(manifests, list):
            raise SevenhugsConnectionError("Format inattendu pour la liste des manifestes")

        webhooks_manifest = next(
            (m for m in manifests if isinstance(m, dict) and m.get("id") == "sevenhugs.control.webhooks"),
            None,
        )
        if not webhooks_manifest:
            raise SevenhugsConnectionError("Manifeste 'sevenhugs.control.webhooks' introuvable")

        manifest_payload = {k: v for k, v in webhooks_manifest.items() if k != "discover"}
        redirect_resp = await self.async_request("POST", "/api/devices", manifest_payload)
        if not isinstance(redirect_resp, dict) or "url" not in redirect_resp:
            raise SevenhugsConnectionError(f"Échec de l'initialisation de l'assistant Webhooks: {redirect_resp}")

        setup_path = redirect_resp["url"]
        if (
            not isinstance(setup_path, str)
            or not setup_path.startswith("/api/devices/")
            or not setup_path.endswith("/setup")
            or "://" in setup_path
            or ".." in setup_path
            or "\\" in setup_path
            or "@" in setup_path
            or any(ch.isspace() for ch in setup_path)
        ):
            raise SevenhugsAuthError(
                f"Chemin d'assistant Lua rejeté pour raison de sécurité: {setup_path!r}"
            )
        setup_url = f"http://{self.host}{setup_path}"

        # Étape 1 : Récupération du cookie 7HUGS_SID via GET non chiffré
        cookie_sid: str = ""
        timeout_param = (
            aiohttp.ClientTimeout(total=15)
            if hasattr(aiohttp, "ClientTimeout")
            else 15
        )
        try:
            async with self._session.get(setup_url, timeout=timeout_param) as r:
                # Capture du cookie dans headers ou cookies
                if "Set-Cookie" in r.headers:
                    cookie_sid = r.headers["Set-Cookie"].split(";")[0]
                elif hasattr(r, "cookies") and "7HUGS_SID" in r.cookies:
                    cookie_sid = f"7HUGS_SID={r.cookies['7HUGS_SID'].value}"
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SevenhugsConnectionError(f"Impossible d'accéder à l'URL d'assistant {setup_url}: {err}") from err

        async def post_lua(payload_data: dict) -> tuple[int, dict]:
            nonlocal cookie_sid
            req_headers = {
                "Content-Type": "application/json",
            }
            if cookie_sid:
                req_headers["Cookie"] = cookie_sid
            body_bytes = json.dumps(payload_data).encode("utf-8")
            async with self._session.post(
                setup_url, data=body_bytes, headers=req_headers, timeout=timeout_param
            ) as resp:
                if "Set-Cookie" in resp.headers:
                    cookie_sid = resp.headers["Set-Cookie"].split(";")[0]
                elif hasattr(resp, "cookies") and "7HUGS_SID" in resp.cookies:
                    cookie_sid = f"7HUGS_SID={resp.cookies['7HUGS_SID'].value}"
                resp_raw = await resp.read()
                try:
                    return resp.status, json.loads(resp_raw.decode("utf-8"))
                except Exception:
                    return resp.status, {}

        # Séquence de transition d'état Lua
        await post_lua({})
        await post_lua({"state": "main_page"})

        for spec in triggers:
            if isinstance(spec, str):
                parts = spec.split("|", 3)
                if len(parts) < 3:
                    raise ValueError(f"Spécification trigger invalide: '{spec}' (attendu: 'Nom|METHOD|URL')")
                name = parts[0].strip()
                method = parts[1].strip().lower()
                trig_url = parts[2].strip()
                body_val = parts[3] if len(parts) > 3 else ""
            elif isinstance(spec, dict):
                name = str(spec.get("name", "Trigger"))
                method = str(spec.get("method", "post")).lower()
                trig_url = str(spec.get("url", ""))
                body_val = str(spec.get("body", ""))
            else:
                continue

            await post_lua({"state": "add_choice"})
            await post_lua(self.build_lua_payload("advanced_add_edit", item_id=method, list_id="methods_id"))
            inputs = {"url": trig_url, "name": name, "body": body_val}
            await post_lua(self.build_lua_payload("post_advanced_add_edit", inputs=inputs))

        st_done, _ = await post_lua({"state": "done"})
        if st_done not in (200, 204):
            raise SevenhugsConnectionError(f"Validation finale de l'assistant Webhooks échouée ({st_done})")
        return True

    async def async_delete_remote_items(self, item_ids: list[str]) -> int:
        """Supprime définitivement des appareils (/api/devices) ou des groupes (/api/components-groups) de la télécommande."""
        if not item_ids:
            return 0

        devices_map: dict[tuple[str, str], dict] = {}
        try:
            devices_raw = await self.async_request("GET", "/api/devices")
            if isinstance(devices_raw, list):
                for cat in devices_raw:
                    if isinstance(cat, dict):
                        for d in cat.get("devices_info", []):
                            if isinstance(d, dict):
                                pkg = str(d.get("package_id", ""))
                                dev = str(d.get("device_id", ""))
                                if pkg and dev:
                                    devices_map[(pkg, dev)] = d
        except Exception as err:
            _LOGGER.debug("Impossible de pré-charger /api/devices avant suppression: %s", err)

        groups_map: dict[str, dict] = {}
        try:
            groups_raw = await self.async_request("GET", "/api/components-groups")
            if isinstance(groups_raw, list):
                for g in groups_raw:
                    if isinstance(g, dict) and g.get("id"):
                        groups_map[str(g["id"])] = g
        except Exception as err:
            _LOGGER.debug("Impossible de pré-charger /api/components-groups avant suppression: %s", err)

        deleted_count = 0
        deleted_set: set[str] = set()

        # Supprimer les groupes (sevenhugs.control.groups:*) en premier pour libérer les appareils rattachés
        sorted_ids = sorted(
            (r.strip() for r in item_ids if isinstance(r, str) and ":" in r),
            key=lambda x: 0 if x.startswith("sevenhugs.control.groups:") else 1,
        )

        for item_id in sorted_ids:
            pkg_id, _, dev_id = item_id.partition(":")
            try:
                if pkg_id == "sevenhugs.control.groups":
                    payload = groups_map.get(dev_id, {"id": dev_id, "package_id": "sevenhugs.control.groups"})
                    await self.async_request("DELETE", "/api/components-groups", payload)
                else:
                    payload = dict(devices_map.get((pkg_id, dev_id), {"package_id": pkg_id, "device_id": dev_id}))
                    await self.async_request("DELETE", "/api/devices", payload)
                deleted_count += 1
                deleted_set.add(item_id)
                _LOGGER.info("Appareil/groupe supprimé de la télécommande : %s", item_id)
            except Exception as err:
                _LOGGER.warning("Échec de la suppression de %s sur la télécommande : %s", item_id, err)

        if deleted_set:
            try:
                settings = await self.async_request("GET", "/api/settings")
                if isinstance(settings, dict) and isinstance(settings.get("carousel_order"), list):
                    old_carousel = settings["carousel_order"]
                    new_carousel = [
                        item for item in old_carousel
                        if not (isinstance(item, dict) and item.get("id") in deleted_set)
                    ]
                    if len(new_carousel) != len(old_carousel):
                        await self.async_request("PATCH", "/api/settings", {"carousel_order": new_carousel})
            except Exception as err:
                _LOGGER.debug("Mise à jour optionnelle de carousel_order ignorée : %s", err)

        return deleted_count
