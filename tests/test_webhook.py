"""Tests unitaires et asynchrones pour le module Webhook de Sevenhugs Smart Remote (Phase 4 - R1 & R5).

Couvre l'intégralité des spécifications et exigences du webhook :
1. Enregistrement et désenregistrement du webhook local :
   - Inscription locale exclusive (local_only=True) avec webhook_id = f"smart_remote_{entry.entry_id}".
   - Métadonnées complètes (domain, name, webhook_id, allowed_methods=['POST', 'PUT']).
   - Désenregistrement propre et idempotent sans exception.
2. Validation du payload JSON et codes de réponse HTTP :
   - HTTP 200 OK en cas de payload valide.
   - HTTP 400 Bad Request en cas de JSON syntaxiquement invalide (ValueError / JSONDecodeError).
   - HTTP 400 Bad Request si 'domain' est manquant ou non-str.
   - HTTP 400 Bad Request si 'action' ET 'service' sont manquants ou non-str.
   - HTTP 400 Bad Request si le payload n'est pas un dictionnaire JSON.
   - HTTP 405 Method Not Allowed si méthode HTTP non autorisée (ex: GET).
   - HTTP 404 Not Found si webhook_id n'est pas enregistré.
3. Routage et exécution directe des services Home Assistant (hass.services.async_call) :
   - Actions supportées : light.turn_on, switch.toggle, scene.turn_on.
   - Normalisation du nom de service (suppression du préfixe domaine éventuel : 'light.turn_on' -> 'turn_on').
   - Support des clés alternatives ('service' à la place d''action').
   - Transmission des paramètres et données additionnelles (data, params, parameters, brightness, etc.).
   - Support de listes d'entités ('entity_id': ['light.1', 'light.2']).
4. Émission systématique sur le bus d'événements Home Assistant (hass.bus.async_fire) :
   - Firing systématique de EVENT_SEVENHUGS_BUTTON_PRESSED ('sevenhugs_button_pressed').
   - Transmission intégrale des attributs reçus pour automatisation universelle.
   - Présence de 'entry_id', 'remote_id', et données du bouton.
5. Réveil optimiste du coordinateur de données (coordinator.async_handle_wake_up) :
   - Appel immédiat et non-bloquant de async_handle_wake_up('webhook').
   - Tolérance aux absences du coordinateur (déchargé, non initialisé).
6. Résilience aux pannes et non-crash du runtime :
   - Échec du service ciblé (ServiceNotFound, HomeAssistantError) absorbé et loggé sans crash.
   - Échec du réveil du coordinateur absorbé sans crash.
7. Intégration de bout en bout avec le gestionnaire de webhooks Home Assistant (MockWebhookManager).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# Configuration des chemins d'importation
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import aiohttp.web as web

from custom_components.smart_remote_rebirth import SevenhugsData
from custom_components.smart_remote_rebirth.const import (
    CONF_ENTITIES,
    CONF_HOST,
    CONF_PORT,
    CONF_REMOTE_ID,
    CONF_SERIAL_NUMBER,
    DOMAIN,
    EVENT_SEVENHUGS_BUTTON_PRESSED,
    WEBHOOK_ID_PREFIX,
)
from custom_components.smart_remote_rebirth.webhook import (
    async_generate_webhook_url,
    async_get_webhook_id,
    async_handle_webhook,
    async_register_webhook,
    async_unregister_webhook,
    validate_dict,
    validate_entity_id,
    validate_string,
    validate_webhook_payload,
)


class MockRequest:
    """Mock léger représentant une requête HTTP aiohttp.web.Request."""

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


class TestSevenhugsWebhook(unittest.IsolatedAsyncioTestCase):
    """Suite de tests unitaires et asynchrones pour le webhook Sevenhugs."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.entry_id = "entry_7hugs_test_42"
        self.remote_id = "5726"
        self.entry = ConfigEntry(
            entry_id=self.entry_id,
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.50",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "a06fe048b48b",
                CONF_REMOTE_ID: self.remote_id,
            },
            unique_id="a06fe048b48b",
        )
        self.coordinator = MagicMock()
        self.coordinator.entry = self.entry
        self.coordinator.async_handle_wake_up = AsyncMock()

        self.client = MagicMock()
        self.client.keys = {CONF_REMOTE_ID: self.remote_id}
        self.session = MagicMock()

        self.runtime_data = SevenhugsData(
            client=self.client,
            session=self.session,
            coordinator=self.coordinator,
        )
        self.hass.data[DOMAIN] = {self.entry.entry_id: self.runtime_data}
        self.entry.runtime_data = self.runtime_data
        self.expected_webhook_id = f"{WEBHOOK_ID_PREFIX}{self.entry.entry_id}"

    # =========================================================================
    # 1. ENREGISTREMENT ET CYCLE DE VIE DU WEBHOOK
    # =========================================================================

    async def test_async_register_webhook_success(self) -> None:
        """Vérifie l'enregistrement conforme du webhook local (local_only=True)."""
        registered_id = async_register_webhook(self.hass, self.entry)
        self.assertEqual(registered_id, self.expected_webhook_id)
        self.assertTrue(self.hass.webhook.has_webhook(registered_id))

        info = self.hass.webhook.get_webhook(registered_id)
        self.assertIsNotNone(info)
        self.assertEqual(info.domain, DOMAIN)
        self.assertEqual(info.webhook_id, registered_id)
        self.assertTrue(info.local_only, "Le webhook Sevenhugs doit impérativement être local_only=True")
        self.assertIn("POST", info.allowed_methods)
        self.assertEqual(info.handler, async_handle_webhook)

        # Vérification génération d'URL et de chemin via le module webhook
        url = async_generate_webhook_url(self.hass, self.entry)
        self.assertEqual(url, f"http://127.0.0.1:8123/api/webhook/{registered_id}")
        url_from_helper = webhook.async_generate_url(self.hass, registered_id)
        self.assertEqual(url_from_helper, url)
        path = webhook.async_generate_path(registered_id)
        self.assertEqual(path, f"/api/webhook/{registered_id}")

    async def test_async_unregister_webhook_success(self) -> None:
        """Vérifie le désenregistrement propre du webhook."""
        async_register_webhook(self.hass, self.entry)
        self.assertTrue(self.hass.webhook.has_webhook(self.expected_webhook_id))

        async_unregister_webhook(self.hass, self.entry)
        self.assertFalse(self.hass.webhook.has_webhook(self.expected_webhook_id))

    async def test_async_unregister_webhook_idempotent(self) -> None:
        """Vérifie que le désenregistrement d'un webhook inexistant ne lève pas d'exception."""
        # Non enregistré initialement
        async_unregister_webhook(self.hass, self.entry)
        # Double désenregistrement après enregistrement
        async_register_webhook(self.hass, self.entry)
        async_unregister_webhook(self.hass, self.entry)
        async_unregister_webhook(self.hass, self.entry)
        self.assertFalse(self.hass.webhook.has_webhook(self.expected_webhook_id))

    async def test_register_webhook_format_with_various_entry_ids(self) -> None:
        """Vérifie que l'ID de webhook respecte toujours le préfixe WEBHOOK_ID_PREFIX."""
        for test_id in ["uuid_1234", "custom-entry-abc", "a06fe048b48b"]:
            custom_entry = ConfigEntry(
                entry_id=test_id,
                domain=DOMAIN,
                title="Remote",
                data={},
            )
            wid = async_register_webhook(self.hass, custom_entry)
            self.assertEqual(wid, f"smart_remote_{test_id}")
            self.assertEqual(async_get_webhook_id(custom_entry), f"smart_remote_{test_id}")
            self.assertEqual(async_get_webhook_id(test_id), f"smart_remote_{test_id}")
            async_unregister_webhook(self.hass, custom_entry)

    # =========================================================================
    # 2. DISPATCH DES APPELS DE SERVICES HOME ASSISTANT
    # =========================================================================

    async def test_dispatch_light_turn_on(self) -> None:
        """Vérifie le routage réussi vers light.turn_on avec paramètres."""
        payload = {
            "domain": "light",
            "action": "turn_on",
            "entity_id": "light.salon_plafonnier",
            "data": {"brightness": 255, "rgb_color": [255, 128, 0]},
        }
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "OK")
        self.assertEqual(len(self.hass.services.calls), 1)

        call = self.hass.services.calls[0]
        self.assertEqual(call["domain"], "light")
        self.assertEqual(call["service"], "turn_on")
        self.assertEqual(call["service_data"]["entity_id"], "light.salon_plafonnier")
        self.assertEqual(call["service_data"]["brightness"], 255)
        self.assertEqual(call["service_data"]["rgb_color"], [255, 128, 0])

    async def test_dispatch_switch_toggle(self) -> None:
        """Vérifie le routage réussi vers switch.toggle."""
        payload = {
            "domain": "switch",
            "action": "toggle",
            "entity_id": "switch.prise_machine_a_cafe",
        }
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.hass.services.calls), 1)
        call = self.hass.services.calls[0]
        self.assertEqual(call["domain"], "switch")
        self.assertEqual(call["service"], "toggle")
        self.assertEqual(call["service_data"], {"entity_id": "switch.prise_machine_a_cafe"})

    async def test_dispatch_scene_turn_on(self) -> None:
        """Vérifie le routage réussi vers scene.turn_on."""
        payload = {
            "domain": "scene",
            "action": "turn_on",
            "entity_id": "scene.ambiance_cinema",
        }
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.hass.services.calls), 1)
        call = self.hass.services.calls[0]
        self.assertEqual(call["domain"], "scene")
        self.assertEqual(call["service"], "turn_on")
        self.assertEqual(call["service_data"], {"entity_id": "scene.ambiance_cinema"})

    async def test_dispatch_using_service_key_as_action_alias(self) -> None:
        """Vérifie que la clé 'service' est acceptée comme synonyme de 'action'."""
        payload = {
            "domain": "light",
            "service": "turn_off",
            "entity_id": "light.bureau",
        }
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 200)
        call = self.hass.services.calls[0]
        self.assertEqual(call["domain"], "light")
        self.assertEqual(call["service"], "turn_off")

    async def test_dispatch_stripping_domain_prefix(self) -> None:
        """Vérifie la normalisation du service lorsqu'il est préfixé par le domaine (ex: 'light.toggle')."""
        payload = {
            "domain": "light",
            "action": "light.toggle",
            "entity_id": "light.cuisine",
        }
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 200)
        call = self.hass.services.calls[0]
        self.assertEqual(call["domain"], "light")
        self.assertEqual(call["service"], "toggle")

    async def test_dispatch_with_params_and_parameters_keys(self) -> None:
        """Vérifie la fusion des dictionnaires alternatifs 'params' et 'parameters'."""
        for alt_key in ("params", "parameters"):
            self.hass.services.reset()
            payload = {
                "domain": "media_player",
                "action": "volume_set",
                "entity_id": "media_player.tv_salon",
                alt_key: {"volume_level": 0.35},
            }
            req = MockRequest(json_data=payload)
            resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)
            self.assertEqual(resp.status, 200)
            call = self.hass.services.calls[0]
            self.assertEqual(call["service_data"]["volume_level"], 0.35)

    async def test_dispatch_multiple_entities_in_list(self) -> None:
        """Vérifie la transmission de listes d'entity_ids."""
        payload = {
            "domain": "light",
            "action": "turn_on",
            "entity_id": ["light.spot_1", "light.spot_2", "light.spot_3"],
        }
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 200)
        call = self.hass.services.calls[0]
        self.assertEqual(call["service_data"]["entity_id"], ["light.spot_1", "light.spot_2", "light.spot_3"])

    async def test_dispatch_without_entity_id(self) -> None:
        """Vérifie qu'une commande autorisée sans entity_id (lorsque CONF_ENTITIES n'est pas restreint) est acceptée."""
        payload = {
            "domain": "light",
            "action": "turn_off",
        }
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 200)
        call = self.hass.services.calls[0]
        self.assertEqual(call["domain"], "light")
        self.assertEqual(call["service"], "turn_off")
        self.assertEqual(call["service_data"], {})

    # =========================================================================
    # 3. ÉMISSION SYSTÉMATIQUE SUR LE BUS D'ÉVÉNEMENTS
    # =========================================================================

    async def test_bus_event_fired_on_every_valid_request(self) -> None:
        """Vérifie que l'événement sevenhugs_button_pressed est systématiquement émis."""
        payload = {
            "domain": "light",
            "action": "turn_on",
            "entity_id": "light.couloir",
            "custom_metadata": "valeur_arbitraire",
        }
        req = MockRequest(json_data=payload)
        await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(len(self.hass.bus.events), 1)
        evt = self.hass.bus.events[0]
        self.assertEqual(evt.event_type, EVENT_SEVENHUGS_BUTTON_PRESSED)
        self.assertEqual(evt.data["domain"], "light")
        self.assertEqual(evt["action"], "turn_on")
        self.assertEqual(evt.data["entity_id"], "light.couloir")
        self.assertEqual(evt.data["custom_metadata"], "valeur_arbitraire")

    async def test_bus_event_contains_entry_and_remote_metadata(self) -> None:
        """Vérifie la présence de entry_id, remote_id et webhook_id dans les données d'événement."""
        payload = {"domain": "switch", "action": "toggle", "entity_id": "switch.lampe"}
        req = MockRequest(json_data=payload)
        await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        evt = self.hass.bus.events[0]
        self.assertEqual(evt.data.get("entry_id"), self.entry_id)
        self.assertEqual(evt.data.get("remote_id"), self.remote_id)
        self.assertEqual(evt.data.get("webhook_id"), self.expected_webhook_id)

    async def test_bus_event_fires_for_custom_automation_trigger(self) -> None:
        """Vérifie qu'un événement peut servir de déclencheur d'automatisation Home Assistant."""
        received = []
        self.hass.bus.async_listen(EVENT_SEVENHUGS_BUTTON_PRESSED, lambda e: received.append(e))

        payload = {
            "event": "button_press",
            "domain": "automation",
            "action": "trigger",
            "button_name": "Cinema Button",
        }
        req = MockRequest(json_data=payload)
        await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].data["button_name"], "Cinema Button")

    # =========================================================================
    # 4. RÉVEIL OPTIMISTE DU COORDINATEUR (COORDINATOR WAKE-UP)
    # =========================================================================

    async def test_coordinator_wake_up_triggered_on_webhook(self) -> None:
        """Vérifie que coordinator.async_handle_wake_up('webhook') est planifié sur un webhook entrant."""
        payload = {"domain": "light", "action": "toggle", "entity_id": "light.desk"}
        req = MockRequest(json_data=payload)
        await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.coordinator.async_handle_wake_up.assert_called_once_with("webhook")

    async def test_coordinator_wake_up_tolerates_none_coordinator(self) -> None:
        """Vérifie la tolérance lorsque le coordinateur n'est pas instancié dans runtime_data."""
        self.runtime_data.coordinator = None
        payload = {"domain": "light", "action": "toggle", "entity_id": "light.desk"}
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.hass.services.calls), 1)

    async def test_coordinator_wake_up_tolerates_missing_entry_in_hass_data(self) -> None:
        """Vérifie la tolérance si hass.data ne contient pas l'entry_id."""
        self.hass.data[DOMAIN] = {}
        payload = {"domain": "light", "action": "toggle", "entity_id": "light.desk"}
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.hass.services.calls), 1)

    # =========================================================================
    # 5. CODES DE RETOUR HTTP ET VALIDATION DE STRUCTURE
    # =========================================================================

    async def test_http_200_on_success(self) -> None:
        """Vérifie le code HTTP 200 OK en cas de succès."""
        payload = {"domain": "switch", "action": "turn_on", "entity_id": "switch.plug"}
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.text, "OK")

    async def test_http_400_on_malformed_json_syntax(self) -> None:
        """Vérifie le retour HTTP 400 en cas d'erreur de décodage JSON."""
        req = MockRequest(raise_json=ValueError("Unterminated string starting at line 1"))
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 400)
        self.assertIn("Invalid JSON", resp.text)
        self.assertEqual(len(self.hass.services.calls), 0)
        self.assertEqual(len(self.hass.bus.events), 0)
        self.coordinator.async_handle_wake_up.assert_not_called()

    async def test_http_400_on_missing_domain(self) -> None:
        """Vérifie le rejet HTTP 400 si la clé 'domain' est absente."""
        payload = {"action": "turn_on", "entity_id": "light.desk"}
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 400)
        self.assertIn("domain", resp.text)
        self.assertEqual(len(self.hass.services.calls), 0)
        self.assertEqual(len(self.hass.bus.events), 0)

    async def test_http_400_on_missing_action_and_service(self) -> None:
        """Vérifie le rejet HTTP 400 si ni 'action' ni 'service' ne sont fournis."""
        payload = {"domain": "light", "entity_id": "light.desk"}
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 400)
        self.assertIn("action", resp.text)
        self.assertEqual(len(self.hass.services.calls), 0)

    async def test_http_400_on_empty_dict(self) -> None:
        """Vérifie le rejet HTTP 400 sur payload vide."""
        req = MockRequest(json_data={})
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 400)
        self.assertEqual(len(self.hass.services.calls), 0)

    async def test_http_400_on_non_dict_payload(self) -> None:
        """Vérifie le rejet HTTP 400 si le payload JSON est une liste ou un scalaire."""
        for bad_payload in (["light", "turn_on"], "simple_string", 42, False):
            req = MockRequest(json_data=bad_payload)
            resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)
            self.assertEqual(resp.status, 400)

    async def test_http_400_on_invalid_types_for_required_fields(self) -> None:
        """Vérifie le rejet HTTP 400 si domain ou action ne sont pas des chaînes."""
        for bad_domain in (123, True, [], {}):
            req = MockRequest(json_data={"domain": bad_domain, "action": "turn_on"})
            resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)
            self.assertEqual(resp.status, 400)

        for bad_action in (456, False, [], {}):
            req = MockRequest(json_data={"domain": "light", "action": bad_action})
            resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)
            self.assertEqual(resp.status, 400)

    async def test_http_405_on_unsupported_method_via_webhook_manager(self) -> None:
        """Vérifie que MockWebhookManager retourne HTTP 405 si méthode != POST/PUT."""
        async_register_webhook(self.hass, self.entry)
        req_get = MockRequest(method="GET")
        resp = await self.hass.webhook.async_handle_webhook(self.expected_webhook_id, req_get)

        self.assertEqual(resp.status, 405)
        self.assertIn("Method GET not allowed", resp.text)

    async def test_http_404_on_unregistered_webhook(self) -> None:
        """Vérifie que MockWebhookManager retourne HTTP 404 si le webhook n'existe pas."""
        req = MockRequest(method="POST", json_data={"domain": "light", "action": "toggle"})
        resp = await self.hass.webhook.async_handle_webhook("unknown_webhook_id", req)

        self.assertEqual(resp.status, 404)

    # =========================================================================
    # 6. RÉSILIENCE AUX ERREURS ET NON-CRASH DU RUNTIME
    # =========================================================================

    async def test_service_call_failure_does_not_crash_handler(self) -> None:
        """Vérifie qu'un échec de service call (ex. HomeAssistantError) n'interrompt pas le webhook."""
        async def failing_service(call: Any) -> None:
            raise HomeAssistantError("Hardware lamp unreachable or socket broken")

        self.hass.services.async_register("light", "turn_on", failing_service)

        payload = {"domain": "light", "action": "turn_on", "entity_id": "light.broken_lamp"}
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        # Le handler capture l'exception, ne crashe pas, et retourne 200 (car l'événement bouton a été traité)
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.hass.bus.events), 1)
        self.coordinator.async_handle_wake_up.assert_called_once_with("webhook")

    async def test_coordinator_wake_up_failure_does_not_crash_handler(self) -> None:
        """Vérifie qu'une exception dans async_handle_wake_up n'interrompt pas le webhook."""
        self.coordinator.async_handle_wake_up = MagicMock(side_effect=RuntimeError("Coordinator lock dead"))

        payload = {"domain": "switch", "action": "toggle", "entity_id": "switch.lamp"}
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.hass.services.calls), 1)

    # =========================================================================
    # 7. INTÉGRATION DE BOUT EN BOUT VIA MOCKWEBHOOKMANAGER
    # =========================================================================

    async def test_end_to_end_webhook_registration_dispatch_and_unregistration(self) -> None:
        """Vérifie le cycle complet de bout en bout via hass.webhook."""
        # 1. Enregistrement
        wid = async_register_webhook(self.hass, self.entry)
        self.assertTrue(self.hass.webhook.has_webhook(wid))

        # 2. Exécution d'une requête POST valide via l'infrastructure HA
        payload = {
            "domain": "light",
            "action": "turn_on",
            "entity_id": "light.salon",
            "data": {"brightness": 128},
        }
        post_req = MockRequest(json_data=payload, method="POST")
        resp = await self.hass.webhook.async_handle_webhook(wid, post_req)

        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.hass.services.calls), 1)
        self.assertEqual(len(self.hass.bus.events), 1)
        self.coordinator.async_handle_wake_up.assert_called_once_with("webhook")

        # 3. Désenregistrement
        async_unregister_webhook(self.hass, self.entry)
        self.assertFalse(self.hass.webhook.has_webhook(wid))

        # 4. Requête post-désenregistrement -> 404
        resp_after = await self.hass.webhook.async_handle_webhook(wid, post_req)
        self.assertEqual(resp_after.status, 404)

    # =========================================================================
    # 8. DURCISSEMENT DE SÉCURITÉ (LISTES BLANCHES DOMAINES/ACTIONS/ENTITÉS)
    # =========================================================================

    async def test_http_403_on_disallowed_critical_domains(self) -> None:
        """Vérifie que les domaines critiques (homeassistant, lock, shell_command) sont rejetés avec HTTP 403."""
        for bad_domain, bad_action in (
            ("homeassistant", "restart"),
            ("lock", "unlock"),
            ("shell_command", "exec_root"),
            ("hassio", "host_reboot"),
        ):
            req = MockRequest(json_data={"domain": bad_domain, "action": bad_action})
            resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)
            self.assertEqual(resp.status, 403)
        self.assertEqual(len(self.hass.services.calls), 0)

    async def test_http_403_on_disallowed_service_in_allowed_domain(self) -> None:
        """Vérifie qu'un service non autorisé dans un domaine autorisé retourne HTTP 403."""
        payload = {
            "domain": "light",
            "action": "delete_all_lights",
            "entity_id": "light.salon",
        }
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 403)
        self.assertEqual(len(self.hass.services.calls), 0)

    async def test_http_403_on_domain_entity_mismatch(self) -> None:
        """Vérifie que entity_id doit correspondre au domaine demandé (HTTP 403 sinon)."""
        payload = {
            "domain": "light",
            "action": "turn_on",
            "entity_id": "switch.prise_salon",
        }
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 403)
        self.assertEqual(len(self.hass.services.calls), 0)

    async def test_http_403_when_entity_not_in_configured_entities(self) -> None:
        """Vérifie que seules les entités exposées dans CONF_ENTITIES sont autorisées."""
        self.entry.options = {CONF_ENTITIES: ["light.autorisee", "switch.autorise"]}

        # 1. Entité autorisée -> HTTP 200
        ok_req = MockRequest(
            json_data={"domain": "light", "action": "turn_on", "entity_id": "light.autorisee"}
        )
        ok_resp = await async_handle_webhook(self.hass, self.expected_webhook_id, ok_req)
        self.assertEqual(ok_resp.status, 200)
        self.assertEqual(len(self.hass.services.calls), 1)

        # 2. Entité non exposée -> HTTP 403
        forbidden_req = MockRequest(
            json_data={"domain": "light", "action": "turn_on", "entity_id": "light.interdite"}
        )
        forbidden_resp = await async_handle_webhook(
            self.hass, self.expected_webhook_id, forbidden_req
        )
        self.assertEqual(forbidden_resp.status, 403)
        self.assertEqual(len(self.hass.services.calls), 1)

        # 3. Absence d'entity_id alors que CONF_ENTITIES est configuré -> HTTP 403
        missing_entity_req = MockRequest(
            json_data={"domain": "light", "action": "turn_off"}
        )
        missing_entity_resp = await async_handle_webhook(
            self.hass, self.expected_webhook_id, missing_entity_req
        )
        self.assertEqual(missing_entity_resp.status, 403)

    async def test_parameter_pollution_cannot_override_entity_id_or_target(self) -> None:
        """Vérifie qu'un sous-dictionnaire params/data ne peut pas écraser entity_id ou injecter area_id/device_id."""
        self.entry.options = {CONF_ENTITIES: ["light.autorisee"]}
        payload = {
            "domain": "light",
            "action": "turn_on",
            "entity_id": "light.autorisee",
            "params": {
                "entity_id": "light.interdite",
                "area_id": "living_room",
                "device_id": "dev_123",
                "brightness": 200,
            },
        }
        req = MockRequest(json_data=payload)
        resp = await async_handle_webhook(self.hass, self.expected_webhook_id, req)

        self.assertEqual(resp.status, 200)
        call = self.hass.services.calls[0]
        self.assertEqual(call["service_data"]["entity_id"], "light.autorisee")
        self.assertNotIn("area_id", call["service_data"])
        self.assertNotIn("device_id", call["service_data"])
        self.assertEqual(call["service_data"]["brightness"], 200)


if __name__ == "__main__":
    unittest.main()
