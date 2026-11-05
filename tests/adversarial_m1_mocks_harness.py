"""Adversarial stress test harness for Milestone 1 Home Assistant mock infrastructure.

Target under stress: tests/ha_mock.py
1. MockServices: unhandled calls, missing handlers, concurrent async calls, parameter mutability, dict/attr access.
2. MockBus: duplicate listeners, non-existent unsubs, async coroutine vs sync dispatch, listener exception isolation, listen_once.
3. MockWebhookManager: unregistered webhook IDs (404), invalid HTTP methods (405), missing handlers, URL generation, idempotence.
4. Selectors & Voluptuous: EntitySelector variations, Number/Boolean/Select selectors, selector_factory, voluptuous schema failure modes.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import sys
import time
from typing import Any

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock
from homeassistant.core import HomeAssistant
from homeassistant.components import webhook
import voluptuous as vol
import aiohttp.web as web


class MockHttpRequest:
    """Mock HTTP request with method, path, and json helper."""
    def __init__(self, json_data: Any = None, method: str = "POST", path: str = "/") -> None:
        self._json_data = json_data
        self.method = method
        self.path = path

    async def json(self) -> Any:
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data


# Global counters
TOTAL_TESTS = 0
PASSED_TESTS = 0
FAILED_TESTS = 0
FAILURES: list[str] = []


def record_test(name: str, passed: bool, detail: str = "") -> None:
    global TOTAL_TESTS, PASSED_TESTS, FAILED_TESTS
    TOTAL_TESTS += 1
    if passed:
        PASSED_TESTS += 1
        print(f"  [PASS] {name}" + (f" ({detail})" if detail else ""))
    else:
        FAILED_TESTS += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} -> {detail}")


# ============================================================================
# SUITE 1: MockServices Adversarial Stress
# ============================================================================

async def stress_mock_services() -> None:
    print("\n=== SUITE 1: MockServices Adversarial Stress ===")
    hass = HomeAssistant()
    services = hass.services

    # 1.1 Unhandled service call (no handler registered)
    services.reset()
    res1 = await services.async_call("unregistered_domain", "unregistered_service", {"foo": "bar"}, return_response=False)
    res2 = await services.async_call("unregistered_domain", "unregistered_service", {"foo": "bar"}, return_response=True)
    record_test(
        "1.1 Unhandled service call returns None",
        res1 is None and res2 is None and len(services.calls) == 2,
        f"Calls recorded: {len(services.calls)}, res1={res1}, res2={res2}"
    )

    # 1.2 Service lifecycle: register, has_service, call, remove, re-check
    services.reset()
    called = []
    async def sample_handler(call):
        called.append(call["action"])
        return {"status": "ok", "action": call["action"]}

    services.async_register("custom_domain", "my_action", sample_handler)
    has1 = services.has_service("custom_domain", "my_action")
    has_async1 = services.async_has_service("custom_domain", "my_action")
    res_call = await services.async_call("custom_domain", "my_action", {"action": "activate"}, return_response=True)
    
    services.async_remove("custom_domain", "my_action")
    has2 = services.has_service("custom_domain", "my_action")
    has_async2 = services.async_has_service("custom_domain", "my_action")
    res_after_remove = await services.async_call("custom_domain", "my_action", {"action": "re-activate"}, return_response=True)

    record_test(
        "1.2 Service registration, invocation, removal and lifecycle",
        has1 and has_async1 and (not has2) and (not has_async2) and res_call == {"status": "ok", "action": "activate"} and res_after_remove is None and len(called) == 1,
        f"has1={has1}, has2={has2}, res_call={res_call}, res_after_remove={res_after_remove}"
    )

    # 1.3 Idempotent removal of non-existent service
    try:
        services.async_remove("non_existent", "never_registered")
        services.remove("non_existent", "never_registered")
        idempotent_ok = True
    except Exception as err:
        idempotent_ok = False
    record_test("1.3 Non-existent service removal is idempotent", idempotent_ok)

    # 1.4 ServiceCall polymorphic dict and attribute access
    services.reset()
    await services.async_call(
        domain="light",
        service="turn_on",
        service_data={"entity_id": "light.desk", "brightness": 200},
        blocking=True,
        return_response=False,
        target={"entity_id": "light.desk"},
    )
    call = services.calls[0]
    polymorphic_ok = (
        call.domain == "light"
        and call.service == "turn_on"
        and call.service_data == {"entity_id": "light.desk", "brightness": 200}
        and call.blocking is True
        and call.return_response is False
        and call.target == {"entity_id": "light.desk"}
        and call["domain"] == "light"
        and call["service"] == "turn_on"
        and call["entity_id"] == "light.desk"  # direct access to nested service_data key
        and call["brightness"] == 200
        and call["service_data"] == {"entity_id": "light.desk", "brightness": 200}
    )
    key_error_ok = False
    try:
        _ = call["missing_parameter_xyz"]
    except KeyError:
        key_error_ok = True
    record_test("1.4 ServiceCall dual dict & attribute access + KeyError on missing", polymorphic_ok and key_error_ok)

    # 1.5 Parameter mutability and isolation
    services.reset()
    input_params = {"command": "wake", "nested": [1, 2, 3]}
    await services.async_call("remote", "send_command", input_params)
    input_params["command"] = "MUTATED_AFTER_CALL"
    call_saved = services.calls[0]
    isolated_ok = (call_saved["command"] == "wake" and call_saved.service_data["command"] == "wake")
    record_test("1.5 Parameter dictionary shallow copy isolation", isolated_ok, f"Saved value: {call_saved['command']}")

    # 1.6 Concurrent async calls burst (100 parallel calls)
    services.reset()
    num_concurrent = 100
    async def worker(idx: int):
        await services.async_call("burst", f"cmd_{idx}", {"index": idx, "ts": time.time()})

    await asyncio.gather(*(worker(i) for i in range(num_concurrent)))
    concurrent_ok = (
        len(services.calls) == num_concurrent
        and len({c["service"] for c in services.calls}) == num_concurrent
    )
    record_test("1.6 Burst concurrent async_call execution (100 parallel)", concurrent_ok, f"Recorded {len(services.calls)} calls")

    # 1.7 Handler exception propagation in async_call
    services.reset()
    def failing_handler(c):
        raise ValueError("Service exploded intentionally")

    services.async_register("bad", "fail", failing_handler)
    exception_caught = False
    try:
        await services.async_call("bad", "fail", {"test": True})
    except ValueError as err:
        if "exploded" in str(err):
            exception_caught = True
    call_logged = (len(services.calls) == 1)
    record_test("1.7 Handler exception cleanly propagates and call is recorded", exception_caught and call_logged)

    # 1.8 Sync vs Async handler dispatch
    services.reset()
    sync_called = False
    def sync_handler(c):
        nonlocal sync_called
        sync_called = True
        return {"type": "sync"}

    services.async_register("test", "sync_srv", sync_handler)
    res_sync = await services.async_call("test", "sync_srv", return_response=True)
    res_sync_direct = services.call("test", "sync_srv", return_response=True)
    record_test(
        "1.8 Sync handler callable via both async_call and call()",
        sync_called and res_sync == {"type": "sync"} and res_sync_direct == {"type": "sync"}
    )

    # 1.9 None service_data defaults to empty dict & 500 mixed calls
    services.reset()
    await services.async_call("test", "none_data", service_data=None)
    call_none = services.calls[0]
    none_data_ok = (call_none.data == {} and call_none.service_data == {})

    services.reset()
    num_burst = 500
    async def mixed_worker(i):
        await services.async_call("burst", f"svc_{i % 10}", {"idx": i})
    await asyncio.gather(*(mixed_worker(i) for i in range(num_burst)))
    record_test(
        "1.9 None service_data fallback to empty dict and 500 burst calls integrity",
        none_data_ok and len(services.calls) == num_burst,
        f"Calls recorded: {len(services.calls)}"
    )


# ============================================================================
# SUITE 2: MockBus Adversarial Stress
# ============================================================================

async def stress_mock_bus() -> None:
    print("\n=== SUITE 2: MockBus Adversarial Stress ===")
    hass = HomeAssistant()
    bus = hass.bus

    # 2.1 Duplicate listeners & granular removal
    bus.reset()
    counter = 0
    def duplicate_listener(evt):
        nonlocal counter
        counter += 1

    unsub1 = bus.async_listen("dup_event", duplicate_listener)
    unsub2 = bus.async_listen("dup_event", duplicate_listener)
    bus.async_fire("dup_event", {"n": 1})
    count_after_both = counter

    unsub1()
    bus.async_fire("dup_event", {"n": 2})
    count_after_one_unsub = counter

    unsub2()
    bus.async_fire("dup_event", {"n": 3})
    count_after_both_unsub = counter

    record_test(
        "2.1 Duplicate listeners and individual unsubscribe handles",
        count_after_both == 2 and count_after_one_unsub == 3 and count_after_both_unsub == 3,
        f"counts: both={count_after_both}, one_unsub={count_after_one_unsub}, both_unsub={count_after_both_unsub}"
    )

    # 2.2 Unsubscribing non-existent listener and double-unsubscribe idempotence
    bus.reset()
    try:
        unsub1()  # already unsubscribed
        unsub1()  # again
        dummy_unsub = bus.async_listen("temp", lambda e: None)
        dummy_unsub()
        dummy_unsub()
        dummy_unsub()
        unsub_safe = True
    except Exception as err:
        unsub_safe = False
    record_test("2.2 Unsubscribe idempotence and no-op on non-existent listeners", unsub_safe)

    # 2.3 Async coroutine vs Sync callback dispatch
    bus.reset()
    exec_log = []
    def sync_listener(evt):
        exec_log.append(("sync", evt["step"]))

    async def async_coro_listener(evt):
        await asyncio.sleep(0.001)
        exec_log.append(("async_coro", evt["step"]))

    def sync_returns_coro_listener(evt):
        async def inner():
            await asyncio.sleep(0.001)
            exec_log.append(("sync_returning_coro", evt["step"]))
        return inner()

    bus.async_listen("mixed_event", sync_listener)
    bus.async_listen("mixed_event", async_coro_listener)
    bus.async_listen("mixed_event", sync_returns_coro_listener)

    bus.async_fire("mixed_event", {"step": 42})
    # Yield control to event loop to let scheduled tasks run
    await asyncio.sleep(0.02)
    logged_types = {t[0] for t in exec_log}
    record_test(
        "2.3 Async coroutine, sync callback, and coroutine-returning sync callback dispatch",
        len(exec_log) == 3 and logged_types == {"sync", "async_coro", "sync_returning_coro"},
        f"Executed types: {logged_types}"
    )

    # 2.4 Exception fault isolation inside listeners
    bus.reset()
    l1_ran = False
    l3_ran = False
    def l1(evt):
        nonlocal l1_ran
        l1_ran = True

    def l2_exploding(evt):
        raise RuntimeError("Listener 2 crashed intentionally")

    def l3(evt):
        nonlocal l3_ran
        l3_ran = True

    bus.async_listen("fail_event", l1)
    bus.async_listen("fail_event", l2_exploding)
    bus.async_listen("fail_event", l3)

    try:
        bus.async_fire("fail_event", {"test": 1})
        no_bus_crash = True
    except Exception:
        no_bus_crash = False

    record_test(
        "2.4 Listener exception is contained and subsequent listeners execute",
        no_bus_crash and l1_ran and l3_ran,
        f"no_bus_crash={no_bus_crash}, l1_ran={l1_ran}, l3_ran={l3_ran}"
    )

    # 2.5 Wildcard '*' listeners
    bus.reset()
    wildcard_events = []
    specific_events = []
    bus.async_listen("*", lambda e: wildcard_events.append(e.event_type))
    bus.async_listen("specific", lambda e: specific_events.append(e.event_type))

    bus.async_fire("specific", {"a": 1})
    bus.async_fire("other", {"b": 2})

    record_test(
        "2.5 Wildcard '*' listener receives all events",
        len(wildcard_events) == 2 and len(specific_events) == 1 and wildcard_events == ["specific", "other"],
        f"wildcard_events={wildcard_events}, specific={specific_events}"
    )

    # 2.6 async_listen_once under repeat firing and exceptions
    bus.reset()
    once_calls = []
    def once_callback(evt):
        once_calls.append(evt["seq"])
        if evt["seq"] == 1:
            raise ValueError("First call exception")

    bus.async_listen_once("once_topic", once_callback)
    for i in range(1, 6):
        bus.async_fire("once_topic", {"seq": i})

    record_test(
        "2.6 async_listen_once fires strictly once even if callback raises",
        once_calls == [1],
        f"once_calls={once_calls}"
    )

    # 2.7 Event class accessors, dictionary behavior, and time_fired
    bus.reset()
    bus.async_fire("data_check", {"temp": 21.5, "mode": "heat"}, origin="LOCAL")
    evt = bus.events[0]
    event_ok = (
        evt.event_type == "data_check"
        and evt["temp"] == 21.5
        and evt.data["mode"] == "heat"
        and evt.get("mode") == "heat"
        and evt.get("missing", 999) == 999
        and ("temp" in evt)
        and ("event_type" in evt)
        and ("non_existent" not in evt)
        and isinstance(evt.time_fired, datetime.datetime)
        and evt.origin == "LOCAL"
    )
    evt_key_error = False
    try:
        _ = evt["completely_unknown_key"]
    except KeyError:
        evt_key_error = True
    record_test("2.7 Event dataclass dictionary/attribute access & time_fired", event_ok and evt_key_error)

    # 2.8 Event payload isolation against caller mutation
    bus.reset()
    caller_data = {"param": "initial"}
    bus.async_fire("isolation_check", caller_data)
    caller_data["param"] = "MUTATED"
    saved_param = bus.events[0].data["param"]
    record_test("2.8 Event payload shallow copy protects event history", saved_param == "initial", f"Saved: {saved_param}")

    # 2.9 Modifying listener registry inside event callback
    bus.reset()
    fired_events = []
    unsub_inner = None
    def dynamic_listener_1(evt):
        fired_events.append("dyn1")
        if unsub_inner:
            unsub_inner()
        bus.async_listen("dyn_topic", lambda e: fired_events.append("new_dyn"))

    def dynamic_listener_2(evt):
        fired_events.append("dyn2")

    bus.async_listen("dyn_topic", dynamic_listener_1)
    unsub_inner = bus.async_listen("dyn_topic", dynamic_listener_2)

    try:
        bus.async_fire("dyn_topic", {})
        mutation_safe = True
    except Exception:
        mutation_safe = False

    bus.async_fire("dyn_topic", {})
    record_test(
        "2.9 Modifying listener registry inside event callback is safe against mutation errors",
        mutation_safe and "dyn1" in fired_events and "new_dyn" in fired_events,
        f"fired_events={fired_events}"
    )


# ============================================================================
# SUITE 3: MockWebhookManager Adversarial Stress
# ============================================================================

async def stress_mock_webhook() -> None:
    print("\n=== SUITE 3: MockWebhookManager Adversarial Stress ===")
    hass = HomeAssistant()
    wm = hass.webhook

    # 3.1 Unregistered webhook ID handling (HTTP 404)
    req = MockHttpRequest(json_data={"hello": "world"}, method="POST")
    resp_404 = await wm.async_handle_webhook("non_existent_id_999", req)
    record_test(
        "3.1 Unregistered webhook ID returns HTTP 404 Response",
        resp_404 is not None and resp_404.status == 404 and "not found" in resp_404.text.lower(),
        f"status={getattr(resp_404, 'status', None)}, text={getattr(resp_404, 'text', None)}"
    )

    # 3.2 Method Not Allowed (HTTP 405) & case sensitivity
    async def dummy_handler(h, wid, r):
        return web.Response(status=200, text="OK")

    wm.async_register("domain_test", "Test Hook", "hook_post_only", dummy_handler, allowed_methods=["POST"])

    # Valid POST
    r_post = MockHttpRequest(method="POST")
    resp_ok = await wm.async_handle_webhook("hook_post_only", r_post)

    # Invalid GET
    r_get = MockHttpRequest(method="GET")
    resp_get = await wm.async_handle_webhook("hook_post_only", r_get)

    # Lowercase 'post' should fail if methods are strict uppercase
    r_lower = MockHttpRequest(method="post")
    resp_lower = await wm.async_handle_webhook("hook_post_only", r_lower)

    # Invalid PUT
    r_put = MockHttpRequest(method="PUT")
    resp_put = await wm.async_handle_webhook("hook_post_only", r_put)

    record_test(
        "3.2 HTTP 405 Method Not Allowed on disallowed or non-matching methods",
        resp_ok.status == 200 and resp_get.status == 405 and resp_lower.status == 405 and resp_put.status == 405,
        f"POST={resp_ok.status}, GET={resp_get.status}, post={resp_lower.status}, PUT={resp_put.status}"
    )

    # 3.3 Default allowed methods (POST, PUT)
    wm.async_register("domain_test", "Default Methods Hook", "hook_default", dummy_handler)
    resp_def_post = await wm.async_handle_webhook("hook_default", MockHttpRequest(method="POST"))
    resp_def_put = await wm.async_handle_webhook("hook_default", MockHttpRequest(method="PUT"))
    resp_def_del = await wm.async_handle_webhook("hook_default", MockHttpRequest(method="DELETE"))
    record_test(
        "3.3 Default webhook methods allow POST & PUT, reject DELETE",
        resp_def_post.status == 200 and resp_def_put.status == 200 and resp_def_del.status == 405,
        f"POST={resp_def_post.status}, PUT={resp_def_put.status}, DELETE={resp_def_del.status}"
    )

    # 3.4 Missing method attribute on request (graceful fallback)
    class RawRequestWithoutMethod:
        pass
    resp_no_method = await wm.async_handle_webhook("hook_default", RawRequestWithoutMethod())
    record_test(
        "3.4 Request lacking .method attribute falls through to handler without crash",
        resp_no_method.status == 200
    )

    # 3.5 URL Generation under various base URL configurations
    # Standard base url
    hass.config.internal_url = "http://192.168.1.100:8123"
    url1 = webhook.async_generate_url(hass, "my_hook_id")
    
    # Trailing slash handling
    hass.config.internal_url = "http://homeassistant.local:8123/"
    url2 = webhook.async_generate_url(hass, "my_hook_id")

    # None internal_url fallback
    hass.config.internal_url = None
    url3 = webhook.async_generate_url(hass, "my_hook_id")

    url_tests_ok = (
        url1 == "http://192.168.1.100:8123/api/webhook/my_hook_id"
        and url2 == "http://homeassistant.local:8123/api/webhook/my_hook_id"
        and url3 == "http://127.0.0.1:8123/api/webhook/my_hook_id"
    )
    record_test("3.5 async_generate_url handles custom hosts, trailing slashes and fallbacks", url_tests_ok, f"u1={url1}, u2={url2}, u3={url3}")

    # 3.6 async_generate_path argument polymorphism
    path1 = webhook.async_generate_path("test_id")
    path2 = webhook.async_generate_path(hass, "test_id")
    path3 = webhook.async_generate_path(webhook_id="test_id")
    path_args_ok = (path1 == "/api/webhook/test_id" and path2 == "/api/webhook/test_id" and path3 == "/api/webhook/test_id")
    path_type_error = False
    try:
        webhook.async_generate_path()
    except TypeError:
        path_type_error = True
    record_test("3.6 async_generate_path supports 1 arg, 2 args, keyword, and raises TypeError on empty", path_args_ok and path_type_error)

    # 3.7 Webhook registration overwrite & unregister idempotence
    async def handler_v1(h, wid, r):
        return web.Response(status=200, text="V1")
    async def handler_v2(h, wid, r):
        return web.Response(status=200, text="V2")

    webhook.async_register(hass, "test", "Hook", "dyn_hook", handler_v1)
    res_v1 = await webhook.async_handle_webhook(hass, "dyn_hook", MockHttpRequest())
    
    # Overwrite
    webhook.async_register(hass, "test", "Hook", "dyn_hook", handler_v2)
    res_v2 = await webhook.async_handle_webhook(hass, "dyn_hook", MockHttpRequest())

    # Unregister
    webhook.async_unregister(hass, "dyn_hook")
    res_after_unsub = await webhook.async_handle_webhook(hass, "dyn_hook", MockHttpRequest())
    # Second unregister should not raise
    webhook.async_unregister(hass, "dyn_hook")

    record_test(
        "3.7 Webhook handler overwrite and idempotent unregistration",
        res_v1.text == "V1" and res_v2.text == "V2" and res_after_unsub.status == 404,
        f"v1={res_v1.text}, v2={res_v2.text}, after={res_after_unsub.status}"
    )

    # 3.8 MockWebResponse and mock_json_response encoding & headers
    r_bytes = web.Response(body=b'{"binary": true}', status=201, content_type="application/json")
    r_text = web.Response(text='{"text": true}', status=200)
    r_json = web.json_response({"key": "valeur_accentuée_éàü", "num": 42}, status=200)
    import json
    parsed_json = json.loads(r_json.text)

    record_test(
        "3.8 MockWebResponse and mock_json_response encoding & headers",
        r_bytes.status == 201 and r_bytes.body == b'{"binary": true}' and r_text.text == '{"text": true}' and parsed_json["key"] == "valeur_accentuée_éàü",
        f"parsed_json={parsed_json}"
    )

    # 3.9 Empty allowed_methods and multi-webhook isolation
    wm.async_register("dom", "No Methods", "hook_no_methods", dummy_handler, allowed_methods=[])
    resp_empty_methods = await wm.async_handle_webhook("hook_no_methods", MockHttpRequest(method="POST"))

    for i in range(5):
        wm.async_register("dom", f"Hook {i}", f"hook_{i}", dummy_handler)
    hooks_registered = all(wm.has_webhook(f"hook_{i}") for i in range(5))
    record_test(
        "3.9 Empty allowed_methods rejects all requests and multi-webhook registry isolates IDs",
        resp_empty_methods.status == 405 and hooks_registered and len(wm.webhooks) >= 5,
        f"empty_status={resp_empty_methods.status}, registered={hooks_registered}"
    )


# ============================================================================
# SUITE 4: Selectors and Voluptuous Schema Validation Stress
# ============================================================================

def stress_selectors_and_voluptuous() -> None:
    print("\n=== SUITE 4: Selectors & Voluptuous Schema Stress ===")
    from homeassistant.helpers import selector
    from homeassistant.helpers.selector import (
        EntitySelector,
        EntitySelectorConfig,
        SelectSelector,
        SelectSelectorConfig,
        SelectSelectorMode,
        BooleanSelector,
        BooleanSelectorConfig,
        NumberSelector,
        NumberSelectorConfig,
        NumberSelectorMode,
    )

    # 4.1 EntitySelector with multiple=True across polymorphic inputs
    sel_multi = EntitySelector(EntitySelectorConfig(multiple=True, domain=["light", "switch"]))
    r_list = sel_multi(["light.lamp", "switch.desk"])
    r_tuple = sel_multi(("light.lamp",))
    r_set = sel_multi({"light.lamp"})
    r_single = sel_multi("light.lamp")
    r_int = sel_multi(42)
    r_none = sel_multi(None)

    multi_ok = (
        r_list == ["light.lamp", "switch.desk"]
        and r_tuple == ["light.lamp"]
        and r_set == ["light.lamp"]
        and r_single == ["light.lamp"]
        and r_int == ["42"]
        and r_none == []
    )
    record_test(
        "4.1 EntitySelector(multiple=True) converts single, tuple, set, scalar, and None gracefully",
        multi_ok,
        f"single={r_single}, none={r_none}, tuple={r_tuple}"
    )

    # 4.2 EntitySelector with multiple=False unpacking single-element list
    sel_single = EntitySelector(EntitySelectorConfig(multiple=False))
    s_single = sel_single("light.desk")
    s_unpacked = sel_single(["light.desk"])
    s_multi_list = sel_single(["light.desk", "light.fan"])
    s_none = sel_single(None)

    single_ok = (
        s_single == "light.desk"
        and s_unpacked == "light.desk"
        and s_multi_list == ["light.desk", "light.fan"]
        and s_none is None
    )
    record_test(
        "4.2 EntitySelector(multiple=False) unboxes 1-element list and preserves scalars",
        single_ok,
        f"s_unpacked={s_unpacked}, s_none={s_none}"
    )

    # 4.3 BooleanSelector truthy and falsy coercion
    b_sel = BooleanSelector()
    b1 = b_sel(True)
    b2 = b_sel(False)
    b3 = b_sel(1)
    b4 = b_sel(0)
    b5 = b_sel("yes")
    b6 = b_sel("")
    b7 = b_sel(None)

    bool_ok = (b1 is True and b2 is False and b3 is True and b4 is False and b5 is True and b6 is False and b7 is False)
    record_test("4.3 BooleanSelector boolean coercion across truthy/falsy types", bool_ok)

    # 4.4 selector_factory dispatch and config parsing
    f_ent = selector.selector({"entity": {"multiple": True, "domain": "light"}})
    f_sel = selector.selector({"select": {"options": ["opt1", "opt2"], "mode": "list"}})
    f_num = selector.selector({"number": {"min": 10, "max": 100, "step": 5, "mode": "slider"}})
    f_bool = selector.selector({"boolean": {}})
    f_unknown = selector.selector({"custom_unknown": {}})

    factory_ok = (
        isinstance(f_ent, EntitySelector) and f_ent.config.multiple is True
        and isinstance(f_sel, SelectSelector) and f_sel.config.options == ["opt1", "opt2"]
        and isinstance(f_num, NumberSelector) and f_num.config.min == 10 and f_num.config.max == 100
        and isinstance(f_bool, BooleanSelector)
        and isinstance(f_unknown, selector.Selector)
    )
    record_test("4.4 selector_factory correctly instantiates all typed selectors", factory_ok)

    # 4.5 Voluptuous schema integration with selectors
    schema = vol.Schema({
        vol.Required("entities"): EntitySelector(EntitySelectorConfig(multiple=True)),
        vol.Optional("enabled", default=True): BooleanSelector(),
        vol.Optional("polling_interval", default=60): NumberSelector(NumberSelectorConfig(min=15, max=300)),
    })

    # Valid schema run
    valid_res = schema({"entities": ["light.living_room"], "polling_interval": 30})
    valid_ok = (valid_res["entities"] == ["light.living_room"] and valid_res["enabled"] is True and valid_res["polling_interval"] == 30)

    # Missing Required field
    missing_required_ok = False
    try:
        schema({"polling_interval": 30})
    except vol.Invalid as err:
        if "Required key missing" in str(err) or "entities" in str(err):
            missing_required_ok = True

    record_test("4.5 Voluptuous schema validation: valid defaults and Required key failure", valid_ok and missing_required_ok)

    # 4.6 Voluptuous Validators: All, Coerce, Range, In failure modes
    strict_schema = vol.Schema({
        vol.Required("interval"): vol.All(vol.Coerce(int), vol.Range(min=15, max=300)),
        vol.Required("mode"): vol.In(["fast", "normal", "slow"]),
    })

    # Valid input with string coercion
    res_coerced = strict_schema({"interval": "45", "mode": "normal"})
    coerced_ok = (res_coerced["interval"] == 45 and res_coerced["mode"] == "normal")

    # Coerce failure
    coerce_fail_ok = False
    try:
        strict_schema({"interval": "not_a_number", "mode": "fast"})
    except vol.Invalid:
        coerce_fail_ok = True

    # Range failure (less than min)
    range_min_fail_ok = False
    try:
        strict_schema({"interval": 10, "mode": "fast"})
    except vol.Invalid as err:
        if "less than minimum" in str(err):
            range_min_fail_ok = True

    # Range failure (greater than max)
    range_max_fail_ok = False
    try:
        strict_schema({"interval": 500, "mode": "fast"})
    except vol.Invalid as err:
        if "greater than maximum" in str(err):
            range_max_fail_ok = True

    # In validator failure
    in_fail_ok = False
    try:
        strict_schema({"interval": 60, "mode": "invalid_mode"})
    except vol.Invalid as err:
        if "not in" in str(err):
            in_fail_ok = True

    record_test(
        "4.6 Voluptuous All/Coerce/Range/In validators enforce boundaries and raise vol.Invalid",
        coerced_ok and coerce_fail_ok and range_min_fail_ok and range_max_fail_ok and in_fail_ok,
        f"coerced={coerced_ok}, coerce_fail={coerce_fail_ok}, min_fail={range_min_fail_ok}, max_fail={range_max_fail_ok}, in_fail={in_fail_ok}"
    )

    # 4.7 Callable defaults and unexpected extra key stripping
    schema_callable = vol.Schema({
        vol.Required("items", default=list): EntitySelector(EntitySelectorConfig(multiple=True)),
        vol.Optional("timeout", default=lambda: 120): vol.Coerce(int),
    })
    res_default_call = schema_callable({"unexpected_extra_key": "discard_me"})
    record_test(
        "4.7 Voluptuous callable defaults evaluated and unmodeled extra keys stripped",
        res_default_call["items"] == [] and res_default_call["timeout"] == 120 and "unexpected_extra_key" not in res_default_call,
        f"res={res_default_call}"
    )


# ============================================================================
# MAIN RUNNER
# ============================================================================

async def main() -> int:
    print("======================================================================")
    print("STARTING ADVERSARIAL STRESS TEST HARNESS — MILESTONE 1 MOCKS")
    print("======================================================================")
    start_time = time.time()

    await stress_mock_services()
    await stress_mock_bus()
    await stress_mock_webhook()
    stress_selectors_and_voluptuous()

    duration = time.time() - start_time
    print("\n======================================================================")
    print(f"HARNESS SUMMARY: {PASSED_TESTS}/{TOTAL_TESTS} tests passed in {duration:.3f}s")
    if FAILED_TESTS == 0:
        print("ALL ADVERSARIAL STRESS TESTS PASSED SUCCESSFULLY! (Verdict: APPROVE)")
        print("======================================================================")
        return 0
    else:
        print(f"FAILED TESTS ({FAILED_TESTS}):")
        for f in FAILURES:
            print(f"  - {f}")
        print("======================================================================")
        return 1


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.exit(code)
