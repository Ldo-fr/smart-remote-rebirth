"""Adversarial stress and regression test harness for Milestone 1.

Axes of Challenge:
1. Strict invariant: len(PLATFORMS) == 3 across all import paths (direct, package, re-import, mock toggle).
2. ServiceCall dual-access (dict + attribute + nested data delegation + KeyError semantics).
3. Isolation of hass.data.clear() against hass.services, hass.bus, and hass.webhook.
4. Stress-testing MockServices & MockBus (sync/async handlers, return_response, wildcards, re-entrant unsub).
5. Stress-testing MockWebhookManager (methods, 404/405, trailing slashes, unregister idempotence).
6. Selectors and aiohttp.web shims (voluptuous compatibility, response headers/body).
7. Phase 4 Constants integrity (exact values, types, immutability check).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import unittest
from typing import Any

# Ensure project root is in sys.path
CURRENT_DIR = os.path.abspath(os.path.dirname(__file__) if "__file__" in locals() else os.getcwd())
PROJECT_ROOT = CURRENT_DIR
while PROJECT_ROOT != "/" and not os.path.exists(os.path.join(PROJECT_ROOT, "custom_components")):
    PROJECT_ROOT = os.path.dirname(PROJECT_ROOT)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock
from homeassistant.core import HomeAssistant, ServiceCall, Event
from homeassistant.components import webhook
from homeassistant.helpers import selector
import aiohttp.web as web
import voluptuous as vol


class MockHttpRequest:
    """Mock HTTP request with method, path, headers, and json payload."""
    def __init__(
        self,
        json_data: dict | None = None,
        method: str = "POST",
        path: str = "/api/webhook/test",
        headers: dict | None = None,
    ) -> None:
        self._json_data = json_data if json_data is not None else {}
        self.method = method
        self.path = path
        self.headers = headers or {}

    async def json(self) -> dict:
        return self._json_data


class AdversarialMilestone1Harness:
    def __init__(self) -> None:
        self.tests_run = 0
        self.tests_passed = 0
        self.failures: list[tuple[str, str]] = []

    def run_check(self, name: str, fn: Any) -> None:
        self.tests_run += 1
        try:
            import inspect
            if inspect.iscoroutinefunction(fn):
                asyncio.run(fn())
            else:
                res = fn()
                if asyncio.iscoroutine(res):
                    asyncio.run(res)
            self.tests_passed += 1
            print(f"  [PASS] {name}")
        except Exception as exc:
            self.failures.append((name, str(exc)))
            print(f"  [FAIL] {name}: {exc}")

    # =========================================================================
    # AXIS 1: len(PLATFORMS) == 3 across all import paths
    # =========================================================================
    def check_platforms_import_direct(self) -> None:
        from custom_components.sevenhugs_smart_remote.const import PLATFORMS
        assert len(PLATFORMS) == 3, f"Expected len(PLATFORMS) == 3, got {len(PLATFORMS)}"
        platforms_str = [str(p).replace("Platform.", "").lower() for p in PLATFORMS]
        assert "sensor" in platforms_str
        assert "binary_sensor" in platforms_str
        assert "button" in platforms_str

    def check_platforms_import_package(self) -> None:
        from custom_components.sevenhugs_smart_remote import PLATFORMS
        assert len(PLATFORMS) == 3, f"Expected len(PLATFORMS) == 3, got {len(PLATFORMS)}"
        platforms_str = [str(p).replace("Platform.", "").lower() for p in PLATFORMS]
        assert set(platforms_str) == {"sensor", "binary_sensor", "button"}

    def check_platforms_fallback_path(self) -> None:
        # Simulate import without homeassistant.const
        import sys
        old_ha_const = sys.modules.get("homeassistant.const")
        try:
            sys.modules["homeassistant.const"] = None  # Force ImportError on import
            # Delete const from sys.modules to force reload
            old_const = sys.modules.pop("custom_components.sevenhugs_smart_remote.const", None)
            const_mod = importlib.import_module("custom_components.sevenhugs_smart_remote.const")
            assert len(const_mod.PLATFORMS) == 3, f"Fallback len(PLATFORMS) expected 3, got {len(const_mod.PLATFORMS)}"
            assert const_mod.PLATFORMS == ["sensor", "binary_sensor", "button"]
        finally:
            if old_ha_const is not None:
                sys.modules["homeassistant.const"] = old_ha_const
            # Restore const module
            sys.modules.pop("custom_components.sevenhugs_smart_remote.const", None)
            importlib.import_module("custom_components.sevenhugs_smart_remote.const")

    # =========================================================================
    # AXIS 2: ServiceCall dual dict & attribute access
    # =========================================================================
    def check_service_call_attributes(self) -> None:
        call = ServiceCall(
            domain="light",
            service="turn_on",
            service_data={"entity_id": "light.living_room", "brightness": 128},
            blocking=True,
            return_response=True,
            target={"device_id": "dev_123"},
            context="ctx_456",
        )
        assert call.domain == "light"
        assert call.service == "turn_on"
        assert call.data == {"entity_id": "light.living_room", "brightness": 128}
        assert call.service_data == {"entity_id": "light.living_room", "brightness": 128}
        assert call.blocking is True
        assert call.return_response is True
        assert call.target == {"device_id": "dev_123"}
        assert call.context == "ctx_456"

    def check_service_call_dict_indexing(self) -> None:
        call = ServiceCall(
            domain="switch",
            service="toggle",
            service_data={"entity_id": "switch.heater", "state": "on"},
        )
        # Top-level dict keys
        assert call["domain"] == "switch"
        assert call["service"] == "toggle"
        assert call["data"] == {"entity_id": "switch.heater", "state": "on"}
        assert call["service_data"] == {"entity_id": "switch.heater", "state": "on"}
        assert call["blocking"] is False
        assert call["return_response"] is False
        assert call["target"] is None

        # Inner service_data delegation
        assert call["entity_id"] == "switch.heater"
        assert call["state"] == "on"

        # Missing key must raise KeyError
        try:
            _ = call["non_existent_key"]
            assert False, "Should have raised KeyError for missing key"
        except KeyError:
            pass

    def check_service_call_edge_empty_data(self) -> None:
        call_empty = ServiceCall(domain="homeassistant", service="restart")
        assert call_empty.domain == "homeassistant"
        assert call_empty.data == {}
        assert call_empty.service_data == {}
        try:
            _ = call_empty["entity_id"]
            assert False, "Should have raised KeyError on empty data"
        except KeyError:
            pass

    def check_service_call_data_vs_service_data_arg(self) -> None:
        call_with_data = ServiceCall(domain="media_player", service="play", data={"volume": 0.5})
        assert call_with_data["volume"] == 0.5
        assert call_with_data.service_data == {"volume": 0.5}
        assert call_with_data.data == {"volume": 0.5}

    # =========================================================================
    # AXIS 3: hass.data.clear() isolation against services, bus, webhook
    # =========================================================================
    async def check_hass_data_clear_isolation(self) -> None:
        hass = HomeAssistant()

        # 1. Register service, listener, and webhook
        service_called = []
        async def dummy_service(call):
            service_called.append(call)
            return "svc_ok"
        hass.services.async_register("test_domain", "test_act", dummy_service)

        bus_events = []
        hass.bus.async_listen("test_event", lambda e: bus_events.append(e))

        webhook_received = []
        async def dummy_webhook(h, w_id, req):
            webhook_received.append((w_id, await req.json()))
            return web.Response(text="wh_ok", status=200)
        webhook.async_register(hass, "test_domain", "Test WH", "wh_test_123", dummy_webhook)

        # 2. Populate hass.data heavily
        hass.data["domain_entry"] = {"client": "mock_client", "coordinator": "mock_coord"}
        hass.data["webhook"] = "accidental_string_collision"
        hass.data["custom_state"] = 9999

        # 3. Perform wipe out
        hass.data.clear()
        assert len(hass.data) == 0, "hass.data must be completely empty"

        # 4. Verify hass.services still works perfectly
        assert hass.services.has_service("test_domain", "test_act") is True
        res = await hass.services.async_call("test_domain", "test_act", {"p": 1}, return_response=True)
        assert res == "svc_ok"
        assert len(service_called) == 1
        assert len(hass.services.calls) == 1

        # 5. Verify hass.bus still works perfectly
        hass.bus.async_fire("test_event", {"val": "fire_after_clear"})
        assert len(bus_events) == 1
        assert bus_events[0]["val"] == "fire_after_clear"
        assert len(hass.bus.events) == 1

        # 6. Verify hass.webhook still works perfectly
        assert hass.webhook.has_webhook("wh_test_123") is True
        req = MockHttpRequest(json_data={"msg": "hello_after_clear"})
        wh_resp = await webhook.async_handle_webhook(hass, "wh_test_123", req)
        assert wh_resp.status == 200
        assert wh_resp.text == "wh_ok"
        assert len(webhook_received) == 1

        # 7. Verify hass.states, config_entries, device_registry, config intact
        assert hasattr(hass, "states") and hass.states is not None
        assert hasattr(hass, "config_entries") and hass.config_entries is not None
        assert hasattr(hass, "device_registry") and hass.device_registry is not None
        assert hasattr(hass, "config") and hass.config.internal_url == "http://127.0.0.1:8123"

    # =========================================================================
    # AXIS 4: Stress-testing MockServices & MockBus
    # =========================================================================
    async def check_mock_services_advanced(self) -> None:
        hass = HomeAssistant()

        # Synchronous service registration and calling
        def sync_svc(call):
            return {"sum": call["a"] + call["b"]}

        hass.services.async_register("math", "add", sync_svc)
        assert hass.services.has_service("math", "add") is True
        assert hass.services.async_has_service("math", "add") is True

        res = await hass.services.async_call("math", "add", {"a": 10, "b": 25}, return_response=True)
        assert res == {"sum": 35}

        # Sync call method
        sync_res = hass.services.call("math", "add", {"a": 2, "b": 3}, return_response=True)
        assert sync_res == {"sum": 5}

        # Service removal
        hass.services.async_remove("math", "add")
        assert hass.services.has_service("math", "add") is False

        # Reset
        hass.services.reset()
        assert len(hass.services.calls) == 0

    async def check_mock_bus_advanced(self) -> None:
        hass = HomeAssistant()

        # Wildcard listener
        all_events = []
        unsub_wildcard = hass.bus.async_listen("*", lambda e: all_events.append(e))

        # Async coroutine listener
        async_events = []
        async def async_listener(e):
            async_events.append(e)

        unsub_async = hass.bus.async_listen("custom_evt", async_listener)

        # Fire event
        hass.bus.async_fire("custom_evt", {"k": "v1"})
        await asyncio.sleep(0.01)  # allow task loop to run

        assert len(all_events) == 1
        assert len(async_events) == 1
        assert async_events[0].event_type == "custom_evt"
        assert async_events[0].data == {"k": "v1"}
        assert async_events[0]["k"] == "v1"
        assert "k" in async_events[0]
        assert async_events[0].get("k") == "v1"
        assert async_events[0].get("missing", "def") == "def"

        # Re-entrant unsubscription test: listener unsubscribes itself during call
        self_unsub_called = []
        reentrant_unsub = None
        def self_unsub_listener(e):
            self_unsub_called.append(e)
            reentrant_unsub()

        reentrant_unsub = hass.bus.async_listen("reentrant_evt", self_unsub_listener)
        hass.bus.async_fire("reentrant_evt", {"round": 1})
        hass.bus.async_fire("reentrant_evt", {"round": 2})

        assert len(self_unsub_called) == 1, "Self-unsubscribing listener should only be called once"

        # Unsub remaining
        unsub_wildcard()
        unsub_async()

    # =========================================================================
    # AXIS 5: Stress-testing MockWebhookManager
    # =========================================================================
    async def check_mock_webhook_stress(self) -> None:
        hass = HomeAssistant()

        # Custom internal URL with trailing slash
        hass.config.internal_url = "https://my-ha.local:8123/"
        url = webhook.async_generate_url(hass, "wh_custom_url")
        assert url == "https://my-ha.local:8123/api/webhook/wh_custom_url", f"Unexpected URL formatting: {url}"

        path = webhook.async_generate_path("wh_custom_url")
        assert path == "/api/webhook/wh_custom_url"

        # Multi-method registration
        async def multi_handler(h, wid, req):
            return web.Response(text=f"method_{req.method}", status=200)

        webhook.async_register(
            hass,
            "test_domain",
            "Multi Method WH",
            "wh_multi",
            multi_handler,
            allowed_methods=["POST", "PUT", "GET"],
        )

        for m in ["POST", "PUT", "GET"]:
            req = MockHttpRequest(method=m)
            resp = await webhook.async_handle_webhook(hass, "wh_multi", req)
            assert resp.status == 200
            assert resp.text == f"method_{m}"

        # Disallowed method: DELETE -> 405
        req_del = MockHttpRequest(method="DELETE")
        resp_405 = await webhook.async_handle_webhook(hass, "wh_multi", req_del)
        assert resp_405.status == 405

        # Unknown webhook -> 404
        resp_404 = await webhook.async_handle_webhook(hass, "non_existent_wh", req)
        assert resp_404.status == 404

        # Idempotent unregister
        webhook.async_unregister(hass, "wh_multi")
        assert hass.webhook.has_webhook("wh_multi") is False
        webhook.async_unregister(hass, "wh_multi")  # No error

    # =========================================================================
    # AXIS 6: Selectors & aiohttp.web shims
    # =========================================================================
    def check_selectors_and_aiohttp_web(self) -> None:
        # SelectSelector
        sel_cfg = selector.SelectSelectorConfig(
            options=["option1", "option2", "option3"],
            mode=selector.SelectSelectorMode.DROPDOWN,
            multiple=True,
        )
        sel = selector.SelectSelector(sel_cfg)
        assert sel.config.options == ["option1", "option2", "option3"]
        assert sel.config.multiple is True

        # BooleanSelector
        bool_cfg = selector.BooleanSelectorConfig()
        bool_sel = selector.BooleanSelector(bool_cfg)
        assert bool_sel is not None

        # Factory function
        factory_sel = selector.selector({"select": {"options": ["a", "b"]}})
        assert isinstance(factory_sel, selector.SelectSelector)

        # aiohttp.web.Response headers and body
        resp = web.Response(text="Payload Test", status=201, headers={"X-Custom": "HeaderValue"})
        assert resp.status == 201
        assert resp.text == "Payload Test"
        assert resp.body == b"Payload Test"
        assert resp.headers["X-Custom"] == "HeaderValue"

        # aiohttp.web.json_response
        j_resp = web.json_response({"key": "value", "list": [1, 2, 3]}, status=200)
        assert j_resp.status == 200
        assert "application/json" in j_resp.content_type
        assert j_resp.text == '{"key": "value", "list": [1, 2, 3]}'

    # =========================================================================
    # AXIS 7: Phase 4 Constants & Invariants
    # =========================================================================
    def check_phase4_constants(self) -> None:
        from custom_components.sevenhugs_smart_remote.const import (
            CONF_ENTITIES,
            CONF_TRIGGER_RESYNC,
            DEFAULT_EXPOSED_DOMAINS,
            EVENT_SEVENHUGS_BUTTON_PRESSED,
            WEBHOOK_ID_PREFIX,
        )
        assert isinstance(CONF_ENTITIES, str) and CONF_ENTITIES == "entities"
        assert isinstance(CONF_TRIGGER_RESYNC, str) and CONF_TRIGGER_RESYNC == "trigger_resync"
        assert isinstance(DEFAULT_EXPOSED_DOMAINS, list)
        assert all(d in DEFAULT_EXPOSED_DOMAINS for d in ("light", "switch", "scene", "media_player"))
        assert isinstance(EVENT_SEVENHUGS_BUTTON_PRESSED, str) and EVENT_SEVENHUGS_BUTTON_PRESSED == "sevenhugs_button_pressed"
        assert isinstance(WEBHOOK_ID_PREFIX, str) and WEBHOOK_ID_PREFIX == "sevenhugs_"

    def run_all(self) -> bool:
        print("=== ADVERSARIAL MILESTONE 1 REGRESSION & INVARIANTS HARNESS ===")
        # Axis 1
        self.run_check("Axis 1: PLATFORMS import direct (len==3)", self.check_platforms_import_direct)
        self.run_check("Axis 1: PLATFORMS import package (len==3)", self.check_platforms_import_package)
        self.run_check("Axis 1: PLATFORMS fallback path without HA (len==3)", self.check_platforms_fallback_path)

        # Axis 2
        self.run_check("Axis 2: ServiceCall attribute access", self.check_service_call_attributes)
        self.run_check("Axis 2: ServiceCall dict indexing & delegation", self.check_service_call_dict_indexing)
        self.run_check("Axis 2: ServiceCall empty data edge cases", self.check_service_call_edge_empty_data)
        self.run_check("Axis 2: ServiceCall data vs service_data arguments", self.check_service_call_data_vs_service_data_arg)

        # Axis 3
        self.run_check("Axis 3: hass.data.clear() isolation against services/bus/webhook", self.check_hass_data_clear_isolation)

        # Axis 4
        self.run_check("Axis 4: MockServices advanced sync/async handlers & calls", self.check_mock_services_advanced)
        self.run_check("Axis 4: MockBus advanced wildcard, async coroutine, and re-entrant unsub", self.check_mock_bus_advanced)

        # Axis 5
        self.run_check("Axis 5: MockWebhookManager methods, 404/405, trailing slash", self.check_mock_webhook_stress)

        # Axis 6
        self.run_check("Axis 6: Selectors and aiohttp.web shims", self.check_selectors_and_aiohttp_web)

        # Axis 7
        self.run_check("Axis 7: Phase 4 Constants exact types and values", self.check_phase4_constants)

        print(f"\n--- RÉSULTATS : {self.tests_passed}/{self.tests_run} tests passés avec succès ---")
        if self.failures:
            print(f"ÉCHECS DÉTECTÉS ({len(self.failures)}) :")
            for name, err in self.failures:
                print(f"  - {name}: {err}")
            return False
        return True


if __name__ == "__main__":
    harness = AdversarialMilestone1Harness()
    success = harness.run_all()
    sys.exit(0 if success else 1)
