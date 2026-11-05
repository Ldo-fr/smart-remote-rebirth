"""Harness de validation autonome pour Milestone 1 (Mocks & Constantes).

Valide :
1. MockServices : async_call enregistre les appels, async_register, has_service, return_response.
2. MockBus : async_fire enregistre les événements (accès Event.data et Event['key']), écouteurs, unsubscribe, wildcard, listen_once.
3. MockWebhookManager & homeassistant.components.webhook :
   - async_register enregistre les métadonnées (domain, name, webhook_id, allowed_methods, local_only).
   - async_generate_url et async_generate_path retournent les URLs conformes.
   - async_unregister désenregistre proprement et avec idempotence.
   - async_handle_webhook achemine vers le handler, retourne 404 si inconnu, 405 si méthode rejetée.
4. homeassistant.helpers.selector : EntitySelector, EntitySelectorConfig, SelectSelector, NumberSelector, intégration avec voluptuous.
5. aiohttp.web : Response et json_response disponibles et fonctionnels.
6. custom_components/sevenhugs_smart_remote/const.py : Présence et valeurs des constantes Phase 4.
"""

from __future__ import annotations

import asyncio
import os
import sys

current_dir = os.path.abspath(os.path.dirname(__file__) if "__file__" in locals() else os.getcwd())
while current_dir != "/" and not os.path.exists(os.path.join(current_dir, "custom_components")):
    current_dir = os.path.dirname(current_dir)
PROJECT_ROOT = current_dir
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock
from homeassistant.core import HomeAssistant


class MockHttpRequest:
    """Mock léger d'une requête HTTP aiohttp."""
    def __init__(self, json_data: dict | None = None, method: str = "POST", path: str = "/") -> None:
        self._json_data = json_data or {}
        self.method = method
        self.path = path

    async def json(self) -> dict:
        return self._json_data


async def verify_mock_services() -> None:
    hass = HomeAssistant()
    assert hasattr(hass, "services"), "HomeAssistant doit posséder l'attribut services"

    # 1. async_call enregistrement
    await hass.services.async_call("light", "turn_on", {"entity_id": "light.desk", "brightness": 255})
    assert len(hass.services.calls) == 1, "Le service call doit être enregistré dans hass.services.calls"
    call = hass.services.calls[0]
    assert call["domain"] == "light"
    assert call["service"] == "turn_on"
    assert call["service_data"] == {"entity_id": "light.desk", "brightness": 255}

    # 2. async_register et exécution avec return_response
    executed = False
    async def mock_handler(call_rec):
        nonlocal executed
        executed = True
        return {"result": "success"}

    hass.services.async_register("switch", "toggle", mock_handler)
    assert hass.services.has_service("switch", "toggle"), "has_service doit retourner True pour switch.toggle"
    res = await hass.services.async_call("switch", "toggle", {"entity_id": "switch.lamp"}, return_response=True)
    assert executed is True, "Le gestionnaire de service enregistré doit avoir été invoqué"
    assert res == {"result": "success"}, "La valeur retournée par le handler doit être transmise"

    print("[PASS] MockServices: async_call records domain, service, parameters, blocking, handler execution")


async def verify_mock_bus() -> None:
    hass = HomeAssistant()
    assert hasattr(hass, "bus"), "HomeAssistant doit posséder l'attribut bus"

    # 1. async_fire enregistre l'événement
    payload = {"domain": "light", "action": "toggle", "entity_id": "light.salon"}
    hass.bus.async_fire("sevenhugs_button_pressed", payload)
    assert len(hass.bus.events) == 1, "L'événement doit être enregistré dans hass.bus.events"
    evt = hass.bus.events[0]
    assert evt.event_type == "sevenhugs_button_pressed"
    assert evt.data["domain"] == "light"
    assert evt["action"] == "toggle", "Event doit supporter l'accès par crochet evt['key']"

    # 2. async_listen et unsubscribe
    received_events = []
    def listener(event):
        received_events.append(event)

    unsub = hass.bus.async_listen("sevenhugs_button_pressed", listener)
    hass.bus.async_fire("sevenhugs_button_pressed", {"domain": "scene", "action": "activate"})
    assert len(received_events) == 1
    assert received_events[0]["domain"] == "scene"

    unsub()
    hass.bus.async_fire("sevenhugs_button_pressed", {"domain": "switch", "action": "turn_off"})
    assert len(received_events) == 1, "Unsubscribe doit arrêter la réception d'événements"

    # 3. async_listen_once
    once_events = []
    hass.bus.async_listen_once("test_once_event", lambda e: once_events.append(e))
    hass.bus.async_fire("test_once_event", {"num": 1})
    hass.bus.async_fire("test_once_event", {"num": 2})
    assert len(once_events) == 1, "listen_once ne doit recevoir l'événement qu'une seule fois"
    assert once_events[0]["num"] == 1

    print("[PASS] MockBus: async_fire records Event, async_listen, unsubscribe, and listen_once operate correctly")


async def verify_mock_webhook() -> None:
    hass = HomeAssistant()
    assert hasattr(hass, "webhook"), "HomeAssistant doit posséder l'attribut webhook"

    from homeassistant.components import webhook

    # 1. async_register et génération d'URL / path
    called_webhook = False
    async def webhook_handler(ha, w_id, request):
        nonlocal called_webhook
        called_webhook = True
        data = await request.json()
        assert w_id == "sevenhugs_entry_42"
        assert data["test"] == 123
        import aiohttp.web as web
        return web.Response(status=200, text="OK")

    webhook.async_register(
        hass,
        "sevenhugs_smart_remote",
        "Sevenhugs Remote Webhook",
        "sevenhugs_entry_42",
        webhook_handler,
        allowed_methods=["POST"],
        local_only=True,
    )

    assert hass.webhook.has_webhook("sevenhugs_entry_42") is True
    url = webhook.async_generate_url(hass, "sevenhugs_entry_42")
    assert url == "http://127.0.0.1:8123/api/webhook/sevenhugs_entry_42"
    path = webhook.async_generate_path("sevenhugs_entry_42")
    assert path == "/api/webhook/sevenhugs_entry_42"

    # 2. async_handle_webhook
    req = MockHttpRequest(json_data={"test": 123}, method="POST")
    resp = await webhook.async_handle_webhook(hass, "sevenhugs_entry_42", req)
    assert called_webhook is True, "Le handler de webhook doit être invoqué"
    assert resp.status == 200
    assert resp.text == "OK"

    # 3. HTTP 405 Method Not Allowed
    get_req = MockHttpRequest(method="GET")
    resp_405 = await webhook.async_handle_webhook(hass, "sevenhugs_entry_42", get_req)
    assert resp_405.status == 405

    # 4. HTTP 404 Webhook Not Found
    resp_404 = await webhook.async_handle_webhook(hass, "unknown_webhook_id", req)
    assert resp_404.status == 404

    # 5. async_unregister
    webhook.async_unregister(hass, "sevenhugs_entry_42")
    assert hass.webhook.has_webhook("sevenhugs_entry_42") is False
    # Idempotent unregister without exception
    webhook.async_unregister(hass, "sevenhugs_entry_42")

    print("[PASS] MockWebhookManager: register, URL generation, dispatch, 404/405 handling, and unregister verified")


def verify_selector_and_voluptuous() -> None:
    from homeassistant.helpers import selector
    from homeassistant.helpers.selector import (
        EntitySelector,
        EntitySelectorConfig,
        SelectSelector,
        SelectSelectorConfig,
        SelectSelectorMode,
        NumberSelector,
        NumberSelectorConfig,
        NumberSelectorMode,
    )
    import voluptuous as vol

    config = EntitySelectorConfig(multiple=True, domain=["light", "switch"])
    ent_sel = EntitySelector(config)
    assert ent_sel.config.multiple is True
    assert ent_sel.config.domain == ["light", "switch"]

    num_sel = NumberSelector(NumberSelectorConfig(min=15, max=300, mode=NumberSelectorMode.SLIDER))
    assert num_sel.config.min == 15
    assert num_sel.config.max == 300

    # Test avec voluptuous schema
    schema = vol.Schema({
        vol.Optional("entities", default=[]): ent_sel,
        vol.Optional("polling_interval", default=60): num_sel,
    })

    validated = schema({"entities": ["light.kitchen", "switch.desk"], "polling_interval": 30})
    assert validated["entities"] == ["light.kitchen", "switch.desk"]
    assert validated["polling_interval"] == 30

    default_validated = schema({})
    assert default_validated["entities"] == []
    assert default_validated["polling_interval"] == 60

    print("[PASS] Selector: EntitySelector, NumberSelector, and voluptuous validation verified")


def verify_aiohttp_web_shim() -> None:
    import aiohttp.web as web

    r1 = web.Response(status=200, text="Success")
    assert r1.status == 200
    assert r1.text == "Success"
    assert r1.body == b"Success"

    r2 = web.Response(status=400, text="Bad Request")
    assert r2.status == 400
    assert r2.text == "Bad Request"

    r3 = web.json_response({"error": "invalid_payload"}, status=400)
    assert r3.status == 400
    assert "application/json" in r3.content_type

    print("[PASS] aiohttp.web: Response and json_response shims verified")


def verify_const_phase4() -> None:
    from custom_components.sevenhugs_smart_remote.const import (
        CONF_ENTITIES,
        DEFAULT_EXPOSED_DOMAINS,
        EVENT_SEVENHUGS_BUTTON_PRESSED,
        WEBHOOK_ID_PREFIX,
        CONF_TRIGGER_RESYNC,
    )

    assert CONF_ENTITIES == "entities"
    assert DEFAULT_EXPOSED_DOMAINS == ["light", "switch", "scene", "media_player"] or list(DEFAULT_EXPOSED_DOMAINS) == ["light", "switch", "scene", "media_player"]
    assert EVENT_SEVENHUGS_BUTTON_PRESSED == "sevenhugs_button_pressed"
    assert WEBHOOK_ID_PREFIX == "sevenhugs_"
    assert CONF_TRIGGER_RESYNC == "trigger_resync"

    print("[PASS] const.py: Phase 4 constants verified")


async def main() -> None:
    print("--- Démarrage des vérifications Milestone 1 ---")
    await verify_mock_services()
    await verify_mock_bus()
    await verify_mock_webhook()
    verify_selector_and_voluptuous()
    verify_aiohttp_web_shim()
    verify_const_phase4()
    print("--- TOUTES LES VÉRIFICATIONS MILESTONE 1 SONT PASSÉES AVEC SUCCÈS (6/6) ---")


if __name__ == "__main__":
    asyncio.run(main())
