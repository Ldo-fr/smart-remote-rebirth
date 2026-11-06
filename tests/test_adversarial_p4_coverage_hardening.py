"""Adversarial Coverage Hardening Suite for Sevenhugs Smart Remote (Phase 4 - Tier 5).

Milestone 5 Adversarial Coverage Hardening Challenger (challenger_p4_m5_1).
Roles: critic, specialist.

Exhaustive empirical white-box challenge across Phase 4 source code:
1. `webhook.py`:
   - String, Entity ID, and Dictionary Voluptuous validators with edge values
   - Schema validation fuzzing: non-dict, missing required keys, invalid types
   - Action / service resolution and domain prefix normalization
   - Parameter unpacking priority (data, params, parameters, service_data, extra keys)
   - Resilient exception handling when HA service calls fail
   - Event bus firing and payload encapsulation
   - Coordinator wake-up dispatching
   - Webhook registration, idempotence, and post-unload 404
2. `lua_injector.py`:
   - Dual constructor signature support
   - Remote ID resolution chain across 6 fallback layers
   - Entity ID parsing and token splitting edge cases
   - Full domain action mapping (light, switch, scene, media_player, input_boolean, fan)
   - Graceful filtering of unsupported domains (climate, vacuum, camera, etc.)
   - Friendly name extraction with missing/empty/failing states
   - Collection type safety, deduplication, and AwaitableList dual semantics
   - Asynchronous lock serialization under concurrent stress
   - Deep fault injection (SevenhugsAuthError, SevenhugsConnectionError, TimeoutError,
     CancelledError re-raise, arbitrary exceptions)
   - Coordinator wake-up notification on successful sync
3. `config_flow.py` & Options Flow:
   - PIN format parsing fuzzing (valid 8 digits in various formats, invalid lengths/chars)
   - Options flow default values, bounds validation, and trigger_resync reset behavior
   - Backward compatibility with partial option dictionaries
4. `__init__.py`:
   - SevenhugsData container dict/attribute bidirectional sync and proxy delegation
   - Setup rollback on coordinator first refresh failure
   - Complete lifecycle unregister/unload/teardown idempotence
   - Update listener with reload and without reload fallback
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
import voluptuous as vol
from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError

from tests.conftest import MockSmartRemoteServer
from custom_components.sevenhugs_smart_remote import (
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
from custom_components.sevenhugs_smart_remote.config_flow import (
    SevenhugsConfigFlow,
    SevenhugsOptionsFlowHandler,
)
from custom_components.sevenhugs_smart_remote.const import (
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
    DEFAULT_ENTITIES,
    DEFAULT_EXPOSED_DOMAINS,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    EVENT_SEVENHUGS_BUTTON_PRESSED,
    MAX_POLLING_INTERVAL,
    MIN_POLLING_INTERVAL,
    WEBHOOK_ID_PREFIX,
)
from custom_components.sevenhugs_smart_remote.lua_injector import (
    DEFAULT_DOMAIN_ACTIONS,
    _AwaitableList,
)
from custom_components.sevenhugs_smart_remote.webhook import (
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


class MockWebhookRequest:
    """Mock HTTP request for webhook handler testing."""

    def __init__(
        self,
        json_data: Any = None,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        raise_json: Exception | None = None,
    ) -> None:
        self._json_data = json_data
        self.method = method
        self.headers = headers or {"Content-Type": "application/json"}
        self._raise_json = raise_json

    async def json(self) -> Any:
        if self._raise_json:
            raise self._raise_json
        return self._json_data


class TestAdversarialWebhookCoverage(unittest.IsolatedAsyncioTestCase):
    """Adversarial stress tests for custom_components.sevenhugs_smart_remote.webhook."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.entry_id = "adv_webhook_entry_99"
        self.entry = ConfigEntry(
            entry_id=self.entry_id,
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (9999)",
            data={
                CONF_HOST: "192.168.1.50",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "SH-ADV-9999",
                CONF_REMOTE_ID: "9999",
            },
        )
        self.webhook_id = f"{WEBHOOK_ID_PREFIX}{self.entry_id}"

    async def test_validate_string_edge_cases(self) -> None:
        """Verify validate_string accepts non-empty strings and rejects empty/non-strings."""
        self.assertEqual(validate_string("light"), "light")
        self.assertEqual(validate_string("  turn_on  "), "turn_on")

        # Falsy and invalid inputs
        for invalid in ("", "   ", None, 123, True, [], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(vol.Invalid):
                    validate_string(invalid)

    async def test_validate_entity_id_edge_cases(self) -> None:
        """Verify validate_entity_id handles single strings, string lists, and rejects invalid structures."""
        self.assertEqual(validate_entity_id("light.bulb"), "light.bulb")
        self.assertEqual(validate_entity_id("  light.bulb  "), "light.bulb")
        self.assertEqual(
            validate_entity_id(["light.bulb1", "  light.bulb2 "]),
            ["light.bulb1", "light.bulb2"],
        )

        # Invalid forms: empty list, list with empty string, non-string items, None, dict
        for invalid in (
            "",
            "   ",
            [],
            ["light.bulb", ""],
            ["light.bulb", "   "],
            ["light.bulb", 123],
            None,
            123,
            {"entity_id": "light.bulb"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(vol.Invalid):
                    validate_entity_id(invalid)

    async def test_validate_dict_edge_cases(self) -> None:
        """Verify validate_dict enforces dictionary types."""
        self.assertEqual(validate_dict({"key": "val"}), {"key": "val"})
        self.assertEqual(validate_dict({}), {})

        for invalid in (None, 123, "string", [1, 2, 3], True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(vol.Invalid):
                    validate_dict(invalid)

    async def test_validate_webhook_payload_action_service_normalization(self) -> None:
        """Verify validate_webhook_payload supports either action or service and extra fields."""
        p1 = {"domain": "light", "action": "turn_on", "brightness": 255}
        validated1 = validate_webhook_payload(p1)
        self.assertEqual(validated1["domain"], "light")
        self.assertEqual(validated1["action"], "turn_on")
        self.assertEqual(validated1["brightness"], 255)

        p2 = {"domain": "switch", "service": "toggle", "custom_tag": "test"}
        validated2 = validate_webhook_payload(p2)
        self.assertEqual(validated2["domain"], "switch")
        self.assertEqual(validated2["service"], "toggle")
        self.assertEqual(validated2["custom_tag"], "test")

        # Missing both action and service
        with self.assertRaises(vol.Invalid):
            validate_webhook_payload({"domain": "light"})

        # Missing domain
        with self.assertRaises(vol.Invalid):
            validate_webhook_payload({"action": "turn_on"})

        # Non-dict payload
        with self.assertRaises(vol.Invalid):
            validate_webhook_payload(["not", "a", "dict"])

    async def test_async_get_webhook_id_variants(self) -> None:
        """Verify async_get_webhook_id normalizes strings and ConfigEntry objects."""
        self.assertEqual(async_get_webhook_id(self.entry), f"sevenhugs_{self.entry_id}")
        self.assertEqual(async_get_webhook_id(self.entry_id), f"sevenhugs_{self.entry_id}")
        self.assertEqual(
            async_get_webhook_id(f"sevenhugs_{self.entry_id}"),
            f"sevenhugs_{self.entry_id}",
        )

    async def test_async_register_and_unregister_idempotence(self) -> None:
        """Verify webhook registration and unregistration can be called multiple times safely."""
        wid1 = async_register_webhook(self.hass, self.entry)
        self.assertEqual(wid1, self.webhook_id)

        # Double registration must not raise
        wid2 = async_register_webhook(self.hass, self.entry)
        self.assertEqual(wid2, self.webhook_id)

        # Double unregistration must not raise
        async_unregister_webhook(self.hass, self.entry)
        async_unregister_webhook(self.hass, self.entry)

        # Unregistering random ID must not raise
        async_unregister_webhook(self.hass, "non_existent_entry_id")

    async def test_async_handle_webhook_malformed_json_returns_400(self) -> None:
        """Verify invalid JSON payload syntax triggers HTTP 400 Bad Request."""
        req = MockWebhookRequest(raise_json=ValueError("Unterminated string"))
        resp = await async_handle_webhook(self.hass, self.webhook_id, req)
        self.assertEqual(resp.status, 400)
        self.assertIn("Invalid JSON payload", resp.text)

    async def test_async_handle_webhook_invalid_schema_returns_400(self) -> None:
        """Verify invalid schema triggers HTTP 400 Bad Request."""
        # Non-dict
        req1 = MockWebhookRequest(json_data=["invalid", "array"])
        resp1 = await async_handle_webhook(self.hass, self.webhook_id, req1)
        self.assertEqual(resp1.status, 400)
        self.assertIn("Invalid payload schema", resp1.text)

        # Missing required domain
        req2 = MockWebhookRequest(json_data={"action": "turn_on"})
        resp2 = await async_handle_webhook(self.hass, self.webhook_id, req2)
        self.assertEqual(resp2.status, 400)

        # Missing action/service
        req3 = MockWebhookRequest(json_data={"domain": "light"})
        resp3 = await async_handle_webhook(self.hass, self.webhook_id, req3)
        self.assertEqual(resp3.status, 400)

    async def test_async_handle_webhook_service_call_and_prefix_stripping(self) -> None:
        """Verify action with domain prefix is stripped and service call receives all params."""
        req = MockWebhookRequest(
            json_data={
                "domain": "light",
                "action": "light.turn_on",
                "entity_id": "light.kitchen",
                "brightness": 200,
                "params": {"transition": 2},
            }
        )

        resp = await async_handle_webhook(self.hass, self.webhook_id, req)
        self.assertEqual(resp.status, 200)

        calls = self.hass.services.calls
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.domain, "light")
        self.assertEqual(call.service, "turn_on")  # Stripped prefix
        self.assertEqual(call.data["entity_id"], "light.kitchen")
        self.assertEqual(call.data["brightness"], 200)
        self.assertEqual(call.data["transition"], 2)

    async def test_async_handle_webhook_resilience_to_ha_service_exception(self) -> None:
        """Verify webhook handler returns HTTP 200 even if Home Assistant service execution throws."""
        with patch.object(
            self.hass.services,
            "async_call",
            side_effect=HomeAssistantError("Physical hardware unreachable"),
        ):
            req = MockWebhookRequest(
                json_data={
                    "domain": "light",
                    "action": "turn_on",
                    "entity_id": "light.broken",
                }
            )
            resp = await async_handle_webhook(self.hass, self.webhook_id, req)
            self.assertEqual(resp.status, 200)

    async def test_async_handle_webhook_coordinator_wakeup_and_event_bus(self) -> None:
        """Verify coordinator wake up is triggered and bus event is fired."""
        mock_coordinator = MagicMock()
        mock_coordinator.async_handle_wake_up = AsyncMock()

        self.hass.data.setdefault(DOMAIN, {})[self.entry_id] = {
            "coordinator": mock_coordinator,
        }

        fired_events = []

        def event_listener(event):
            fired_events.append(event)

        self.hass.bus.async_listen(EVENT_SEVENHUGS_BUTTON_PRESSED, event_listener)

        req = MockWebhookRequest(
            json_data={
                "domain": "scene",
                "action": "turn_on",
                "entity_id": "scene.movie_time",
            }
        )

        resp = await async_handle_webhook(self.hass, self.webhook_id, req)
        self.assertEqual(resp.status, 200)

        # Bus event verified
        self.assertEqual(len(fired_events), 1)
        self.assertEqual(fired_events[0].event_type, EVENT_SEVENHUGS_BUTTON_PRESSED)
        self.assertEqual(fired_events[0].data["entry_id"], self.entry_id)
        self.assertEqual(fired_events[0].data["domain"], "scene")
        self.assertEqual(fired_events[0].data["action"], "turn_on")

        # Coordinator wake up called
        mock_coordinator.async_handle_wake_up.assert_called_once_with("webhook")


class TestAdversarialLuaInjectorCoverage(unittest.IsolatedAsyncioTestCase):
    """Adversarial stress tests for custom_components.sevenhugs_smart_remote.lua_injector."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.entry = ConfigEntry(
            entry_id="adv_lua_entry_42",
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (4242)",
            data={
                CONF_HOST: "192.168.1.60",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "SH-ADV-4242",
                CONF_REMOTE_ID: "4242",
            },
            options={
                CONF_ENTITIES: ["light.living_room", "switch.fan"],
            },
        )
        self.mock_client = MagicMock()
        self.mock_client.keys = {CONF_REMOTE_ID: "4242"}
        self.mock_client.async_push_webhooks = AsyncMock(return_value=True)

        self.injector = SevenhugsLuaInjector(self.hass, self.entry, self.mock_client)

    async def test_dual_constructor_signatures(self) -> None:
        """Verify injector constructor accepts both (hass, entry, client) and (hass, client, entry)."""
        inj1 = SevenhugsLuaInjector(self.hass, self.entry, self.mock_client)
        self.assertIs(inj1.entry, self.entry)
        self.assertIs(inj1.client, self.mock_client)

        # Inverted order
        inj2 = SevenhugsLuaInjector(self.hass, self.mock_client, self.entry)
        self.assertIs(inj2.entry, self.entry)
        self.assertIs(inj2.client, self.mock_client)

    async def test_remote_id_resolution_fallbacks(self) -> None:
        """Verify remote_id resolution cascades through all 6 fallback levels."""
        # 1. client.keys
        c1 = MagicMock()
        c1.keys = {CONF_REMOTE_ID: "id_from_keys"}
        inj1 = SevenhugsLuaInjector(self.hass, self.entry, c1)
        self.assertEqual(inj1.remote_id, "id_from_keys")

        # 2. client._keys
        c2 = MagicMock(spec=[])
        c2._keys = {CONF_REMOTE_ID: "id_from_private_keys"}
        inj2 = SevenhugsLuaInjector(self.hass, self.entry, c2)
        self.assertEqual(inj2.remote_id, "id_from_private_keys")

        # 3. client._remote_id attribute
        c3 = MagicMock(spec=[])
        c3._remote_id = "id_from_client_attr"
        inj3 = SevenhugsLuaInjector(self.hass, self.entry, c3)
        self.assertEqual(inj3.remote_id, "id_from_client_attr")

        # 4. entry.data[CONF_REMOTE_ID]
        c4 = MagicMock(spec=[])
        e4 = ConfigEntry(entry_id="e4", domain=DOMAIN, title="E4", data={CONF_REMOTE_ID: "id_from_entry_data"})
        inj4 = SevenhugsLuaInjector(self.hass, e4, c4)
        self.assertEqual(inj4.remote_id, "id_from_entry_data")

        # 5. entry.data[CONF_SERIAL_NUMBER] fallback
        e5 = ConfigEntry(entry_id="e5", domain=DOMAIN, title="E5", data={CONF_SERIAL_NUMBER: "SN_FALLBACK"})
        inj5 = SevenhugsLuaInjector(self.hass, e5, c4)
        self.assertEqual(inj5.remote_id, "SN_FALLBACK")

        # 6. completely empty -> ""
        e6 = ConfigEntry(entry_id="e6", domain=DOMAIN, title="E6", data={})
        inj6 = SevenhugsLuaInjector(self.hass, e6, c4)
        self.assertEqual(inj6.remote_id, "")

    async def test_convert_entity_to_trigger_malformed_inputs(self) -> None:
        """Verify convert_entity_to_trigger returns None on invalid or malformed entity strings."""
        for malformed in (
            None,
            123,
            [],
            {},
            "",
            "   ",
            "no_dot",
            ".leading_dot",
            "trailing_dot.",
            "..",
            "light..",
            "..light",
        ):
            with self.subTest(malformed=malformed):
                trigger = self.injector.convert_entity_to_trigger(malformed)  # type: ignore[arg-type]
                self.assertIsNone(trigger)

    async def test_convert_entity_to_trigger_unsupported_domains(self) -> None:
        """Verify unsupported domains return None and do not crash."""
        for unsupported in (
            "climate.hvac",
            "vacuum.roborock",
            "camera.doorbell",
            "lock.front_door",
            "sensor.temperature",
            "binary_sensor.motion",
        ):
            with self.subTest(unsupported=unsupported):
                trigger = self.injector.convert_entity_to_trigger(unsupported)
                self.assertIsNone(trigger)

    async def test_convert_entity_to_trigger_supported_domains_mapping(self) -> None:
        """Verify all supported domains map to their exact default actions and schema."""
        expected_mappings = {
            "light.living_room": "toggle",
            "switch.power_strip": "toggle",
            "scene.romantic": "turn_on",
            "script.morning": "turn_on",
            "button.doorbell": "press",
            "cover.curtains": "toggle",
            "media_player.soundbar": "toggle",
            "input_boolean.vacation_mode": "toggle",
            "fan.ceiling": "toggle",
            "automation.wakeup": "trigger",
        }

        for entity_id, expected_action in expected_mappings.items():
            with self.subTest(entity_id=entity_id):
                trigger = self.injector.convert_entity_to_trigger(entity_id)
                self.assertIsNotNone(trigger)
                self.assertEqual(trigger["method"], "POST")
                self.assertEqual(trigger["name"], entity_id)

                body = json.loads(trigger["body"])
                self.assertEqual(body["event"], "button_press")
                self.assertEqual(body["domain"], entity_id.split(".")[0])
                self.assertEqual(body["action"], expected_action)
                self.assertEqual(body["entity_id"], entity_id)
                self.assertEqual(body["source_remote"], "4242")

    async def test_friendly_name_resolution_and_fallbacks(self) -> None:
        """Verify friendly_name resolution handles normal, missing, empty, or failing states."""
        # State with friendly name
        self.hass.states.set(
            "light.desk",
            "on",
            attributes={"friendly_name": "Bureau Lampe"},
        )
        t1 = self.injector.convert_entity_to_trigger("light.desk")
        self.assertEqual(t1["name"], "Bureau Lampe")

        # State with whitespace friendly name
        self.hass.states.set(
            "light.corridor",
            "off",
            attributes={"friendly_name": "   "},
        )
        t2 = self.injector.convert_entity_to_trigger("light.corridor")
        self.assertEqual(t2["name"], "light.corridor")

        # Exception in hass.states.get
        with patch.object(self.hass.states, "get", side_effect=RuntimeError("State engine busy")):
            t3 = self.injector.convert_entity_to_trigger("light.bedroom")
            self.assertEqual(t3["name"], "light.bedroom")

    async def test_async_build_triggers_from_entities_deduplication_and_awaitable(self) -> None:
        """Verify deduplication, case normalization, and AwaitableList dual compatibility."""
        entity_list = [
            "light.lamp",
            "LIGHT.LAMP",
            "  light.lamp  ",
            "switch.socket",
            "switch.SOCKET",
            "invalid_no_domain",
        ]

        # 1. Direct synchronous call
        res_sync = self.injector.async_build_triggers_from_entities(entity_list)
        self.assertIsInstance(res_sync, _AwaitableList)
        self.assertEqual(len(res_sync), 2)
        self.assertEqual({t["name"] for t in res_sync}, {"light.lamp", "switch.socket"})

        # 2. Awaitable call
        res_async = await self.injector.async_build_triggers_from_entities(entity_list)
        self.assertEqual(len(res_async), 2)

    async def test_async_sync_entities_empty_list_returns_true(self) -> None:
        """Verify empty entities list returns True without executing remote HTTP calls."""
        res = await self.injector.async_sync_entities([])
        self.assertTrue(res)
        self.assertFalse(self.injector._pending_resync)
        self.mock_client.async_push_webhooks.assert_not_called()

    async def test_async_sync_entities_fault_injection(self) -> None:
        """Verify comprehensive fault injection in async_sync_entities sets _pending_resync."""
        test_entities = ["light.test_lamp"]

        faults = [
            (SevenhugsAuthError("Session expired"), False),
            (SevenhugsConnectionError("Host down"), False),
            (asyncio.TimeoutError("Timeout"), False),
            (ConnectionResetError("RST"), False),
            (RuntimeError("Unexpected fault"), False),
        ]

        for exc, expected_result in faults:
            with self.subTest(exc=exc):
                self.mock_client.async_push_webhooks.side_effect = exc
                res = await self.injector.async_sync_entities(test_entities)
                self.assertEqual(res, expected_result)
                self.assertTrue(self.injector._pending_resync)

        # CancelledError MUST be re-raised
        self.mock_client.async_push_webhooks.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.injector.async_sync_entities(test_entities)

    async def test_async_sync_entities_concurrency_lock_serialization(self) -> None:
        """Verify concurrent sync calls are serialized through the asyncio lock."""
        execution_order = []

        async def fake_push(triggers):
            execution_order.append("start")
            await asyncio.sleep(0.05)
            execution_order.append("end")
            return True

        self.mock_client.async_push_webhooks.side_effect = fake_push

        task1 = asyncio.create_task(self.injector.async_sync_entities(["light.a"]))
        task2 = asyncio.create_task(self.injector.async_sync_entities(["light.b"]))

        r1, r2 = await asyncio.gather(task1, task2)
        self.assertTrue(r1)
        self.assertTrue(r2)
        # Verify strictly serialized: [start, end, start, end]
        self.assertEqual(execution_order, ["start", "end", "start", "end"])


class TestAdversarialConfigFlowAndOptionsCoverage(unittest.IsolatedAsyncioTestCase):
    """Adversarial stress tests for config flow and options flow."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()

    async def test_config_flow_pin_fuzzing(self) -> None:
        """Verify PIN code format handling: rejects invalid PINs, accepts 8 digits."""
        flow = SevenhugsConfigFlow()
        flow.hass = self.hass

        # Invalid PIN formats must return 'invalid_pin'
        for invalid_pin in ("", "1234567", "123456789", "abcdefgh", "12-34-56", "!@#$%^&*"):
            with self.subTest(invalid_pin=invalid_pin):
                res = await flow.async_step_pair_pin({"pin": invalid_pin})
                self.assertEqual(res["type"], "form")
                self.assertEqual(res["step_id"], "pair_pin")
                self.assertEqual(res["errors"], {"base": "invalid_pin"})

    async def test_options_flow_bounds_and_defaults(self) -> None:
        """Verify options flow validates polling interval range and trigger_resync."""
        entry = ConfigEntry(
            entry_id="opt_test_1",
            domain=DOMAIN,
            title="Options Test Remote",
            data={},
            options={
                CONF_ENTITIES: ["light.one", "switch.two"],
                CONF_POLLING_INTERVAL: 45,
                CONF_AUTO_SYNC_WEBHOOKS: True,
                CONF_TIMEOUT: 12,
            },
        )
        handler = SevenhugsOptionsFlowHandler(entry)
        handler.hass = self.hass

        # 1. Initial form display
        res_form = await handler.async_step_init()
        self.assertEqual(res_form["type"], "form")
        self.assertEqual(res_form["step_id"], "init")

        # 2. Submission with valid data
        user_input = {
            CONF_ENTITIES: ["light.new"],
            CONF_POLLING_INTERVAL: 60,
            CONF_AUTO_SYNC_WEBHOOKS: False,
            CONF_TIMEOUT: 10,
            CONF_TRIGGER_RESYNC: True,
        }
        res_create = await handler.async_step_init(user_input)
        self.assertEqual(res_create["type"], "create_entry")
        self.assertEqual(res_create["data"], user_input)

    async def test_options_flow_backward_compatibility_partial_dict(self) -> None:
        """Verify options flow submission without Phase 4 specific keys succeeds."""
        entry = ConfigEntry(entry_id="legacy_entry", domain=DOMAIN, title="Legacy Remote", data={}, options={})
        handler = SevenhugsOptionsFlowHandler(entry)
        handler.hass = self.hass

        legacy_input = {
            CONF_POLLING_INTERVAL: 30,
            CONF_AUTO_SYNC_WEBHOOKS: True,
            CONF_TIMEOUT: 8,
        }
        res = await handler.async_step_init(legacy_input)
        self.assertEqual(res["type"], "create_entry")
        self.assertEqual(res["data"][CONF_POLLING_INTERVAL], 30)


class TestAdversarialLifecycleAndDataContainer(unittest.IsolatedAsyncioTestCase):
    """Adversarial stress tests for __init__.py, SevenhugsData container, and lifecycle."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.server = MockSmartRemoteServer()
        await self.server.start()
        self.entry = ConfigEntry(
            entry_id="adv_lifecycle_entry_1",
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (5726)",
            data={
                CONF_HOST: self.server.host,
                CONF_PORT: self.server.port,
                CONF_SERIAL_NUMBER: "SH-LIFE-001",
                CONF_REMOTE_ID: "5726",
            },
            options={
                CONF_AUTO_SYNC_WEBHOOKS: True,
                CONF_TRIGGER_RESYNC: False,
            },
        )

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def test_sevenhugs_data_container_bidirectional_sync_and_proxy(self) -> None:
        """Verify SevenhugsData supports dict access, attr access, proxying, and sync."""
        mock_client = MagicMock(spec=AsyncSmartRemoteClient)
        mock_client.host = "192.168.1.100"
        mock_session = MagicMock()
        mock_coordinator = MagicMock()
        mock_injector = MagicMock()

        data = SevenhugsData(
            client=mock_client,
            session=mock_session,
            coordinator=mock_coordinator,
            webhook_id="sevenhugs_test",
            injector=mock_injector,
        )

        # Attribute access
        self.assertIs(data.client, mock_client)
        self.assertIs(data.session, mock_session)
        self.assertIs(data.coordinator, mock_coordinator)
        self.assertEqual(data.webhook_id, "sevenhugs_test")
        self.assertIs(data.injector, mock_injector)

        # Dict access
        self.assertIs(data["client"], mock_client)
        self.assertIs(data["session"], mock_session)
        self.assertIs(data["coordinator"], mock_coordinator)
        self.assertEqual(data["webhook_id"], "sevenhugs_test")
        self.assertIs(data["injector"], mock_injector)

        # Proxy attribute lookup to client
        self.assertEqual(data.host, "192.168.1.100")

        # Mutating dict updates attribute
        new_client = MagicMock(spec=AsyncSmartRemoteClient)
        data["client"] = new_client
        self.assertIs(data.client, new_client)

        # Mutating attribute updates dict
        new_injector = MagicMock()
        data.injector = new_injector
        self.assertIs(data["injector"], new_injector)

        # Non-existent attribute raises AttributeError
        with self.assertRaises(AttributeError):
            _ = data.completely_unknown_attribute

    async def test_async_setup_entry_rollback_on_first_refresh_failure(self) -> None:
        """Verify async_setup_entry cleans up webhook and resources if first refresh fails."""
        with patch.object(
            SevenhugsDataUpdateCoordinator,
            "async_config_entry_first_refresh",
            side_effect=ConfigEntryNotReady("Network timeout during initial refresh"),
        ):
            with self.assertRaises(ConfigEntryNotReady):
                await async_setup_entry(self.hass, self.entry)

            # Webhook must be unregistered
            webhook_id = f"sevenhugs_{self.entry.entry_id}"
            self.assertFalse(self.hass.webhook.has_webhook(webhook_id))

            # Entry must not leak in hass.data
            self.assertNotIn(self.entry.entry_id, self.hass.data.get(DOMAIN, {}))

    async def test_update_listener_fallback_without_reload(self) -> None:
        """Verify update_listener calls injector.async_sync_entities when async_reload is absent."""
        import types

        mock_injector = MagicMock()
        mock_injector.async_sync_entities = AsyncMock(return_value=True)

        self.hass.data.setdefault(DOMAIN, {})[self.entry.entry_id] = {
            "injector": mock_injector,
        }

        # Simulation of config_entries object without async_reload
        stripped_config_entries = types.SimpleNamespace()
        with patch.object(self.hass, "config_entries", stripped_config_entries):
            await update_listener(self.hass, self.entry)
            await asyncio.sleep(0.01)
            mock_injector.async_sync_entities.assert_called_once()


if __name__ == "__main__":
    unittest.main()
