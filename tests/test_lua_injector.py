"""Suite de tests unitaires et asynchrones pour SevenhugsLuaInjector (Phase 4 - R2 & R5).

Couvre l'intégralité des spécifications et exigences du Lua Injector :
1. Initialisation de SevenhugsLuaInjector et extraction des métadonnées (remote_id, hass, entry, client).
2. Génération de l'URL de webhook local conforme à sevenhugs_{entry_id}.
3. Construction des déclencheurs de boutons par domaine :
   - light -> action toggle
   - switch -> action toggle
   - scene -> action turn_on
   - media_player -> action toggle
   - domaines fallback (fan, input_boolean) -> toggle
   - domaines non supportés -> ignorés avec journalisation
4. Extraction du friendly_name depuis les attributs d'état (hass.states.get) avec repli vers entity_id.
5. Repli gracieux (fallback) si l'entité n'est pas enregistrée dans hass.states ou sans friendly_name.
6. Validation de la conformité du payload JSON du corps (body) avec le schéma voluptuous de webhook.py.
7. Exécution de async_sync_entities :
   - Avec liste explicite d'entités.
   - En lisant les entités depuis entry.options[CONF_ENTITIES].
   - En lisant les entités depuis entry.data[CONF_ENTITIES].
   - Gestion des listes d'entités vides (aucun crash, retour True, aucun appel réseau).
8. Gestion d'erreurs et résilience :
   - SevenhugsConnectionError -> retourne False sans lever d'exception.
   - TimeoutError / asyncio.TimeoutError -> retourne False sans lever d'exception.
   - SevenhugsAuthError -> retourne False sans lever d'exception.
   - aiohttp.ClientError -> retourne False sans lever d'exception.
   - Exception inattendue -> retourne False sans lever d'exception.
   - asyncio.CancelledError -> re-levée pour respect du cycle de vie HA.
9. Tolérance aux entités corrompues, doublons, casse mixte ou formats invalides.
10. Sérialisation des appels concurrents par asyncio.Lock.
11. Notification de réveil au coordinateur lors d'une synchronisation réussie.
12. Test d'intégration de bout en bout avec le gestionnaire de webhooks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

try:
    import aiohttp
except ImportError:
    class _AiohttpModule:
        ClientError = Exception
    aiohttp = _AiohttpModule()  # type: ignore[assignment]

from custom_components.sevenhugs_smart_remote.client import (
    AsyncSmartRemoteClient,
    SevenhugsAuthError,
    SevenhugsConnectionError,
)
from custom_components.sevenhugs_smart_remote.const import (
    CONF_ENTITIES,
    CONF_HOST,
    CONF_PORT,
    CONF_REMOTE_ID,
    CONF_SERIAL_NUMBER,
    DOMAIN,
    EVENT_SEVENHUGS_BUTTON_PRESSED,
    WEBHOOK_ID_PREFIX,
)
from custom_components.sevenhugs_smart_remote.lua_injector import (
    DEFAULT_DOMAIN_ACTIONS,
    SevenhugsLuaInjector,
)
from custom_components.sevenhugs_smart_remote.webhook import (
    async_generate_webhook_url,
    async_handle_webhook,
    async_register_webhook,
    validate_webhook_payload,
)


class MockRequest:
    """Mock léger représentant une requête HTTP aiohttp.web.Request."""

    def __init__(
        self,
        json_data: Any = None,
        method: str = "POST",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._json_data = json_data
        self.method = method
        self.headers = headers or {"Content-Type": "application/json"}
        self.content_type = "application/json"

    async def json(self) -> Any:
        return self._json_data


class TestSevenhugsLuaInjector(unittest.IsolatedAsyncioTestCase):
    """Suite de tests unitaires et asynchrones pour SevenhugsLuaInjector."""

    async def asyncSetUp(self) -> None:
        """Initialise l'environnement de test isolé pour chaque cas."""
        self.hass = HomeAssistant()
        self.entry_id = "entry_7hugs_test_42"
        self.remote_id = "5726"
        self.serial_number = "a06fe048b48b"

        self.entry = ConfigEntry(
            entry_id=self.entry_id,
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.50",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: self.serial_number,
                CONF_REMOTE_ID: self.remote_id,
            },
            options={
                CONF_ENTITIES: [
                    "light.living_room_ceiling",
                    "switch.kitchen_plug",
                ],
            },
            unique_id=self.serial_number,
        )

        self.client = MagicMock(spec=AsyncSmartRemoteClient)
        self.client.host = "192.168.1.50"
        self.client.remote_id = self.remote_id
        self.client.keys = {CONF_REMOTE_ID: self.remote_id}
        self.client.async_push_webhooks = AsyncMock(return_value=True)

        # Pré-population du registre d'états Home Assistant
        self.hass.states.async_set(
            "light.living_room_ceiling",
            "on",
            {"friendly_name": "Plafonnier Salon", "brightness": 255},
        )
        self.hass.states.async_set(
            "switch.kitchen_plug",
            "off",
            {"friendly_name": "Prise Cafetière"},
        )
        self.hass.states.async_set(
            "scene.cinema_night",
            "scening",
            {"friendly_name": "Ambiance Cinéma"},
        )
        self.hass.states.async_set(
            "media_player.living_room_tv",
            "playing",
            {"friendly_name": "Téléviseur Salon"},
        )

        self.injector = SevenhugsLuaInjector(
            hass=self.hass,
            entry=self.entry,
            client=self.client,
        )
        self.hass.data[DOMAIN] = {self.entry_id: {"client": self.client}}

    # =========================================================================
    # 1. INITIALISATION ET CONFIGURATION
    # =========================================================================

    async def test_init_standard_attributes(self) -> None:
        """Vérifie l'assignation correcte des attributs de base de l'injecteur."""
        self.assertIs(self.injector.hass, self.hass)
        self.assertIs(self.injector.entry, self.entry)
        self.assertIs(self.injector.client, self.client)
        self.assertEqual(self.injector.remote_id, "5726")
        self.assertIsInstance(self.injector._lock, asyncio.Lock)

    async def test_init_flexible_argument_order(self) -> None:
        """Vérifie la tolérance à l'inversion d'arguments (hass, client, entry)."""
        alt_injector = SevenhugsLuaInjector(self.hass, self.client, self.entry)
        self.assertIs(alt_injector.entry, self.entry)
        self.assertIs(alt_injector.client, self.client)
        self.assertEqual(alt_injector.remote_id, "5726")

    async def test_init_remote_id_fallback_hierarchy(self) -> None:
        """Vérifie la résolution de remote_id depuis entry.data puis client."""
        # Cas 1 : remote_id dans entry.data
        inj1 = SevenhugsLuaInjector(self.hass, self.entry, self.client)
        self.assertEqual(inj1.remote_id, "5726")

        # Cas 2 : remote_id absent de entry.data, présent dans client.remote_id
        entry_no_rid = ConfigEntry(
            entry_id="sub_entry",
            domain=DOMAIN,
            title="Remote",
            data={CONF_HOST: "192.168.1.51"},
        )
        client_with_rid = MagicMock()
        client_with_rid.remote_id = "8899"
        client_with_rid.keys = {}
        inj2 = SevenhugsLuaInjector(self.hass, entry_no_rid, client_with_rid)
        self.assertEqual(inj2.remote_id, "8899")

        # Cas 3 : remote_id absent partout -> chaîne vide
        client_empty = MagicMock()
        client_empty.remote_id = None
        client_empty.keys = {}
        inj3 = SevenhugsLuaInjector(self.hass, entry_no_rid, client_empty)
        self.assertEqual(inj3.remote_id, "")

    async def test_remote_id_direct_mock_protection(self) -> None:
        """Garantit qu'un MagicMock non typé ne produit pas d'objet Mock dans remote_id."""
        raw_mock_client = MagicMock()  # Accéder à raw_mock_client.remote_id renvoie MagicMock
        entry_no_id = ConfigEntry(entry_id="sub2", domain=DOMAIN, title="R", data={})
        inj = SevenhugsLuaInjector(self.hass, entry_no_id, raw_mock_client)
        self.assertEqual(inj.remote_id, "")

    # =========================================================================
    # 2. GÉNÉRATION DE L'URL WEBHOOK
    # =========================================================================

    async def test_get_webhook_url_structure(self) -> None:
        """Vérifie que l'URL générée cible bien l'endpoint local sevenhugs_{entry_id}."""
        url = self.injector.get_webhook_url()
        expected_url = f"http://127.0.0.1:8123/api/webhook/{WEBHOOK_ID_PREFIX}{self.entry_id}"
        self.assertEqual(url, expected_url)

    async def test_get_webhook_url_matches_webhook_module(self) -> None:
        """Garantit la stricte parité avec async_generate_webhook_url de webhook.py."""
        direct_url = async_generate_webhook_url(self.hass, self.entry)
        injector_url = self.injector.get_webhook_url()
        self.assertEqual(injector_url, direct_url)

    async def test_webhook_url_property(self) -> None:
        """Vérifie que la propriété webhook_url renvoie l'URL attendue."""
        self.assertEqual(
            self.injector.webhook_url,
            f"http://127.0.0.1:8123/api/webhook/{WEBHOOK_ID_PREFIX}{self.entry_id}",
        )

    # =========================================================================
    # 3. CONSTRUCTION DE DÉCLENCHEURS (TRIGGER BUILDING PAR DOMAINE)
    # =========================================================================

    async def test_trigger_building_light_domain(self) -> None:
        """Vérifie la construction d'un déclencheur pour une lumière (action: toggle)."""
        triggers = await self.injector.async_build_triggers_from_entities(["light.living_room_ceiling"])
        self.assertEqual(len(triggers), 1)
        trig = triggers[0]

        self.assertEqual(trig["name"], "Plafonnier Salon")
        self.assertEqual(trig["method"], "POST")
        self.assertTrue(trig["url"].endswith(f"sevenhugs_{self.entry_id}"))

        body = json.loads(trig["body"])
        self.assertEqual(body["event"], "button_press")
        self.assertEqual(body["domain"], "light")
        self.assertEqual(body["action"], "toggle")
        self.assertEqual(body["entity_id"], "light.living_room_ceiling")
        self.assertEqual(body["source_remote"], "5726")

    async def test_trigger_building_switch_domain(self) -> None:
        """Vérifie la construction d'un déclencheur pour un interrupteur / prise (action: toggle)."""
        triggers = await self.injector.async_build_triggers_from_entities(["switch.kitchen_plug"])
        self.assertEqual(len(triggers), 1)
        trig = triggers[0]

        self.assertEqual(trig["name"], "Prise Cafetière")
        self.assertEqual(trig["method"], "POST")

        body = json.loads(trig["body"])
        self.assertEqual(body["domain"], "switch")
        self.assertEqual(body["action"], "toggle")
        self.assertEqual(body["entity_id"], "switch.kitchen_plug")

    async def test_trigger_building_scene_domain(self) -> None:
        """Vérifie la construction d'un déclencheur pour une scène (action: turn_on)."""
        triggers = await self.injector.async_build_triggers_from_entities(["scene.cinema_night"])
        self.assertEqual(len(triggers), 1)
        trig = triggers[0]

        self.assertEqual(trig["name"], "Ambiance Cinéma")

        body = json.loads(trig["body"])
        self.assertEqual(body["domain"], "scene")
        self.assertEqual(body["action"], "turn_on")
        self.assertEqual(body["entity_id"], "scene.cinema_night")

    async def test_trigger_building_media_player_domain(self) -> None:
        """Vérifie la construction d'un déclencheur pour un media_player (action: toggle)."""
        triggers = await self.injector.async_build_triggers_from_entities(["media_player.living_room_tv"])
        self.assertEqual(len(triggers), 1)
        trig = triggers[0]

        self.assertEqual(trig["name"], "Téléviseur Salon")

        body = json.loads(trig["body"])
        self.assertEqual(body["domain"], "media_player")
        self.assertEqual(body["action"], "toggle")
        self.assertEqual(body["entity_id"], "media_player.living_room_tv")

    async def test_trigger_building_fallback_domains(self) -> None:
        """Vérifie que les domaines additionnels supportés utilisent toggle par défaut."""
        self.hass.states.async_set("fan.ceiling", "on", {"friendly_name": "Ventilateur"})
        self.hass.states.async_set("input_boolean.away", "off", {"friendly_name": "Mode Absent"})

        triggers = await self.injector.async_build_triggers_from_entities(["fan.ceiling", "input_boolean.away"])
        self.assertEqual(len(triggers), 2)

        body_fan = json.loads(triggers[0]["body"])
        self.assertEqual(body_fan["domain"], "fan")
        self.assertEqual(body_fan["action"], "toggle")

        body_ib = json.loads(triggers[1]["body"])
        self.assertEqual(body_ib["domain"], "input_boolean")
        self.assertEqual(body_ib["action"], "toggle")

    async def test_trigger_building_unsupported_domain_skipped(self) -> None:
        """Vérifie qu'un domaine non supporté est ignoré avec warning."""
        triggers = await self.injector.async_build_triggers_from_entities(["climate.living_room_ac"])
        self.assertEqual(len(triggers), 0)

    # =========================================================================
    # 4. EXTRACTION ET REPLI DU FRIENDLY NAME
    # =========================================================================

    async def test_friendly_name_extracted_from_state_attributes(self) -> None:
        """Vérifie l'extraction prioritaire de state.attributes['friendly_name']."""
        trigger = self.injector.convert_entity_to_trigger("light.living_room_ceiling")
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["name"], "Plafonnier Salon")

    async def test_friendly_name_fallback_unregistered_entity(self) -> None:
        """Vérifie le repli gracieux vers entity_id si l'entité n'existe pas dans le registre d'états."""
        trigger = self.injector.convert_entity_to_trigger("light.garden_spotlight")
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["name"], "light.garden_spotlight")

    async def test_friendly_name_fallback_empty_attributes(self) -> None:
        """Vérifie le repli vers entity_id quand attributes est vide ou ne contient pas friendly_name."""
        self.hass.states.async_set("switch.plain_relay", "off", {})
        trigger = self.injector.convert_entity_to_trigger("switch.plain_relay")
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["name"], "switch.plain_relay")

    async def test_friendly_name_fallback_blank_or_none(self) -> None:
        """Vérifie le repli vers entity_id quand friendly_name est une chaîne blanche."""
        self.hass.states.async_set("light.blank_name", "off", {"friendly_name": "   "})
        trigger = self.injector.convert_entity_to_trigger("light.blank_name")
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["name"], "light.blank_name")

    async def test_friendly_name_fallback_non_string_attribute(self) -> None:
        """Vérifie le repli vers entity_id quand friendly_name est un type non-str (int, bool)."""
        self.hass.states.async_set("light.attr_int", "on", {"friendly_name": 12345})
        trigger = self.injector.convert_entity_to_trigger("light.attr_int")
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["name"], "light.attr_int")

    async def test_friendly_name_unicode_and_special_chars(self) -> None:
        """Vérifie la préservation des accents et emojis dans friendly_name."""
        self.hass.states.async_set(
            "light.special_lamp",
            "on",
            {"friendly_name": "Lumière Étoilée & Café ✨"},
        )
        trigger = self.injector.convert_entity_to_trigger("light.special_lamp")
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["name"], "Lumière Étoilée & Café ✨")

    async def test_friendly_name_exception_in_states_get(self) -> None:
        """Vérifie la résilience si hass.states.get lève une exception."""
        bad_states = MagicMock()
        bad_states.get.side_effect = RuntimeError("Corrupted state machine")
        self.hass.states = bad_states

        trigger = self.injector.convert_entity_to_trigger("light.faulty")
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["name"], "light.faulty")

    # =========================================================================
    # 5. VALIDATION DU FORMAT ET SCHÉMA VOLUPTUOUS DU CORPS (BODY)
    # =========================================================================

    async def test_generated_trigger_body_passes_webhook_validation(self) -> None:
        """Garantit que le JSON généré satisfait strictement le validateur voluptuous de webhook.py."""
        entity_ids = [
            "light.living_room_ceiling",
            "switch.kitchen_plug",
            "scene.cinema_night",
            "media_player.living_room_tv",
        ]
        triggers = await self.injector.async_build_triggers_from_entities(entity_ids)
        self.assertEqual(len(triggers), 4)

        for trig in triggers:
            raw_body = trig["body"]
            self.assertIsInstance(raw_body, str)
            payload = json.loads(raw_body)
            # Doit passer sans lever vol.Invalid
            validated = validate_webhook_payload(payload)
            self.assertIn("domain", validated)
            self.assertIn("action", validated)
            self.assertIn("entity_id", validated)
            self.assertIn("event", validated)
            self.assertEqual(validated["event"], "button_press")

    async def test_generated_trigger_body_json_parsable(self) -> None:
        """Vérifie que le corps du trigger est un JSON valide et contient tous les champs clés."""
        trigger = self.injector.convert_entity_to_trigger("light.living_room_ceiling")
        self.assertIsNotNone(trigger)
        payload = json.loads(trigger["body"])
        self.assertEqual(payload["event"], "button_press")
        self.assertEqual(payload["domain"], "light")
        self.assertEqual(payload["action"], "toggle")
        self.assertEqual(payload["entity_id"], "light.living_room_ceiling")
        self.assertEqual(payload["source_remote"], "5726")

    # =========================================================================
    # 6. GESTION DES LISTES VIDES ET ENTITÉS MALFORMÉES
    # =========================================================================

    async def test_build_triggers_empty_list(self) -> None:
        """Vérifie qu'une liste vide d'entités retourne immédiatement une liste vide."""
        triggers = await self.injector.async_build_triggers_from_entities([])
        self.assertEqual(triggers, [])

    async def test_build_triggers_none_input(self) -> None:
        """Vérifie qu'une entrée None retourne une liste vide."""
        triggers = await self.injector.async_build_triggers_from_entities(None)
        self.assertEqual(triggers, [])

    async def test_build_triggers_skips_malformed_entries(self) -> None:
        """Vérifie que les éléments non valides (None, sans point, vide) sont ignorés sans crash."""
        bad_entities = ["", "invalid_no_dot", None, 12345, ".leading_dot", "trailing_dot.", "light.living_room_ceiling"]
        triggers = await self.injector.async_build_triggers_from_entities(bad_entities)  # type: ignore[arg-type]
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["name"], "Plafonnier Salon")

    async def test_build_triggers_deduplication_preserves_order(self) -> None:
        """Vérifie la déduplication des entités tout en préservant l'ordre."""
        entities = [
            "light.living_room_ceiling",
            "switch.kitchen_plug",
            "light.living_room_ceiling",
            "switch.kitchen_plug",
        ]
        triggers = await self.injector.async_build_triggers_from_entities(entities)
        self.assertEqual(len(triggers), 2)
        self.assertEqual(triggers[0]["name"], "Plafonnier Salon")
        self.assertEqual(triggers[1]["name"], "Prise Cafetière")

    async def test_build_triggers_case_insensitivity(self) -> None:
        """Vérifie la tolérance à la casse des entités."""
        triggers = await self.injector.async_build_triggers_from_entities(["LIGHT.LIVING_ROOM_CEILING"])
        self.assertEqual(len(triggers), 1)
        body = json.loads(triggers[0]["body"])
        self.assertEqual(body["domain"], "light")
        self.assertEqual(body["action"], "toggle")

    async def test_build_triggers_string_passed_instead_of_list(self) -> None:
        """Vérifie qu'une chaîne passée directement retourne une liste vide."""
        triggers = self.injector.async_build_triggers_from_entities("light.living_room_ceiling")
        self.assertEqual(triggers, [])

    async def test_build_triggers_awaitable_and_sync_compatible(self) -> None:
        """Vérifie que la méthode peut être appelée avec await ou en synchrone."""
        # Appel synchrone direct
        res_sync = self.injector.async_build_triggers_from_entities(["light.living_room_ceiling"])
        self.assertEqual(len(res_sync), 1)

        # Appel avec await
        res_async = await self.injector.async_build_triggers_from_entities(["light.living_room_ceiling"])
        self.assertEqual(len(res_async), 1)

    # =========================================================================
    # 7. SYNCHRONISATION ASYNCHRONE (async_sync_entities)
    # =========================================================================

    async def test_async_sync_entities_explicit_list_success(self) -> None:
        """Vérifie la synchronisation réussie avec une liste explicite d'entités."""
        res = await self.injector.async_sync_entities(["light.living_room_ceiling", "switch.kitchen_plug"])
        self.assertTrue(res)
        self.client.async_push_webhooks.assert_awaited_once()

        call_args = self.client.async_push_webhooks.call_args[0][0]
        self.assertEqual(len(call_args), 2)
        self.assertEqual(call_args[0]["name"], "Plafonnier Salon")
        self.assertEqual(call_args[1]["name"], "Prise Cafetière")

    async def test_async_sync_entities_from_options_success(self) -> None:
        """Vérifie la synchronisation automatique depuis entry.options[CONF_ENTITIES]."""
        self.entry.options = {
            CONF_ENTITIES: [
                "scene.cinema_night",
                "media_player.living_room_tv",
            ]
        }
        res = await self.injector.async_sync_entities()
        self.assertTrue(res)
        self.client.async_push_webhooks.assert_awaited_once()

        call_args = self.client.async_push_webhooks.call_args[0][0]
        self.assertEqual(len(call_args), 2)
        self.assertEqual(call_args[0]["name"], "Ambiance Cinéma")
        self.assertEqual(call_args[1]["name"], "Téléviseur Salon")

    async def test_async_sync_entities_from_data_success(self) -> None:
        """Vérifie le repli sur entry.data[CONF_ENTITIES] si absent des options."""
        self.entry.options = {}
        self.entry.data[CONF_ENTITIES] = ["light.living_room_ceiling"]
        res = await self.injector.async_sync_entities()
        self.assertTrue(res)
        self.client.async_push_webhooks.assert_awaited_once()

    async def test_async_sync_entities_scalar_string_coercion(self) -> None:
        """Vérifie qu'un entity_id passé sous forme de str scalaire est converti en liste."""
        res = await self.injector.async_sync_entities("light.living_room_ceiling")  # type: ignore[arg-type]
        self.assertTrue(res)
        self.client.async_push_webhooks.assert_awaited_once()

    async def test_async_sync_entities_empty_explicit_list(self) -> None:
        """Vérifie qu'une liste explicite vide n'appelle pas le client et retourne True."""
        res = await self.injector.async_sync_entities([])
        self.assertTrue(res)
        self.client.async_push_webhooks.assert_not_called()

    async def test_async_sync_entities_empty_options(self) -> None:
        """Vérifie que des options vides n'appellent pas le client et retournent True."""
        self.entry.options = {CONF_ENTITIES: []}
        res = await self.injector.async_sync_entities()
        self.assertTrue(res)
        self.client.async_push_webhooks.assert_not_called()

    async def test_async_sync_entities_missing_options_key(self) -> None:
        """Vérifie le comportement quand CONF_ENTITIES est absent de entry.options."""
        self.entry.options = {}
        self.entry.data.pop(CONF_ENTITIES, None)
        res = await self.injector.async_sync_entities()
        self.assertTrue(res)
        self.client.async_push_webhooks.assert_not_called()

    async def test_async_sync_entities_none_entry_options(self) -> None:
        """Vérifie le comportement quand entry.options est None."""
        self.entry.options = None
        self.entry.data.pop(CONF_ENTITIES, None)
        res = await self.injector.async_sync_entities()
        self.assertTrue(res)
        self.client.async_push_webhooks.assert_not_called()

    async def test_async_sync_entities_returns_client_result_boolean(self) -> None:
        """Vérifie la propagation fidèle d'un retour False du client."""
        self.client.async_push_webhooks.return_value = False
        res = await self.injector.async_sync_entities(["light.living_room_ceiling"])
        self.assertFalse(res)

    # =========================================================================
    # 8. RÉSILIENCE AUX ERREURS (EXCEPTION HANDLING)
    # =========================================================================

    async def test_async_sync_entities_handles_sevenhugs_connection_error(self) -> None:
        """Vérifie que SevenhugsConnectionError est intercepté et retourne False sans crash."""
        self.client.async_push_webhooks.side_effect = SevenhugsConnectionError("Remote offline")
        res = await self.injector.async_sync_entities(["light.living_room_ceiling"])
        self.assertFalse(res)

    async def test_async_sync_entities_handles_asyncio_timeout_error(self) -> None:
        """Vérifie que asyncio.TimeoutError est intercepté et retourne False."""
        self.client.async_push_webhooks.side_effect = asyncio.TimeoutError()
        res = await self.injector.async_sync_entities(["light.living_room_ceiling"])
        self.assertFalse(res)

    async def test_async_sync_entities_handles_builtin_timeout_error(self) -> None:
        """Vérifie que built-in TimeoutError est intercepté et retourne False."""
        self.client.async_push_webhooks.side_effect = TimeoutError("Socket timeout")
        res = await self.injector.async_sync_entities(["light.living_room_ceiling"])
        self.assertFalse(res)

    async def test_async_sync_entities_handles_sevenhugs_auth_error(self) -> None:
        """Vérifie que SevenhugsAuthError est intercepté et retourne False."""
        self.client.async_push_webhooks.side_effect = SevenhugsAuthError("Session invalidated")
        res = await self.injector.async_sync_entities(["light.living_room_ceiling"])
        self.assertFalse(res)

    async def test_async_sync_entities_handles_client_error(self) -> None:
        """Vérifie que aiohttp.ClientError est intercepté et retourne False."""
        self.client.async_push_webhooks.side_effect = aiohttp.ClientError("TCP broken")
        res = await self.injector.async_sync_entities(["light.living_room_ceiling"])
        self.assertFalse(res)

    async def test_async_sync_entities_handles_unexpected_exception(self) -> None:
        """Vérifie qu'une exception inattendue est interceptée et retourne False sans propager."""
        self.client.async_push_webhooks.side_effect = RuntimeError("Panic in emulator")
        res = await self.injector.async_sync_entities(["light.living_room_ceiling"])
        self.assertFalse(res)

    async def test_async_sync_entities_re_raises_cancelled_error(self) -> None:
        """Vérifie que asyncio.CancelledError est bien propagé sans être avalé."""
        self.client.async_push_webhooks.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.injector.async_sync_entities(["light.living_room_ceiling"])

    # =========================================================================
    # 9. SÉRIALISATION PAR LOCK ET NOTIFICATION COORDINATEUR
    # =========================================================================

    async def test_async_sync_entities_serializes_with_lock(self) -> None:
        """Vérifie que deux appels simultanés sont sérialisés via le verrou asyncio.Lock."""
        order: list[str] = []

        async def slow_push(triggers):
            order.append("start")
            await asyncio.sleep(0.02)
            order.append("end")
            return True

        self.client.async_push_webhooks.side_effect = slow_push

        task1 = asyncio.create_task(self.injector.async_sync_entities(["light.living_room_ceiling"]))
        task2 = asyncio.create_task(self.injector.async_sync_entities(["switch.kitchen_plug"]))

        res1, res2 = await asyncio.gather(task1, task2)
        self.assertTrue(res1)
        self.assertTrue(res2)
        self.assertEqual(order, ["start", "end", "start", "end"])

    async def test_coordinator_wake_up_notified_on_successful_sync(self) -> None:
        """Vérifie que coordinator.async_handle_wake_up est planifié lors d'une synchronisation réussie."""
        mock_coordinator = MagicMock()
        mock_coordinator.async_handle_wake_up = AsyncMock()

        self.hass.data[DOMAIN][self.entry_id]["coordinator"] = mock_coordinator

        res = await self.injector.async_sync_entities(["light.living_room_ceiling"])
        self.assertTrue(res)

        # La tâche de réveil est soumise via hass.async_create_task
        await asyncio.sleep(0.01)
        mock_coordinator.async_handle_wake_up.assert_called_once_with("lua_sync")

    # =========================================================================
    # 10. INTÉGRATION DE BOUT EN BOUT AVEC LE WEBHOOK HANDLER
    # =========================================================================

    async def test_e2e_injector_trigger_dispatches_service_via_webhook(self) -> None:
        """Vérifie le cycle complet : Trigger généré -> POST vers webhook -> Exécution du service HA."""
        # 1. Enregistrement du webhook local dans Home Assistant
        registered_id = async_register_webhook(self.hass, self.entry)
        self.assertEqual(registered_id, f"sevenhugs_{self.entry_id}")

        # 2. Génération du déclencheur via l'injecteur
        triggers = await self.injector.async_build_triggers_from_entities(["light.living_room_ceiling"])
        self.assertEqual(len(triggers), 1)
        trigger = triggers[0]

        # 3. Simulation de la télécommande qui poste le body sur le webhook
        req_payload = json.loads(trigger["body"])
        req = MockRequest(json_data=req_payload, method="POST")

        resp = await async_handle_webhook(self.hass, registered_id, req)
        self.assertEqual(resp.status, 200)

        # 4. Vérification de l'appel du service light.toggle
        calls = [c for c in self.hass.services.calls if c.domain == "light" and c.service == "toggle"]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.data.get("entity_id"), "light.living_room_ceiling")

        # 5. Vérification de l'émission d'événement sur le bus
        events = [e for e in self.hass.bus.events if e.event_type == EVENT_SEVENHUGS_BUTTON_PRESSED]
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.data.get("domain"), "light")
        self.assertEqual(ev.data.get("action"), "toggle")
        self.assertEqual(ev.data.get("entity_id"), "light.living_room_ceiling")
        self.assertEqual(ev.data.get("remote_id"), "5726")


if __name__ == "__main__":
    unittest.main()
