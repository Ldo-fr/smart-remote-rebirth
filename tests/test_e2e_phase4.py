"""Banc d'Essais d'Intégration de Bout en Bout (E2E) — Phase 4.

Valide l'architecture complète d'intégration de la Sevenhugs Smart Remote (SR1A)
selon les spécifications de la Phase 4 réparties sur 4 paliers (Tiers 1 à 4) :
- Tier 1 : Couverture fonctionnelle nominale (Webhooks, Services, Bus, Lua Injector, Options Flow)
- Tier 2 : Conditions aux limites & Tolérance aux pannes (Payloads corrompus, 404/405, Scalabilité, Veille)
- Tier 3 : Interactions multi-composants (Boucle fermée bidirectionnelle, Rechargement concurrent, Reconfig/Reauth)
- Tier 4 : Scénarios réels & Charge utilisateur (Multi-pièces salon/cuisine/scène/TV, Réveil de veille événementiel)
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

# Inscription du répertoire racine du projet dans le path d'importation
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from tests.conftest import MockSmartRemoteServer
from custom_components.smart_remote_rebirth import (
    DOMAIN,
    PLATFORMS,
    AsyncSmartRemoteClient,
    SevenhugsAuthError,
    SevenhugsConnectionError,
    SevenhugsData,
    SevenhugsDataUpdateCoordinator,
    SevenhugsLuaInjector,
    async_setup_entry,
    async_unload_entry,
    update_listener,
)
from custom_components.smart_remote_rebirth.config_flow import (
    SevenhugsConfigFlow,
    SevenhugsOptionsFlowHandler,
)
from custom_components.smart_remote_rebirth.const import (
    CONF_AUTO_SYNC_WEBHOOKS,
    CONF_CLIENT_PRIVATE_KEY,
    CONF_CLIENT_PUBLIC_KEY,
    CONF_CONTROLLER_ID,
    CONF_ENTITIES,
    CONF_HOST,
    CONF_POLLING_INTERVAL,
    CONF_PORT,
    CONF_REMOTE_ID,
    CONF_REMOTE_PUBLIC_KEY,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_READ_KEY,
    CONF_SESSION_WRITE_KEY,
    CONF_TIMEOUT,
    CONF_TRIGGER_RESYNC,
    DEFAULT_AUTO_SYNC_WEBHOOKS,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    EVENT_SEVENHUGS_BUTTON_PRESSED,
    WEBHOOK_ID_PREFIX,
)
from custom_components.smart_remote_rebirth.webhook import (
    async_generate_webhook_url,
    async_get_webhook_id,
    async_handle_webhook,
    async_register_webhook,
    async_unregister_webhook,
    validate_webhook_payload,
)


class MockRequest:
    """Mock léger d'une requête HTTP aiohttp.web.Request."""

    def __init__(
        self,
        json_data: Any = None,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        raise_json: Exception | None = None,
        content_type: str = "application/json",
    ) -> None:
        self._json_data = json_data
        self.method = method
        self.headers = headers or {"Content-Type": content_type}
        self.content_type = content_type
        self._raise_json = raise_json

    async def json(self) -> Any:
        if self._raise_json is not None:
            raise self._raise_json
        return self._json_data


def create_mock_entry(
    entry_id: str,
    server: MockSmartRemoteServer,
    options: dict[str, Any] | None = None,
    serial: str | None = None,
) -> ConfigEntry:
    """Génère une ConfigEntry liée à une instance de MockSmartRemoteServer."""
    return ConfigEntry(
        entry_id=entry_id,
        domain=DOMAIN,
        title=f"Sevenhugs Remote ({entry_id})",
        data={
            CONF_HOST: server.host,
            CONF_PORT: server.port,
            CONF_SERIAL_NUMBER: serial or f"SN_{entry_id}",
            CONF_REMOTE_ID: "5726",
            CONF_CONTROLLER_ID: f"ctrl_{entry_id}",
            CONF_CLIENT_PRIVATE_KEY: "01" * 32,
            CONF_CLIENT_PUBLIC_KEY: "02" * 32,
            CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
            CONF_SESSION_WRITE_KEY: "03" * 32,
            CONF_SESSION_READ_KEY: "04" * 32,
        },
        options=options or {},
    )


# ============================================================================
# TIER 1 : COUVERTURE FONCTIONNELLE NOMINALE (CORE FEATURE CONTRACTS)
# ============================================================================

class TestPhase4Tier1FeatureCoverage(unittest.IsolatedAsyncioTestCase):
    """Vérifications E2E Tier 1 : Fonctionnalités de base de la Phase 4."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.server = MockSmartRemoteServer(pair_setup_delay=0.01)
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def test_tier1_webhook_local_registration_and_routing(self) -> None:
        """Vérifie l'enregistrement formel du webhook local : ID, local_only, méthodes POST/PUT."""
        entry = create_mock_entry("t1_reg_01", self.server)
        self.hass.config_entries.async_add(entry)

        setup_ok = await async_setup_entry(self.hass, entry)
        self.assertTrue(setup_ok)

        expected_webhook_id = f"smart_remote_{entry.entry_id}"
        self.assertTrue(self.hass.webhook.has_webhook(expected_webhook_id))

        wh_info = self.hass.webhook.get_webhook(expected_webhook_id)
        self.assertIsNotNone(wh_info)
        self.assertEqual(wh_info.domain, DOMAIN)
        self.assertEqual(wh_info.webhook_id, expected_webhook_id)
        self.assertTrue(wh_info.local_only)
        self.assertIn("POST", wh_info.allowed_methods)
        self.assertIn("PUT", wh_info.allowed_methods)

        gen_url = async_generate_webhook_url(self.hass, entry)
        self.assertIn(f"/api/webhook/{expected_webhook_id}", gen_url)

        unload_ok = await async_unload_entry(self.hass, entry)
        self.assertTrue(unload_ok)
        self.assertFalse(self.hass.webhook.has_webhook(expected_webhook_id))

    async def test_tier1_inbound_payload_processing_light_turn_on(self) -> None:
        """Vérifie le traitement entrant d'un bouton 'light.turn_on' avec paramètres détaillés."""
        entry = create_mock_entry("t1_light_01", self.server)
        self.hass.config_entries.async_add(entry)
        await async_setup_entry(self.hass, entry)
        wh_id = f"smart_remote_{entry.entry_id}"

        payload = {
            "domain": "light",
            "action": "turn_on",
            "entity_id": "light.living_room_ceiling",
            "params": {"brightness": 204, "rgb_color": [255, 128, 0]},
        }
        req = MockRequest(json_data=payload)
        resp = await self.hass.webhook.async_handle_webhook(wh_id, req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "OK")

        self.assertEqual(len(self.hass.services.calls), 1)
        call = self.hass.services.calls[0]
        self.assertEqual(call.domain, "light")
        self.assertEqual(call.service, "turn_on")
        self.assertEqual(call.data["entity_id"], "light.living_room_ceiling")
        self.assertEqual(call.data["brightness"], 204)
        self.assertEqual(call.data["rgb_color"], [255, 128, 0])

        await async_unload_entry(self.hass, entry)

    async def test_tier1_inbound_payload_processing_switch_toggle_multi_entities(self) -> None:
        """Vérifie le traitement d'une commande 'switch.toggle' visant une liste d'entités."""
        entry = create_mock_entry("t1_switch_01", self.server)
        self.hass.config_entries.async_add(entry)
        await async_setup_entry(self.hass, entry)
        wh_id = f"smart_remote_{entry.entry_id}"

        target_entities = ["switch.kitchen_plug", "switch.coffee_maker"]
        payload = {
            "domain": "switch",
            "action": "toggle",
            "entity_id": target_entities,
        }
        req = MockRequest(json_data=payload)
        resp = await self.hass.webhook.async_handle_webhook(wh_id, req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.hass.services.calls), 1)
        call = self.hass.services.calls[0]
        self.assertEqual(call.domain, "switch")
        self.assertEqual(call.service, "toggle")
        self.assertEqual(call.data["entity_id"], target_entities)

        await async_unload_entry(self.hass, entry)

    async def test_tier1_inbound_payload_processing_scene_turn_on(self) -> None:
        """Vérifie le déclenchement d'une scène 'scene.turn_on'."""
        entry = create_mock_entry("t1_scene_01", self.server)
        self.hass.config_entries.async_add(entry)
        await async_setup_entry(self.hass, entry)
        wh_id = f"smart_remote_{entry.entry_id}"

        payload = {
            "domain": "scene",
            "action": "turn_on",
            "entity_id": "scene.movie_night",
            "parameters": {"transition": 2.5},
        }
        req = MockRequest(json_data=payload)
        resp = await self.hass.webhook.async_handle_webhook(wh_id, req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.hass.services.calls), 1)
        call = self.hass.services.calls[0]
        self.assertEqual(call.domain, "scene")
        self.assertEqual(call.service, "turn_on")
        self.assertEqual(call.data["entity_id"], "scene.movie_night")
        self.assertEqual(call.data["transition"], 2.5)

        await async_unload_entry(self.hass, entry)

    async def test_tier1_inbound_payload_processing_custom_media_player_service(self) -> None:
        """Vérifie l'exécution d'un service multimédia avec clé 'service' au lieu de 'action'."""
        entry = create_mock_entry("t1_media_01", self.server)
        self.hass.config_entries.async_add(entry)
        await async_setup_entry(self.hass, entry)
        wh_id = f"smart_remote_{entry.entry_id}"

        payload = {
            "domain": "media_player",
            "service": "media_play_pause",
            "entity_id": "media_player.living_room_tv",
        }
        req = MockRequest(json_data=payload)
        resp = await self.hass.webhook.async_handle_webhook(wh_id, req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.hass.services.calls), 1)
        call = self.hass.services.calls[0]
        self.assertEqual(call.domain, "media_player")
        self.assertEqual(call.service, "media_play_pause")
        self.assertEqual(call.data["entity_id"], "media_player.living_room_tv")

        await async_unload_entry(self.hass, entry)

    async def test_tier1_inbound_payload_action_with_domain_prefix_normalization(self) -> None:
        """Vérifie la normalisation automatique lorsque l'action contient le préfixe domaine."""
        entry = create_mock_entry("t1_prefix_01", self.server)
        self.hass.config_entries.async_add(entry)
        await async_setup_entry(self.hass, entry)
        wh_id = f"smart_remote_{entry.entry_id}"

        # Action passée sous la forme "light.turn_off" au lieu de "turn_off"
        payload = {
            "domain": "light",
            "action": "light.turn_off",
            "entity_id": "light.hallway",
        }
        req = MockRequest(json_data=payload)
        resp = await self.hass.webhook.async_handle_webhook(wh_id, req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.hass.services.calls), 1)
        call = self.hass.services.calls[0]
        self.assertEqual(call.domain, "light")
        self.assertEqual(call.service, "turn_off")  # Préfixe retiré avec succès
        self.assertEqual(call.data["entity_id"], "light.hallway")

        await async_unload_entry(self.hass, entry)

    async def test_tier1_event_bus_emission_contract(self) -> None:
        """Vérifie l'émission systématique de 'sevenhugs_button_pressed' avec toutes ses métadonnées."""
        entry = create_mock_entry("t1_bus_01", self.server)
        self.hass.config_entries.async_add(entry)
        await async_setup_entry(self.hass, entry)
        wh_id = f"smart_remote_{entry.entry_id}"

        received_events = []
        self.hass.bus.async_listen(
            EVENT_SEVENHUGS_BUTTON_PRESSED,
            lambda ev: received_events.append(ev),
        )

        payload = {
            "domain": "light",
            "action": "toggle",
            "entity_id": "light.desk_lamp",
            "custom_meta": "remote_btn_03",
        }
        req = MockRequest(json_data=payload)
        resp = await self.hass.webhook.async_handle_webhook(wh_id, req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(len(received_events), 1)
        event = received_events[0]
        self.assertEqual(event.event_type, EVENT_SEVENHUGS_BUTTON_PRESSED)
        self.assertEqual(event.data["entry_id"], entry.entry_id)
        self.assertEqual(event.data["remote_id"], "5726")
        self.assertEqual(event.data["serial_number"], f"SN_{entry.entry_id}")
        self.assertEqual(event.data["webhook_id"], wh_id)
        self.assertEqual(event.data["domain"], "light")
        self.assertEqual(event.data["action"], "toggle")
        self.assertEqual(event.data["entity_id"], "light.desk_lamp")
        self.assertEqual(event.data["custom_meta"], "remote_btn_03")

        await async_unload_entry(self.hass, entry)

    async def test_tier1_lua_injector_wizard_automation_sequence(self) -> None:
        """Vérifie le déroulement exact de la machine à états de l'assistant Lua vers la télécommande."""
        entry = create_mock_entry("t1_lua_01", self.server)
        self.hass.config_entries.async_add(entry)
        await async_setup_entry(self.hass, entry)

        self.hass.states.async_set("light.living_room", "off", {"friendly_name": "Lumière Salon"})
        self.hass.states.async_set("switch.boiler", "on", {"friendly_name": "Chauffe-Eau"})

        runtime_data = self.hass.data[DOMAIN][entry.entry_id]
        injector: SevenhugsLuaInjector = runtime_data.injector

        # Réinitialisation de la trace du serveur
        self.server.lua_steps_called.clear()

        # Synchronisation explicite de 2 entités
        sync_ok = await injector.async_sync_entities(["light.living_room", "switch.boiler"])
        self.assertTrue(sync_ok)
        self.assertFalse(injector._pending_resync)

        # Vérification de la séquence complète d'états
        expected_sequence = [
            "connect",
            "main_page",
            "add_choice",
            "advanced_add_edit",
            "post_advanced_add_edit",
            "add_choice",
            "advanced_add_edit",
            "post_advanced_add_edit",
            "done",
        ]
        self.assertEqual(self.server.lua_steps_called, expected_sequence)

        await async_unload_entry(self.hass, entry)

    async def test_tier1_options_flow_entity_selection_and_resync_trigger(self) -> None:
        """Vérifie que l'Options Flow expose les clés requises et génère les données de configuration."""
        entry = create_mock_entry("t1_opt_01", self.server)
        self.hass.config_entries.async_add(entry)

        flow = SevenhugsOptionsFlowHandler(entry)
        flow.hass = self.hass

        # 1. Étape initiale sans saisie : affiche le schéma avec les champs obligatoires et optionnels
        step1 = await flow.async_step_init()
        self.assertEqual(step1["type"], FlowResultType.FORM)
        self.assertEqual(step1["step_id"], "init")
        schema = step1["data_schema"]
        schema_keys = [k.schema if hasattr(k, "schema") else k for k in schema.schema.keys()]
        self.assertIn(CONF_ENTITIES, schema_keys)
        self.assertIn(CONF_TRIGGER_RESYNC, schema_keys)
        self.assertIn(CONF_POLLING_INTERVAL, schema_keys)
        self.assertIn(CONF_AUTO_SYNC_WEBHOOKS, schema_keys)
        self.assertIn(CONF_TIMEOUT, schema_keys)

        # 2. Soumission d'options modifiées
        user_input = {
            CONF_ENTITIES: ["light.garden_spot", "switch.irrigation"],
            CONF_TRIGGER_RESYNC: True,
            CONF_POLLING_INTERVAL: 40,
            CONF_AUTO_SYNC_WEBHOOKS: True,
            CONF_TIMEOUT: 12,
        }
        step2 = await flow.async_step_init(user_input=user_input)
        self.assertEqual(step2["type"], FlowResultType.CREATE_ENTRY)
        self.assertEqual(step2["data"][CONF_ENTITIES], ["light.garden_spot", "switch.irrigation"])
        self.assertTrue(step2["data"][CONF_TRIGGER_RESYNC])
        self.assertEqual(step2["data"][CONF_POLLING_INTERVAL], 40)


# ============================================================================
# TIER 2 : CONDITIONS AUX LIMITES ET RÉSILIENCE (BOUNDARY & FAULT INJECTION)
# ============================================================================

class TestPhase4Tier2BoundaryAndCornerCases(unittest.IsolatedAsyncioTestCase):
    """Vérifications E2E Tier 2 : Robustesse, pannes, et conditions aux limites."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.server = MockSmartRemoteServer(pair_setup_delay=0.01)
        await self.server.start()
        self.entry = create_mock_entry("t2_entry", self.server)
        self.hass.config_entries.async_add(self.entry)
        await async_setup_entry(self.hass, self.entry)
        self.wh_id = f"smart_remote_{self.entry.entry_id}"

    async def asyncTearDown(self) -> None:
        if self.hass.webhook.has_webhook(self.wh_id):
            await async_unload_entry(self.hass, self.entry)
        await self.server.stop()

    async def test_tier2_malformed_json_syntax_returns_400(self) -> None:
        """Vérifie qu'une syntaxe JSON corrompue retourne HTTP 400 sans appeler de service."""
        req = MockRequest(raise_json=ValueError("Unterminated string starting at line 1"))
        resp = await self.hass.webhook.async_handle_webhook(self.wh_id, req)

        self.assertEqual(resp.status, 400)
        self.assertIn("Invalid JSON payload", resp.text)
        self.assertEqual(len(self.hass.services.calls), 0)
        self.assertEqual(len(self.hass.bus.events), 0)

    async def test_tier2_non_dict_json_payloads_return_400(self) -> None:
        """Vérifie que les types JSON non-dictionnaires sont rejetés avec HTTP 400."""
        for invalid_body in (["item1", "item2"], "raw string", 42, True):
            with self.subTest(invalid_body=invalid_body):
                req = MockRequest(json_data=invalid_body)
                resp = await self.hass.webhook.async_handle_webhook(self.wh_id, req)
                self.assertEqual(resp.status, 400)
                self.assertIn("Invalid payload schema", resp.text)
        self.assertEqual(len(self.hass.services.calls), 0)

    async def test_tier2_missing_required_domain_returns_400(self) -> None:
        """Vérifie que l'omission de la clé 'domain' est rejetée avec HTTP 400."""
        req = MockRequest(json_data={"action": "turn_on", "entity_id": "light.bulb"})
        resp = await self.hass.webhook.async_handle_webhook(self.wh_id, req)

        self.assertEqual(resp.status, 400)
        self.assertIn("Required key missing: domain", resp.text)
        self.assertEqual(len(self.hass.services.calls), 0)

    async def test_tier2_missing_action_and_service_returns_400(self) -> None:
        """Vérifie que l'absence conjointe de 'action' et 'service' est rejetée avec HTTP 400."""
        req = MockRequest(json_data={"domain": "light", "entity_id": "light.bulb"})
        resp = await self.hass.webhook.async_handle_webhook(self.wh_id, req)

        self.assertEqual(resp.status, 400)
        self.assertIn("Missing or invalid required field: 'action' or 'service'", resp.text)
        self.assertEqual(len(self.hass.services.calls), 0)

    async def test_tier2_disallowed_http_methods_return_405(self) -> None:
        """Vérifie que les méthodes non autorisées (GET, DELETE) sont rejetées avec HTTP 405."""
        for disallowed_method in ("GET", "DELETE"):
            with self.subTest(method=disallowed_method):
                req = MockRequest(json_data={"domain": "light", "action": "toggle"}, method=disallowed_method)
                resp = await self.hass.webhook.async_handle_webhook(self.wh_id, req)
                self.assertEqual(resp.status, 405)
                self.assertIn("not allowed", resp.text)
        self.assertEqual(len(self.hass.services.calls), 0)

    async def test_tier2_webhook_call_post_unload_returns_404(self) -> None:
        """Vérifie qu'un appel vers le webhook après déchargement de l'entrée renvoie HTTP 404."""
        unload_ok = await async_unload_entry(self.hass, self.entry)
        self.assertTrue(unload_ok)
        self.assertFalse(self.hass.webhook.has_webhook(self.wh_id))

        req = MockRequest(json_data={"domain": "light", "action": "turn_on"})
        resp = await self.hass.webhook.async_handle_webhook(self.wh_id, req)

        self.assertEqual(resp.status, 404)
        self.assertIn("not found", resp.text)
        self.assertEqual(len(self.hass.services.calls), 0)

    async def test_tier2_double_webhook_unregistration_idempotence(self) -> None:
        """Vérifie l'idempotence stricte du désenregistrement du webhook (appels répétés tolérés)."""
        async_unregister_webhook(self.hass, self.entry)
        self.assertFalse(self.hass.webhook.has_webhook(self.wh_id))

        # Appels répétés sans exception
        async_unregister_webhook(self.hass, self.entry)
        async_unregister_webhook(self.hass, self.entry)
        async_unregister_webhook(self.hass, "non_existent_entry_id")

    async def test_tier2_lua_injector_zero_entities_graceful_handling(self) -> None:
        """Vérifie que la synchronisation d'une liste vide (0 boutons) réussit sans requête réseau."""
        runtime_data = self.hass.data[DOMAIN][self.entry.entry_id]
        injector: SevenhugsLuaInjector = runtime_data.injector

        self.server.lua_steps_called.clear()
        sync_ok = await injector.async_sync_entities([])

        self.assertTrue(sync_ok)
        self.assertFalse(injector._pending_resync)
        # Aucune interaction avec la machine à états Lua
        self.assertEqual(len(self.server.lua_steps_called), 0)

    async def test_tier2_lua_injector_hundred_entities_scalability(self) -> None:
        """Vérifie la montée en charge de l'injection Lua avec 100 entités configurées."""
        runtime_data = self.hass.data[DOMAIN][self.entry.entry_id]
        injector: SevenhugsLuaInjector = runtime_data.injector

        entities_100 = [f"light.spot_{i:03d}" for i in range(100)]
        self.server.lua_steps_called.clear()

        sync_ok = await injector.async_sync_entities(entities_100)
        self.assertTrue(sync_ok)
        self.assertFalse(injector._pending_resync)

        # 1 connect + 1 main_page + 100 * (add_choice + advanced_add_edit + post_advanced_add_edit) + 1 done = 303
        self.assertEqual(len(self.server.lua_steps_called), 303)
        self.assertEqual(self.server.lua_steps_called[0], "connect")
        self.assertEqual(self.server.lua_steps_called[1], "main_page")
        self.assertEqual(self.server.lua_steps_called[-1], "done")

    async def test_tier2_lua_injector_unsupported_domains_filtered_gracefully(self) -> None:
        """Vérifie que les domaines non supportés (climate, vacuum, camera) sont ignorés sans planter."""
        runtime_data = self.hass.data[DOMAIN][self.entry.entry_id]
        injector: SevenhugsLuaInjector = runtime_data.injector

        mixed_entities = [
            "climate.living_room_ac",
            "vacuum.roborock_s7",
            "camera.front_door",
            "light.desk_lamp",
            "switch.kitchen_fan",
        ]
        self.server.lua_steps_called.clear()

        sync_ok = await injector.async_sync_entities(mixed_entities)
        self.assertTrue(sync_ok)

        # Seuls light et switch doivent être injectés (2 triggers -> 2 * 3 + 3 = 9 étapes)
        self.assertEqual(len(self.server.lua_steps_called), 9)
        self.assertEqual(self.server.lua_steps_called[0], "connect")
        self.assertEqual(self.server.lua_steps_called[-1], "done")

    async def test_tier2_sleep_mode_remote_offline_during_sync(self) -> None:
        """Vérifie l'absorption gracieuse d'une perte réseau ou mise en veille profonde pendant le push."""
        runtime_data = self.hass.data[DOMAIN][self.entry.entry_id]
        injector: SevenhugsLuaInjector = runtime_data.injector

        with patch.object(
            runtime_data.client,
            "async_push_webhooks",
            side_effect=SevenhugsConnectionError("Host unreachable in deep sleep"),
        ):
            sync_ok = await injector.async_sync_entities(["light.living_room"])
            self.assertFalse(sync_ok)
            # Marque la nécessité d'une resynchronisation ultérieure
            self.assertTrue(injector._pending_resync)

    async def test_tier2_sleep_mode_timeout_during_sync(self) -> None:
        """Vérifie l'absorption d'un timeout asyncio lors de la synchronisation Lua."""
        runtime_data = self.hass.data[DOMAIN][self.entry.entry_id]
        injector: SevenhugsLuaInjector = runtime_data.injector

        with patch.object(
            runtime_data.client,
            "async_push_webhooks",
            side_effect=asyncio.TimeoutError("Socket read timed out"),
        ):
            sync_ok = await injector.async_sync_entities(["switch.heater"])
            self.assertFalse(sync_ok)
            self.assertTrue(injector._pending_resync)

    async def test_tier2_auth_failure_during_sync(self) -> None:
        """Vérifie l'absorption d'un SevenhugsAuthError lors de la négociation Lua."""
        runtime_data = self.hass.data[DOMAIN][self.entry.entry_id]
        injector: SevenhugsLuaInjector = runtime_data.injector

        with patch.object(
            runtime_data.client,
            "async_push_webhooks",
            side_effect=SevenhugsAuthError("Session invalidated by remote"),
        ):
            sync_ok = await injector.async_sync_entities(["light.hallway"])
            self.assertFalse(sync_ok)
            self.assertTrue(injector._pending_resync)


# ============================================================================
# TIER 3 : INTERACTIONS MULTI-COMPOSANTS (CROSS-FEATURE INTEGRATION)
# ============================================================================

class TestPhase4Tier3CrossFeatureInteractions(unittest.IsolatedAsyncioTestCase):
    """Vérifications E2E Tier 3 : Interactions transverses et boucle fermée."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.server = MockSmartRemoteServer(pair_setup_delay=0.01)
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def test_tier3_end_to_end_bidirectional_closed_loop(self) -> None:
        """Boucle fermée E2E complète :
        1. Options Flow configure des entités
        2. L'injecteur Lua synchronise les boutons vers la télécommande
        3. La télécommande émet un webhook POST
        4. HA exécute le service et émet l'événement bus
        5. Le coordinateur est réveillé et met à jour son cache télémétrique
        """
        initial_options = {
            CONF_ENTITIES: ["light.dining_room", "switch.espresso"],
            CONF_AUTO_SYNC_WEBHOOKS: True,
        }
        entry = create_mock_entry("t3_loop_01", self.server, options=initial_options)
        self.hass.config_entries.async_add(entry)

        self.hass.states.async_set("light.dining_room", "off", {"friendly_name": "Salle à Manger"})
        self.hass.states.async_set("switch.espresso", "off", {"friendly_name": "Machine Espresso"})

        setup_ok = await async_setup_entry(self.hass, entry)
        self.assertTrue(setup_ok)

        # Attente de la tâche asynchrone d'injection Lua en arrière-plan
        await asyncio.sleep(0.05)

        # Vérification de l'injection Lua initiale vers le mock server
        self.assertIn("done", self.server.lua_steps_called)

        # La télécommande physique envoie un appui bouton vers le webhook enregistré
        wh_id = f"smart_remote_{entry.entry_id}"
        press_payload = {
            "domain": "light",
            "action": "turn_on",
            "entity_id": "light.dining_room",
            "params": {"brightness": 180},
        }
        req = MockRequest(json_data=press_payload)
        resp = await self.hass.webhook.async_handle_webhook(wh_id, req)
        self.assertEqual(resp.status, 200)

        # Vérification 1 : Appel direct du service Home Assistant
        self.assertEqual(len(self.hass.services.calls), 1)
        call = self.hass.services.calls[0]
        self.assertEqual(call.domain, "light")
        self.assertEqual(call.service, "turn_on")
        self.assertEqual(call.data["entity_id"], "light.dining_room")
        self.assertEqual(call.data["brightness"], 180)

        # Vérification 2 : Événement sur le bus Home Assistant
        self.assertEqual(len(self.hass.bus.events), 1)
        ev = self.hass.bus.events[0]
        self.assertEqual(ev.event_type, EVENT_SEVENHUGS_BUTTON_PRESSED)
        self.assertEqual(ev.data["entity_id"], "light.dining_room")

        # Vérification 3 : Réveil événementiel du coordinateur et mise à jour des données
        await asyncio.sleep(0.05)
        runtime_data = self.hass.data[DOMAIN][entry.entry_id]
        coordinator: SevenhugsDataUpdateCoordinator = runtime_data.coordinator
        self.assertEqual(coordinator._consecutive_failures, 0)
        self.assertTrue(coordinator.last_update_success)
        self.assertEqual(coordinator.battery, 95)

        await async_unload_entry(self.hass, entry)

    async def test_tier3_entry_reload_during_in_flight_webhook_dispatch(self) -> None:
        """Vérifie la robustesse lors d'un rechargement de l'entrée alors que des webhooks sont reçus."""
        entry = create_mock_entry("t3_reload_01", self.server)
        self.hass.config_entries.async_add(entry)
        await async_setup_entry(self.hass, entry)
        wh_id = f"smart_remote_{entry.entry_id}"

        # Lance 5 appels de webhooks simultanés tout en déclenchant un reload
        async def call_webhook():
            req = MockRequest(json_data={"domain": "switch", "action": "toggle", "entity_id": "switch.lamp"})
            try:
                return await self.hass.webhook.async_handle_webhook(wh_id, req)
            except Exception as err:
                return err

        # Exécution conjointe : reload + requêtes
        tasks = [asyncio.create_task(call_webhook()) for _ in range(5)]
        reload_task = asyncio.create_task(self.hass.config_entries.async_reload(entry.entry_id))

        results = await asyncio.gather(*tasks, reload_task, return_exceptions=True)
        # Aucune exception fatale non gérée
        for r in results:
            if isinstance(r, Exception):
                self.fail(f"Exception inattendue lors de la concurrence webhook/reload: {r}")

        # Post-reload : l'entrée est à nouveau rechargée et opérationnelle
        self.assertTrue(self.hass.webhook.has_webhook(wh_id))
        req_post = MockRequest(json_data={"domain": "light", "action": "turn_on", "entity_id": "light.desk"})
        resp_post = await self.hass.webhook.async_handle_webhook(wh_id, req_post)
        self.assertEqual(resp_post.status, 200)

        await async_unload_entry(self.hass, entry)

    async def test_tier3_reconfiguration_preserves_webhook_and_options(self) -> None:
        """Vérifie que la reconfiguration IP préserve les options et l'URL du webhook."""
        configured_entities = ["light.kitchen", "switch.fan"]
        entry = create_mock_entry(
            "t3_reconfig_01",
            self.server,
            options={CONF_ENTITIES: configured_entities},
            serial=self.server.serial_number,
        )
        self.hass.config_entries.async_add(entry)
        await async_setup_entry(self.hass, entry)

        original_wh_id = f"smart_remote_{entry.entry_id}"
        self.assertTrue(self.hass.webhook.has_webhook(original_wh_id))

        flow = SevenhugsConfigFlow()
        flow.hass = self.hass
        flow.context = {"entry_id": entry.entry_id}

        res = await flow.async_step_reconfigure({
            CONF_HOST: self.server.host,
            CONF_PORT: self.server.port,
        })
        self.assertEqual(res["type"], FlowResultType.ABORT)
        self.assertEqual(res["reason"], "reconfigure_successful")

        # Options et entités préservées
        self.assertEqual(entry.options.get(CONF_ENTITIES), configured_entities)
        # Webhook ID inchangé
        self.assertEqual(async_get_webhook_id(entry), original_wh_id)

        await async_unload_entry(self.hass, entry)

    async def test_tier3_reauthentication_preserves_webhook_and_options(self) -> None:
        """Vérifie que la réauthentification n'altère pas les configurations d'entités."""
        configured_entities = ["scene.relaxation", "light.terrace"]
        entry = create_mock_entry("t3_reauth_01", self.server, options={CONF_ENTITIES: configured_entities})
        self.hass.config_entries.async_add(entry)
        await async_setup_entry(self.hass, entry)

        flow = SevenhugsConfigFlow()
        flow.hass = self.hass
        flow.context = {"entry_id": entry.entry_id}

        res = await flow.async_step_reauth(entry.data)
        self.assertEqual(res["type"], FlowResultType.FORM)
        self.assertEqual(res["step_id"], "reauth_confirm")

        self.assertEqual(entry.options.get(CONF_ENTITIES), configured_entities)
        await async_unload_entry(self.hass, entry)


# ============================================================================
# TIER 4 : SCÉNARIOS RÉELS ET CHARGE UTILISATEUR (REAL-WORLD WORKLOADS)
# ============================================================================

class TestPhase4Tier4RealWorldWorkloads(unittest.IsolatedAsyncioTestCase):
    """Vérifications E2E Tier 4 : Scénarios complets d'utilisation domotique."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.server = MockSmartRemoteServer(pair_setup_delay=0.01)
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def test_tier4_smart_home_multi_room_sequential_workload(self) -> None:
        """Scénario utilisateur réaliste multi-pièces :
        - Éclairage plafonnier Salon (light)
        - Prise machine à café Cuisine (switch)
        - Ambiance Soirée Cinéma (scene)
        - Téléviseur connecté (media_player)
        Tous synchronisés sur la télécommande, puis activés séquentiellement.
        """
        # 1. Enregistrement des états et libellés dans Home Assistant
        self.hass.states.async_set("light.living_room_ceiling", "off", {"friendly_name": "Plafonnier Salon"})
        self.hass.states.async_set("switch.kitchen_coffee", "off", {"friendly_name": "Machine Café"})
        self.hass.states.async_set("scene.movie_night", "scening", {"friendly_name": "Scène Film"})
        self.hass.states.async_set("media_player.living_room_tv", "idle", {"friendly_name": "TV Salon"})

        entities = [
            "light.living_room_ceiling",
            "switch.kitchen_coffee",
            "scene.movie_night",
            "media_player.living_room_tv",
        ]
        entry = create_mock_entry("t4_home_01", self.server, options={CONF_ENTITIES: entities, CONF_AUTO_SYNC_WEBHOOKS: True})
        self.hass.config_entries.async_add(entry)
        await async_setup_entry(self.hass, entry)

        # Attente de la synchronisation Lua vers la télécommande
        await asyncio.sleep(0.05)
        self.assertIn("done", self.server.lua_steps_called)

        wh_id = f"smart_remote_{entry.entry_id}"

        # Écouteur global sur le bus
        captured_events = []
        self.hass.bus.async_listen(
            EVENT_SEVENHUGS_BUTTON_PRESSED,
            lambda ev: captured_events.append(ev),
        )

        # 2. Déclenchement séquentiel des 4 boutons physiques sur la télécommande
        actions_sequence = [
            {
                "domain": "light",
                "action": "turn_on",
                "entity_id": "light.living_room_ceiling",
                "params": {"brightness": 255},
            },
            {
                "domain": "switch",
                "action": "toggle",
                "entity_id": "switch.kitchen_coffee",
            },
            {
                "domain": "scene",
                "action": "turn_on",
                "entity_id": "scene.movie_night",
            },
            {
                "domain": "media_player",
                "action": "toggle",
                "entity_id": "media_player.living_room_tv",
            },
        ]

        for idx, act in enumerate(actions_sequence):
            req = MockRequest(json_data=act)
            resp = await self.hass.webhook.async_handle_webhook(wh_id, req)
            self.assertEqual(resp.status, 200)

            # Vérification de l'appel de service immédiat correspondant
            self.assertEqual(len(self.hass.services.calls), idx + 1)
            call = self.hass.services.calls[idx]
            self.assertEqual(call.domain, act["domain"])
            self.assertEqual(call.service, act["action"])
            self.assertEqual(call.data["entity_id"], act["entity_id"])

        # 3. Vérification de la complétude et de l'ordre strict des événements bus
        self.assertEqual(len(captured_events), 4)
        for idx, act in enumerate(actions_sequence):
            ev = captured_events[idx]
            self.assertEqual(ev.data["domain"], act["domain"])
            self.assertEqual(ev.data["action"], act["action"])
            self.assertEqual(ev.data["entity_id"], act["entity_id"])
            self.assertEqual(ev.data["entry_id"], entry.entry_id)

        await async_unload_entry(self.hass, entry)

    async def test_tier4_remote_wake_from_sleep_telemetry_workflow(self) -> None:
        """Scénario de réveil de veille matérielle :
        - La télécommande est endormie (échecs temporaires de polling accumulés).
        - L'utilisateur la saisit et appuie sur un bouton.
        - Le webhook dispatch le service et ordonne le réveil du coordinateur.
        - Le coordinateur remet son compteur d'échecs à 0 et rafraîchit immédiatement l'état.
        """
        entry = create_mock_entry("t4_sleep_01", self.server)
        self.hass.config_entries.async_add(entry)
        await async_setup_entry(self.hass, entry)

        runtime_data = self.hass.data[DOMAIN][entry.entry_id]
        coordinator: SevenhugsDataUpdateCoordinator = runtime_data.coordinator

        # Simulation de 2 échecs préalables dus à la veille de la télécommande
        coordinator._consecutive_failures = 2
        self.assertEqual(coordinator._consecutive_failures, 2)

        # L'utilisateur appuie sur un bouton physique pendant le réveil
        wh_id = f"smart_remote_{entry.entry_id}"
        press_payload = {
            "domain": "light",
            "action": "turn_off",
            "entity_id": "light.living_room_ceiling",
        }
        req = MockRequest(json_data=press_payload)
        resp = await self.hass.webhook.async_handle_webhook(wh_id, req)
        self.assertEqual(resp.status, 200)

        # Laisse le coordinateur exécuter sa tâche de rafraîchissement asynchrone
        await asyncio.sleep(0.05)

        # Vérification 1 : Action exécutée
        self.assertEqual(len(self.hass.services.calls), 1)
        self.assertEqual(self.hass.services.calls[0].service, "turn_off")

        # Vérification 2 : Compteur d'échecs réinitialisé à 0
        self.assertEqual(coordinator._consecutive_failures, 0)

        # Vérification 3 : Données rafraîchies et disponibles
        self.assertTrue(coordinator.last_update_success)
        self.assertEqual(coordinator.battery, 95)
        self.assertTrue(coordinator.is_connected)

        await async_unload_entry(self.hass, entry)


if __name__ == "__main__":
    unittest.main()
