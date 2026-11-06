"""Banc d'essais contradictoires et de stress empirique pour le Jalon 3 (Phase 3).

Auteur: Challenger 2 (Milestone 3 - EMPIRICAL CHALLENGER)
Rôles: critic, specialist

Axes contradictoires et scénarios de stress empirique couverts :
1. SevenhugsData - Concurrence et Mutations Massives :
   - Salve de 50 coroutines écrivant et 50 coroutines lisant simultanément
     via attributs et clés de dictionnaire.
   - Remplacement concurrent du client pendant des requêtes proxy intensives.
   - Expansion concurrente de clés personnalisées et suppression.
   - Résilience lors de snapshots et d'itérations sous mutations concurrentes.
   - Invariance bidirectionnelle absolue : runtime_data[k] is getattr(runtime_data, k).

2. SevenhugsData - Délégation Proxy & Cas Limites :
   - Délégation des propriétés directes du client (is_verified, session_generation, host).
   - Propagation stricte des AttributeError internes levées par une propriété client.
   - Propagation transparente des exceptions autres (ValueError, RuntimeError).
   - Délégation des méthodes asynchrones (async_request, async_pair_verify, close).
   - Délégation des méthodes synchrones (build_lua_payload).
   - Recherche d'attributs privés (_leading_underscore) existants vs non-existants.
   - Levée stricte d'AttributeError sur attributs inconnus et KeyError sur clés inconnues.
   - Comportement strict aux limites lorsque client est None ou déconnecté.

3. Validation de la Localisation et Interpolation de Chaînes :
   - Extraction dynamique et validation 100% conforme des clés d'entités entre
     sensor.py, binary_sensor.py, button.py et strings.json / en.json / fr.json.
   - Détection d'éventuelles clés orphelines ou manquantes dans l'arbre entity.
   - Validation de l'interpolation nominale des variables ({sn}, {host}).
   - Test de robustesse aux variables d'interpolation manquantes (KeyError attendu).
   - Test de résistance aux injections de types adverses (None, entiers, Unicode/émojis,
     HTML/XML, formats imbriqués, charges volumineuses).
   - Intégrité syntaxique des accolades pour éviter tout crash dans Home Assistant.

4. Écouteur de Mise à Jour des Options (update_listener) & Rechargement :
   - Enregistrement nominal de update_listener lors du setup.
   - Déclenchement automatique de async_reload lors de modifications d'options.
   - Nettoyage propre et désinscription de l'écouteur via async_on_unload au déchargement.
   - Tolérance aux rechargements multiples et protection contre l'accumulation de listeners.
   - Dégradation gracieuse si async_reload n'est pas exposé par le mock ou l'environnement.
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import sys
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Ingestion de l'environnement de mocks
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

import tests.conftest
from tests.conftest import MockSmartRemoteServer
import aiohttp

from custom_components.sevenhugs_smart_remote import (
    DOMAIN,
    PLATFORMS,
    AsyncSmartRemoteClient,
    SevenhugsData,
    SevenhugsDataUpdateCoordinator,
    async_setup_entry,
    async_unload_entry,
    update_listener,
)
from custom_components.sevenhugs_smart_remote.const import (
    CONF_CLIENT_PRIVATE_KEY,
    CONF_CLIENT_PUBLIC_KEY,
    CONF_CONTROLLER_ID,
    CONF_HOST,
    CONF_PORT,
    CONF_REMOTE_ID,
    CONF_REMOTE_PUBLIC_KEY,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_READ_KEY,
    CONF_SESSION_WRITE_KEY,
    CONF_POLLING_INTERVAL,
    DEFAULT_POLLING_INTERVAL,
    SLEEP_CONSECUTIVE_FAILURES_THRESHOLD,
)
from custom_components.sevenhugs_smart_remote.sensor import (
    SevenhugsBatterySensor,
    SevenhugsWifiSignalSensor,
    SevenhugsActiveScreenSensor,
)
from custom_components.sevenhugs_smart_remote.binary_sensor import (
    SevenhugsChargingBinarySensor,
    SevenhugsConnectivityBinarySensor,
)
from custom_components.sevenhugs_smart_remote.button import (
    SevenhugsForceSyncButton,
    SevenhugsPingButton,
    SevenhugsBackupConfigButton,
)


class TestSevenhugsDataConcurrency(unittest.IsolatedAsyncioTestCase):
    """Banc de stress de concurrence et de synchronisation pour SevenhugsData."""

    async def asyncSetUp(self) -> None:
        self.mock_client = AsyncMock(spec=AsyncSmartRemoteClient)
        self.mock_client.host = "192.168.1.50:80"
        self.mock_session = AsyncMock()
        self.mock_coord = AsyncMock()
        self.data = SevenhugsData(
            client=self.mock_client,
            session=self.mock_session,
            coordinator=self.mock_coord,
        )

    async def test_massive_concurrent_attribute_and_item_mutations(self) -> None:
        """50 coroutines écrivant et 50 coroutines lisant simultanément via clés et attributs."""
        iterations = 50
        errors: list[Exception] = []

        async def worker_writer_item(worker_id: int) -> None:
            try:
                for i in range(iterations):
                    new_val = f"coord_item_{worker_id}_{i}"
                    self.data["coordinator"] = new_val
                    await asyncio.sleep(0.0001)
            except Exception as exc:
                errors.append(exc)

        async def worker_writer_attr(worker_id: int) -> None:
            try:
                for i in range(iterations):
                    new_val = f"coord_attr_{worker_id}_{i}"
                    self.data.coordinator = new_val
                    await asyncio.sleep(0.0001)
            except Exception as exc:
                errors.append(exc)

        async def worker_reader(worker_id: int) -> None:
            try:
                for _ in range(iterations):
                    # Lecture croisée
                    attr_val = self.data.coordinator
                    item_val = self.data["coordinator"]
                    # Comme l'accès dictionnaire et attribut sont tous deux synchrones
                    # et exécutés sur le même thread de l'event loop asyncio, ils doivent
                    # être identiques ou pointer vers des valeurs string valides.
                    self.assertIsInstance(attr_val, (str, AsyncMock))
                    self.assertIsInstance(item_val, (str, AsyncMock))
                    await asyncio.sleep(0.0001)
            except Exception as exc:
                errors.append(exc)

        tasks = []
        for w in range(25):
            tasks.append(worker_writer_item(w))
            tasks.append(worker_writer_attr(w))
            tasks.append(worker_reader(w))

        await asyncio.gather(*tasks)

        self.assertEqual(len(errors), 0, f"Erreurs détectées lors des mutations concurrentes : {errors}")
        # Invariance finale : la clé et l'attribut doivent être strictement identiques
        self.assertIs(self.data.coordinator, self.data["coordinator"])

    async def test_concurrent_delegation_proxy_reads_under_client_swaps(self) -> None:
        """Vérifie que la délégation proxy (host, is_verified) reste stable lors de swaps concurrents du client."""
        client_a = MagicMock()
        client_a.host = "10.0.0.1:80"
        client_a.is_verified = True
        client_a.session_generation = 1

        client_b = MagicMock()
        client_b.host = "10.0.0.2:80"
        client_b.is_verified = False
        client_b.session_generation = 2

        data = SevenhugsData(client=client_a, session=self.mock_session)
        stop_event = asyncio.Event()
        errors: list[Exception] = []

        async def client_switcher() -> None:
            try:
                for step in range(100):
                    data.client = client_b if step % 2 == 0 else client_a
                    await asyncio.sleep(0.0002)
            except Exception as exc:
                errors.append(exc)
            finally:
                stop_event.set()

        async def proxy_reader() -> None:
            try:
                while not stop_event.is_set():
                    host = data.host
                    is_ver = data.is_verified
                    gen = data.session_generation
                    self.assertIn(host, ("10.0.0.1:80", "10.0.0.2:80"))
                    self.assertIn(is_ver, (True, False))
                    self.assertIn(gen, (1, 2))
                    await asyncio.sleep(0.0001)
            except Exception as exc:
                errors.append(exc)

        readers = [asyncio.create_task(proxy_reader()) for _ in range(10)]
        switcher = asyncio.create_task(client_switcher())

        await switcher
        await asyncio.gather(*readers)

        self.assertEqual(len(errors), 0, f"Erreurs de proxy détectées : {errors}")

    async def test_concurrent_custom_keys_expansion_and_integrity(self) -> None:
        """Vérifie l'ajout et la suppression massive de clés tierces sans altération des clés cœur."""
        data = SevenhugsData(client=self.mock_client, session=self.mock_session, coordinator=self.mock_coord)

        async def mutate_custom_keys(worker_id: int) -> None:
            for i in range(20):
                k = f"key_{worker_id}_{i}"
                data[k] = i * 10
                self.assertEqual(data[k], i * 10)
                await asyncio.sleep(0.00005)
                del data[k]
                self.assertNotIn(k, data)

        await asyncio.gather(*(mutate_custom_keys(w) for w in range(10)))

        # Vérification de la préservation absolue des clés primaires
        self.assertIs(data["client"], self.mock_client)
        self.assertIs(data["session"], self.mock_session)
        self.assertIs(data["coordinator"], self.mock_coord)
        self.assertIs(data.client, self.mock_client)
        self.assertIs(data.session, self.mock_session)
        self.assertIs(data.coordinator, self.mock_coord)

    async def test_concurrent_iteration_safety(self) -> None:
        """Vérifie que la création de snapshots (.keys(), .values(), .items()) est sûre."""
        data = SevenhugsData(client=self.mock_client, session=self.mock_session)
        stop = False

        async def mutator() -> None:
            nonlocal stop
            for i in range(100):
                data[f"temp_{i}"] = i
                await asyncio.sleep(0.0001)
                data.pop(f"temp_{i}", None)
            stop = True

        async def iterator() -> None:
            while not stop:
                # Copie défensive de liste
                keys = list(data.keys())
                self.assertIn("client", keys)
                self.assertIn("session", keys)
                items = dict(list(data.items()))
                self.assertIn("client", items)
                await asyncio.sleep(0.0001)

        await asyncio.gather(mutator(), iterator())


class TestSevenhugsDataDelegationEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Banc contradictoire des cas limites de délégation proxy sur SevenhugsData."""

    def test_property_delegation_on_client(self) -> None:
        """Valide la résolution des propriétés natives ou dynamiques du client."""
        class MockClientWithProps:
            def __init__(self) -> None:
                self.host = "192.168.1.88:80"
                self._counter = 10

            @property
            def is_verified(self) -> bool:
                return True

            @property
            def session_generation(self) -> int:
                return self._counter

            @property
            def buggy_property(self) -> None:
                raise AttributeError("Inner attribute not found on sub-object")

            @property
            def error_property(self) -> None:
                raise RuntimeError("Unexpected failure inside property getter")

        client = MockClientWithProps()
        data = SevenhugsData(client=client, session=MagicMock())

        # Propriétés standard
        self.assertEqual(data.host, "192.168.1.88:80")
        self.assertTrue(data.is_verified)
        self.assertEqual(data.session_generation, 10)

        # Propriété interne qui lève AttributeError
        with self.assertRaises(AttributeError):
            _ = data.buggy_property

        # Propriété interne qui lève RuntimeError (ne doit pas être convertie ou masquée)
        with self.assertRaises(RuntimeError) as cm:
            _ = data.error_property
        self.assertIn("Unexpected failure inside property getter", str(cm.exception))

    async def test_method_delegation_sync_and_async(self) -> None:
        """Valide la délégation transparente des méthodes asynchrones et synchrones du client."""
        client = AsyncMock()
        client.async_request.return_value = {"status": "ok", "battery": 80}
        client.async_pair_verify.return_value = True
        client.close.return_value = None
        client.build_lua_payload = MagicMock(return_value={"lua": "print('hello')"})

        data = SevenhugsData(client=client, session=MagicMock())

        # 1. Méthode asynchrone async_request
        req_res = await data.async_request("GET", "/api/settings", payload={"foo": "bar"})
        self.assertEqual(req_res, {"status": "ok", "battery": 80})
        client.async_request.assert_awaited_once_with("GET", "/api/settings", payload={"foo": "bar"})

        # 2. Méthode asynchrone async_pair_verify
        verify_res = await data.async_pair_verify(force=True)
        self.assertTrue(verify_res)
        client.async_pair_verify.assert_awaited_once_with(force=True)

        # 3. Méthode synchrone
        lua_res = data.build_lua_payload(["trigger_a"])
        self.assertEqual(lua_res, {"lua": "print('hello')"})
        client.build_lua_payload.assert_called_once_with(["trigger_a"])

        # 4. Méthode close
        await data.close()
        client.close.assert_awaited_once()

    def test_private_attribute_delegation(self) -> None:
        """Vérifie le comportement sur les attributs privés (_leading_underscore)."""
        client = MagicMock(spec=AsyncSmartRemoteClient)
        client._controller_id = "ctrl-private-123"
        client._keys = {"sec": "key"}

        data = SevenhugsData(client=client, session=MagicMock())

        # Attributs privés présents sur le client
        self.assertEqual(data._controller_id, "ctrl-private-123")
        self.assertEqual(data._keys, {"sec": "key"})

        # Attributs privés absents du client et de SevenhugsData -> AttributeError obligatoire
        with self.assertRaises(AttributeError):
            _ = data._non_existent_private_attr

        with self.assertRaises(AttributeError):
            _ = data.__dunder_non_existent__

    def test_strict_non_existent_attributes_and_keys(self) -> None:
        """Vérifie la levée stricte d'AttributeError pour les attributs et KeyError pour les clés."""
        client = MagicMock(spec=AsyncSmartRemoteClient)
        # Supprime tout comportement auto-mock pour les attributs inexistants
        del client.totally_unknown_attribute
        del client.non_existing_method

        data = SevenhugsData(client=client, session=MagicMock())

        with self.assertRaises(AttributeError):
            _ = data.totally_unknown_attribute

        with self.assertRaises(KeyError):
            _ = data["totally_unknown_key"]

        with self.assertRaises(KeyError):
            _ = data[12345]

        with self.assertRaises(KeyError):
            _ = data[None]

    def test_behavior_when_client_is_none_or_missing(self) -> None:
        """Vérifie la résilience et la levée stricte d'AttributeError quand client est None."""
        mock_coord = MagicMock()
        mock_session = MagicMock()
        data = SevenhugsData(client=None, session=mock_session, coordinator=mock_coord)  # type: ignore[arg-type]

        # Les attributs propres restent accessibles
        self.assertIsNone(data.client)
        self.assertIs(data.session, mock_session)
        self.assertIs(data.coordinator, mock_coord)
        self.assertIs(data["coordinator"], mock_coord)

        # Tout attribut délégué doit lever AttributeError indiquant SevenhugsData
        with self.assertRaises(AttributeError) as cm:
            _ = data.host
        self.assertIn("SevenhugsData", str(cm.exception))
        self.assertIn("host", str(cm.exception))

        with self.assertRaises(AttributeError):
            _ = data.async_request

    def test_bidirectional_synchronization_invariance(self) -> None:
        """Valide exhaustivement l'invariance bidirectionnelle de toutes les clés d'exécution."""
        client1 = MagicMock()
        client1.host = "client1:80"
        client2 = MagicMock()
        client2.host = "client2:80"

        session1 = MagicMock()
        session2 = MagicMock()

        coord1 = MagicMock()
        coord2 = MagicMock()

        data = SevenhugsData(client=client1, session=session1, coordinator=coord1)

        # 1. Mutation via __setitem__
        data["client"] = client2
        self.assertIs(data.client, client2)
        self.assertEqual(data.host, "client2:80")

        data["session"] = session2
        self.assertIs(data.session, session2)

        data["coordinator"] = coord2
        self.assertIs(data.coordinator, coord2)

        # 2. Mutation via __setattr__
        data.client = client1
        self.assertIs(data["client"], client1)
        self.assertEqual(data.host, "client1:80")

        data.session = session1
        self.assertIs(data["session"], session1)

        data.coordinator = coord1
        self.assertIs(data["coordinator"], coord1)

        # 3. Re-assignation None
        data.coordinator = None
        self.assertIsNone(data["coordinator"])
        self.assertIsNone(data.coordinator)


class TestLocalizationValidationAndInterpolation(unittest.TestCase):
    """Banc contradictoire sur la validité et la robustesse de la localisation multilingue."""

    def setUp(self) -> None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.strings_path = os.path.join(base_dir, "custom_components", "sevenhugs_smart_remote", "strings.json")
        self.en_path = os.path.join(base_dir, "custom_components", "sevenhugs_smart_remote", "translations", "en.json")
        self.fr_path = os.path.join(base_dir, "custom_components", "sevenhugs_smart_remote", "translations", "fr.json")

        with open(self.strings_path, encoding="utf-8") as f:
            self.strings = json.load(f)
        with open(self.en_path, encoding="utf-8") as f:
            self.en = json.load(f)
        with open(self.fr_path, encoding="utf-8") as f:
            self.fr = json.load(f)

    def _get_leaf_entries(self, d: dict, prefix: str = "") -> dict[str, str]:
        leaves = {}
        for k, v in d.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                leaves.update(self._get_leaf_entries(v, p))
            else:
                leaves[p] = str(v)
        return leaves

    def test_entity_keys_match_all_platform_definitions(self) -> None:
        """Vérifie dynamiquement que toutes les entités de sensor, binary_sensor et button sont traduites."""
        dummy_entry = ConfigEntry(
            entry_id="test_loc_match",
            domain=DOMAIN,
            title="Sevenhugs Smart Remote",
            data={CONF_SERIAL_NUMBER: "TEST_SERIAL_123"},
        )
        dummy_coordinator = MagicMock()
        dummy_coordinator.serial_number = "TEST_SERIAL_123"
        dummy_coordinator.remote_id = "123"
        dummy_coordinator.firmware_version = "0.76.4"
        dummy_coordinator.mac_address = None

        # Instanciation de l'ensemble des entités de production
        entities_by_platform: dict[str, list[Any]] = {
            "sensor": [
                SevenhugsBatterySensor(dummy_coordinator, dummy_entry),
                SevenhugsWifiSignalSensor(dummy_coordinator, dummy_entry),
                SevenhugsActiveScreenSensor(dummy_coordinator, dummy_entry),
            ],
            "binary_sensor": [
                SevenhugsChargingBinarySensor(dummy_coordinator, dummy_entry),
                SevenhugsConnectivityBinarySensor(dummy_coordinator, dummy_entry),
            ],
            "button": [
                SevenhugsForceSyncButton(dummy_coordinator, dummy_entry),
                SevenhugsPingButton(dummy_coordinator, dummy_entry),
                SevenhugsBackupConfigButton(dummy_coordinator, dummy_entry),
            ],
        }

        # Vérification croisée avec strings.json, en.json et fr.json
        for platform_name, entity_list in entities_by_platform.items():
            for entity in entity_list:
                key = entity._attr_translation_key
                self.assertIsNotNone(key, f"L'entité {entity} n'a pas défini _attr_translation_key")

                # Vérification dans strings.json
                self.assertIn(
                    platform_name,
                    self.strings.get("entity", {}),
                    f"Plateforme {platform_name} absente de strings.json['entity']",
                )
                self.assertIn(
                    key,
                    self.strings["entity"][platform_name],
                    f"Clé de traduction '{key}' pour {platform_name} absente de strings.json",
                )
                self.assertIn("name", self.strings["entity"][platform_name][key])

                # Vérification dans en.json
                self.assertIn(key, self.en["entity"][platform_name])
                self.assertIn("name", self.en["entity"][platform_name][key])

                # Vérification dans fr.json
                self.assertIn(key, self.fr["entity"][platform_name])
                self.assertIn("name", self.fr["entity"][platform_name][key])

        # Vérification qu'il n'y a aucune clé orpheline dans strings.json['entity']
        for platform_name, key_dict in self.strings.get("entity", {}).items():
            implemented_keys = {e._attr_translation_key for e in entities_by_platform.get(platform_name, [])}
            defined_keys = set(key_dict.keys())
            self.assertEqual(
                implemented_keys,
                defined_keys,
                f"Incohérence entre les entités implémentées et traduites pour {platform_name}: {implemented_keys ^ defined_keys}",
            )

    def test_string_interpolation_nominal_formatting(self) -> None:
        """Valide le formatage nominal de toutes les chaînes contenant des placeholders."""
        token_regex = re.compile(r"\{([a-zA-Z0-9_]+)\}")
        test_vars = {
            "sn": "a06fe048b48b260139",
            "host": "192.168.1.42",
        }

        for fname, doc in (("strings.json", self.strings), ("en.json", self.en), ("fr.json", self.fr)):
            leaves = self._get_leaf_entries(doc)
            for path, text in leaves.items():
                tokens = token_regex.findall(text)
                if tokens:
                    # Chaque token doit exister dans nos variables de test
                    for t in tokens:
                        self.assertIn(t, test_vars, f"Token inconnu '{t}' à {path} dans {fname}")
                    formatted = text.format(**test_vars)
                    self.assertNotIn("{", formatted, f"Accolade restante après formatage dans {path} ({fname})")
                    for t in tokens:
                        self.assertIn(test_vars[t], formatted)

    def test_string_interpolation_missing_placeholders_raises_keyerror(self) -> None:
        """Vérifie rigoureusement que le formatage Python sans les variables obligatoires lève KeyError."""
        token_regex = re.compile(r"\{([a-zA-Z0-9_]+)\}")

        for fname, doc in (("strings.json", self.strings), ("en.json", self.en), ("fr.json", self.fr)):
            leaves = self._get_leaf_entries(doc)
            for path, text in leaves.items():
                tokens = set(token_regex.findall(text))
                if tokens:
                    # 1. Formatage sans aucun argument -> doit lever KeyError
                    with self.assertRaises(KeyError, msg=f"Formatage vide n'a pas levé KeyError pour {path} ({fname})"):
                        _ = text.format()

                    # 2. Formatage partiel (si plus d'un token)
                    if len(tokens) > 1:
                        first_token = next(iter(tokens))
                        partial_vars = {first_token: "val1"}
                        with self.assertRaises(KeyError, msg=f"Formatage partiel n'a pas levé KeyError pour {path} ({fname})"):
                            _ = text.format(**partial_vars)

                    # 3. Test de formatage sécurisé avec defaultdict (comportement UI résilient)
                    safe_formatter = collections.defaultdict(lambda: "[VALEUR_INCONNUE]")
                    safe_res = text.format_map(safe_formatter)
                    self.assertIsInstance(safe_res, str)
                    self.assertNotIn("{", safe_res)

    def test_string_interpolation_adversarial_and_injection_payloads(self) -> None:
        """Stress-test de résistance face à des données hostiles (Unicode, injections, scalaires)."""
        adversarial_test_cases = [
            # 1. Caractères spéciaux Unicode & émojis
            {"sn": "SR1A_🎮_éèàô_ñ_한국어", "host": "smart-remote.local"},
            # 2. Caractères de contrôle et formatage échappé
            {"sn": "{0}", "host": "%s%d%x"},
            # 3. Injections d'accolades internes
            {"sn": "{sn}", "host": "{{host}}"},
            # 4. Injections XML / HTML
            {"sn": "<script>alert('xss')</script>", "host": "&quot;&amp;"},
            # 5. Valeurs scalaires / entiers / booléens
            {"sn": 12345678, "host": True},
            # 6. Chaîne volumineuse (10 000 caractères)
            {"sn": "A" * 10000, "host": "B" * 10000},
        ]

        token_regex = re.compile(r"\{([a-zA-Z0-9_]+)\}")

        for doc in (self.strings, self.en, self.fr):
            leaves = self._get_leaf_entries(doc)
            for path, text in leaves.items():
                tokens = token_regex.findall(text)
                if tokens:
                    for case in adversarial_test_cases:
                        # Le formatage doit réussir sans exception ni corruption de mémoire
                        formatted = text.format(**case)
                        self.assertIsInstance(formatted, str)
                        # Pour le cas volumineux, la taille doit refléter l'injection
                        if isinstance(case["sn"], str) and len(case["sn"]) == 10000:
                            self.assertGreater(len(formatted), 10000)

    def test_unmatched_curly_braces_syntax_integrity(self) -> None:
        """Scanne l'intégralité des 3 fichiers JSON pour détecter toute accolade orpheline non appariée."""
        # Regex pour vérifier que toutes les accolades sont soit un token valide {ident}, soit échappées {{ ou }}
        valid_token_pattern = re.compile(r"\{([a-zA-Z0-9_]+)\}")

        for fname, doc in (("strings.json", self.strings), ("en.json", self.en), ("fr.json", self.fr)):
            leaves = self._get_leaf_entries(doc)
            for path, text in leaves.items():
                # Enlève d'abord tous les tokens légitimes
                cleaned = valid_token_pattern.sub("", text)
                # Enlève les accolades échappées valides {{ et }}
                cleaned = cleaned.replace("{{", "").replace("}}", "")
                # Aucune accolade orpheline ne doit subsister
                self.assertNotIn(
                    "{",
                    cleaned,
                    f"Accolade ouvrante orpheline détectée dans '{path}' de {fname} : '{text}'",
                )
                self.assertNotIn(
                    "}",
                    cleaned,
                    f"Accolade fermante orpheline détectée dans '{path}' de {fname} : '{text}'",
                )


class TestOptionsUpdateListenerAndReload(unittest.IsolatedAsyncioTestCase):
    """Banc de validation et de stress de l'écouteur d'options et du rechargement de configuration."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.server = MockSmartRemoteServer()
        await self.server.start()

        self.entry = ConfigEntry(
            entry_id="opt_listener_test_entry_001",
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (Stress Options)",
            data={
                CONF_HOST: self.server.host,
                CONF_PORT: self.server.port,
                CONF_SERIAL_NUMBER: "a06fe048b48b_opt_stress",
                CONF_REMOTE_ID: "8888",
                CONF_CONTROLLER_ID: "ctrl-uuid-opt-stress",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: self.server.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
            options={
                CONF_POLLING_INTERVAL: DEFAULT_POLLING_INTERVAL,
            },
        )
        self.hass.config_entries.async_add(self.entry)

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def test_options_update_listener_nominal_flow(self) -> None:
        """Initialise l'entrée, modifie les options et valide le rechargement automatique."""
        setup_ok = await async_setup_entry(self.hass, self.entry)
        self.assertTrue(setup_ok)

        # 1. Vérification que update_listener est bien attaché
        self.assertIn(update_listener, self.entry._update_listeners)

        # 2. Simulation de modification des options via hass.config_entries.async_update_entry
        new_options = {CONF_POLLING_INTERVAL: 120}
        initial_reload_count = self.hass.config_entries.reload_count

        self.hass.config_entries.async_update_entry(self.entry, options=new_options)

        # Attente de la résolution des tâches de l'event loop
        await asyncio.sleep(0.05)

        # 3. Validation de l'incrémentation du compteur de reload
        self.assertEqual(
            self.hass.config_entries.reload_count,
            initial_reload_count + 1,
            "async_reload aurait dû être déclenché lors de la mise à jour des options",
        )

        # 4. Vérification que l'entrée rechargée est toujours opérationnelle
        self.assertIn(self.entry.entry_id, self.hass.data[DOMAIN])
        runtime_data = self.hass.data[DOMAIN][self.entry.entry_id]
        self.assertIsInstance(runtime_data.coordinator, SevenhugsDataUpdateCoordinator)
        self.assertTrue(runtime_data.coordinator.last_update_success)

        # Nettoyage final
        await async_unload_entry(self.hass, self.entry)

    async def test_options_update_listener_unloading_and_clean_removal(self) -> None:
        """Vérifie que lors du déchargement, le listener est désinscrit sans résidu."""
        setup_ok = await async_setup_entry(self.hass, self.entry)
        self.assertTrue(setup_ok)
        self.assertIn(update_listener, self.entry._update_listeners)

        # Exécution des listeners de déchargement enregistrés via async_on_unload
        unload_ok = await async_unload_entry(self.hass, self.entry)
        self.assertTrue(unload_ok)

        # Dans Home Assistant, le désabonnement est déclenché par l'appel des callbacks de async_on_unload
        for unload_callback in list(self.entry._unload_listeners):
            unload_callback()

        # Vérification qu'aucun update_listener n'est resté actif
        self.assertNotIn(
            update_listener,
            self.entry._update_listeners,
            "L'écouteur update_listener doit être désinscrit après l'exécution des unload_listeners",
        )

    async def test_options_update_listener_graceful_fallback_without_async_reload(self) -> None:
        """Vérifie que update_listener ne crashe pas si hass n'expose pas async_reload."""
        dummy_hass = MagicMock(spec=[])  # Ne contient pas config_entries
        dummy_entry = MagicMock()
        dummy_entry.entry_id = "test_entry_no_reload"

        # Doit s'exécuter silencieusement sans lever d'exception
        await update_listener(dummy_hass, dummy_entry)

    async def test_options_update_listener_rapid_reentrant_updates(self) -> None:
        """Vérifie la robustesse face à une salve de mises à jour rapprochées des options."""
        setup_ok = await async_setup_entry(self.hass, self.entry)
        self.assertTrue(setup_ok)

        # Émission de 5 mises à jour consécutives
        for interval in (15, 30, 45, 60, 120):
            self.hass.config_entries.async_update_entry(
                self.entry,
                options={CONF_POLLING_INTERVAL: interval},
            )
            await asyncio.sleep(0.01)

        # Attente de stabilisation
        await asyncio.sleep(0.1)

        self.assertGreaterEqual(self.hass.config_entries.reload_count, 1)
        self.assertIn(self.entry.entry_id, self.hass.data[DOMAIN])
        runtime_data = self.hass.data[DOMAIN][self.entry.entry_id]
        self.assertTrue(runtime_data.coordinator.last_update_success)

        await async_unload_entry(self.hass, self.entry)


if __name__ == "__main__":
    unittest.main()
