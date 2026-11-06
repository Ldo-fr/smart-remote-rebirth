"""Shims et mocks Home Assistant complets pour exécution autonome des tests (Phase 2).

Fournit les modules et classes nécessaires pour tester config_flow, lifecycle,
reconfiguration, reauthentification, options flow et device registry sans dépendance
vers Home Assistant Core ou voluptuous externe.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import datetime
from enum import Enum
import json
import logging
import os
import sys
import types
from typing import Any, Callable, Mapping, Sequence

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYLIB_DIR = os.path.join(PROJECT_ROOT, ".pylib")

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if os.path.isdir(PYLIB_DIR) and PYLIB_DIR not in sys.path:
    sys.path.insert(0, PYLIB_DIR)

try:
    import aiohttp
except ImportError:
    try:
        import tests.conftest
        import aiohttp
    except ImportError:
        aiohttp = None

# ============================================================================
# 1. SHIM VOLUPTUOUS
# ============================================================================

class Invalid(Exception):
    """Exception de validation voluptuous."""
    def __init__(self, message: str, path: list[Any] | None = None) -> None:
        super().__init__(message)
        self.msg = message
        self.path = path or []

class MultipleInvalid(Invalid):
    """Exception groupée voluptuous."""
    def __init__(self, errors: list[Invalid] | None = None) -> None:
        super().__init__("Multiple invalid entries")
        self.errors = errors or []

class Marker:
    def __init__(self, schema: Any, default: Any = None, description: str | None = None) -> None:
        self.schema = schema
        self.default = default
        self.description = description

    def __hash__(self) -> int:
        return hash(self.schema)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Marker):
            return self.schema == other.schema
        return self.schema == other

class Required(Marker):
    pass

class Optional(Marker):
    pass

class Schema:
    def __init__(self, schema: Any, extra: int = 0) -> None:
        self.schema = schema
        self.extra = extra

    def __call__(self, data: Any) -> dict[str, Any]:
        if not isinstance(self.schema, dict):
            return data
        if data is None:
            data = {}
        result = {}
        for key, validator in self.schema.items():
            raw_key = key.schema if isinstance(key, Marker) else key
            default_val = getattr(key, "default", None)
            if callable(default_val):
                default_val = default_val()

            if raw_key in data:
                val = data[raw_key]
                if callable(validator) and not isinstance(validator, Schema):
                    try:
                        val = validator(val)
                    except Exception as err:
                        raise Invalid(f"Invalid value for {raw_key}: {err}") from err
                result[raw_key] = val
            elif isinstance(key, Required):
                if default_val is not None:
                    result[raw_key] = default_val
                else:
                    raise Invalid(f"Required key missing: {raw_key}")
            elif isinstance(key, Optional) and default_val is not None:
                result[raw_key] = default_val

        return result

def All(*validators: Any) -> Callable[[Any], Any]:
    def validate(val: Any) -> Any:
        for v in validators:
            if callable(v):
                val = v(val)
        return val
    return validate

def Coerce(type_fn: type) -> Callable[[Any], Any]:
    def validate(val: Any) -> Any:
        try:
            return type_fn(val)
        except (ValueError, TypeError) as err:
            raise Invalid(f"Cannot coerce {val} to {type_fn.__name__}") from err
    return validate

def Range(min: Any = None, max: Any = None) -> Callable[[Any], Any]:
    def validate(val: Any) -> Any:
        if min is not None and val < min:
            raise Invalid(f"Value {val} is less than minimum {min}")
        if max is not None and val > max:
            raise Invalid(f"Value {val} is greater than maximum {max}")
        return val
    return validate

def In(container: Sequence[Any] | Mapping[Any, Any]) -> Callable[[Any], Any]:
    def validate(val: Any) -> Any:
        if val not in container:
            raise Invalid(f"Value {val} is not in {container}")
        return val
    return validate

# ============================================================================
# 2. SHIM HOMEASSISTANT EXCEPTIONS & FLOW TYPES
# ============================================================================

class HomeAssistantError(Exception):
    """Erreur de base Home Assistant."""

class ConfigEntryAuthFailed(HomeAssistantError):
    """Erreur d'authentification pour une ConfigEntry."""

class ConfigEntryNotReady(HomeAssistantError):
    """ConfigEntry pas encore prête."""

class AbortFlow(HomeAssistantError):
    """Interruption explicite d'un flux de configuration."""
    def __init__(self, reason: str, description_placeholders: dict[str, Any] | None = None) -> None:
        super().__init__(f"Flow aborted: {reason}")
        self.reason = reason
        self.description_placeholders = description_placeholders or {}

class FlowResultType(str, Enum):
    FORM = "form"
    CREATE_ENTRY = "create_entry"
    ABORT = "abort"
    SHOW_PROGRESS = "progress"
    SHOW_PROGRESS_DONE = "progress_done"
    MENU = "menu"

FlowResult = dict[str, Any]

# ============================================================================
# 3. SHIM CONFIG ENTRIES & CORE
# ============================================================================

class ConfigEntryState(str, Enum):
    NOT_LOADED = "not_loaded"
    LOADED = "loaded"
    SETUP_ERROR = "setup_error"
    SETUP_RETRY = "setup_retry"
    MIGRATION_ERROR = "migration_error"

@dataclass
class ConfigEntry:
    entry_id: str
    domain: str
    title: str
    data: dict[str, Any]
    options: dict[str, Any] = field(default_factory=dict)
    unique_id: str | None = None
    version: int = 1
    state: ConfigEntryState = ConfigEntryState.NOT_LOADED
    runtime_data: Any = None
    _update_listeners: list[Callable] = field(default_factory=list)
    _unload_listeners: list[Callable] = field(default_factory=list)

    def add_update_listener(self, listener: Callable) -> Callable:
        self._update_listeners.append(listener)
        def remove() -> None:
            if listener in self._update_listeners:
                self._update_listeners.remove(listener)
        return remove

    def async_on_unload(self, func: Callable) -> None:
        self._unload_listeners.append(func)

class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, type] = {}

    def register(self, domain: str) -> Callable[[type], type]:
        def decorator(cls: type) -> type:
            self._handlers[domain] = cls
            return cls
        return decorator

    def get(self, domain: str) -> type | None:
        return self._handlers.get(domain)

HANDLERS = HandlerRegistry()

class FlowHandler:
    """Classe de base pour les gestionnaires de flux."""
    cur_step: dict[str, Any] | None = None

    def __init__(self) -> None:
        self.hass: Any = None
        self.handler: str = getattr(self, "DOMAIN", "sevenhugs_smart_remote")
        self.context: dict[str, Any] = {}
        self.flow_id: str = "mock_flow_id_123"

class ConfigFlow(FlowHandler):
    """Classe de base ConfigFlow conforme Home Assistant."""
    DOMAIN: str = ""
    VERSION: int = 1

    def __init_subclass__(cls, domain: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if domain:
            cls.DOMAIN = domain
            HANDLERS.register(domain)(cls)

    def __init__(self) -> None:
        super().__init__()
        self.unique_id: str | None = None

    async def async_set_unique_id(self, unique_id: str | None = None, raise_on_progress: bool = True) -> None:
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(
        self,
        updates: dict[str, Any] | None = None,
        reload_on_update: bool = True,
    ) -> None:
        if not self.unique_id or not self.hass:
            return

        domain = getattr(self, "DOMAIN", self.handler)
        for entry in self.hass.config_entries.async_entries(domain):
            if entry.unique_id == self.unique_id:
                if updates:
                    self.hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})
                    if reload_on_update:
                        self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))
                raise AbortFlow("already_configured")

    def async_show_form(
        self,
        step_id: str,
        data_schema: Any = None,
        errors: dict[str, str] | None = None,
        description_placeholders: dict[str, str] | None = None,
        last_step: bool | None = None,
    ) -> FlowResult:
        return {
            "type": FlowResultType.FORM,
            "flow_id": self.flow_id,
            "handler": getattr(self, "DOMAIN", self.handler),
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
            "description_placeholders": description_placeholders or {},
        }

    def async_create_entry(
        self,
        title: str,
        data: dict[str, Any],
        options: dict[str, Any] | None = None,
        description: str | None = None,
        description_placeholders: dict[str, str] | None = None,
    ) -> FlowResult:
        entry = ConfigEntry(
            entry_id=f"mock_entry_{self.unique_id or 'uuid'}",
            domain=getattr(self, "DOMAIN", self.handler),
            title=title,
            data=data,
            options=options or {},
            unique_id=self.unique_id,
        )
        if self.hass:
            self.hass.config_entries.async_add(entry)

        return {
            "type": FlowResultType.CREATE_ENTRY,
            "flow_id": self.flow_id,
            "handler": getattr(self, "DOMAIN", self.handler),
            "title": title,
            "data": data,
            "options": options or {},
            "description": description,
            "description_placeholders": description_placeholders or {},
            "result": entry,
        }

    def async_abort(
        self,
        reason: str,
        description_placeholders: dict[str, str] | None = None,
    ) -> FlowResult:
        return {
            "type": FlowResultType.ABORT,
            "flow_id": self.flow_id,
            "handler": getattr(self, "DOMAIN", self.handler),
            "reason": reason,
            "description_placeholders": description_placeholders or {},
        }

    def async_show_progress(
        self,
        step_id: str,
        progress_action: str,
        description_placeholders: dict[str, str] | None = None,
    ) -> FlowResult:
        return {
            "type": FlowResultType.SHOW_PROGRESS,
            "flow_id": self.flow_id,
            "handler": getattr(self, "DOMAIN", self.handler),
            "step_id": step_id,
            "progress_action": progress_action,
            "description_placeholders": description_placeholders or {},
        }

    def async_show_progress_done(self, next_step_id: str) -> FlowResult:
        return {
            "type": FlowResultType.SHOW_PROGRESS_DONE,
            "flow_id": self.flow_id,
            "handler": getattr(self, "DOMAIN", self.handler),
            "step_id": next_step_id,
        }

    def async_update_reload_and_abort(
        self,
        entry: ConfigEntry,
        data_updates: dict[str, Any] | None = None,
        options_updates: dict[str, Any] | None = None,
        reason: str = "reconfigure_successful",
        reload_even_if_entry_is_unchanged: bool = True,
    ) -> FlowResult:
        if self.hass:
            new_data = {**entry.data, **(data_updates or {})}
            new_options = {**entry.options, **(options_updates or {})}
            self.hass.config_entries.async_update_entry(entry, data=new_data, options=new_options)
            if reload_even_if_entry_is_unchanged:
                self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))

        return {
            "type": FlowResultType.ABORT,
            "flow_id": self.flow_id,
            "handler": getattr(self, "DOMAIN", self.handler),
            "reason": reason,
        }

    def _get_reconfigure_entry(self) -> ConfigEntry:
        entry_id = self.context.get("entry_id")
        if not entry_id:
            raise KeyError("Reconfigure called without entry_id in context")
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if not entry:
            raise KeyError(f"Config entry {entry_id} not found")
        return entry

    def _get_reauth_entry(self) -> ConfigEntry:
        entry_id = self.context.get("entry_id")
        if entry_id and self.hass:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry:
                return entry
        unique_id = self.context.get("unique_id")
        if unique_id and self.hass:
            for entry in self.hass.config_entries.async_entries():
                if entry.unique_id == unique_id:
                    return entry
        raise KeyError("Reauth called without matching entry in context")


class OptionsFlow(FlowHandler):
    """Classe de base OptionsFlow conforme Home Assistant."""
    def __init__(self, config_entry: ConfigEntry | None = None) -> None:
        super().__init__()
        self.config_entry = config_entry

    def async_show_form(
        self,
        step_id: str,
        data_schema: Any = None,
        errors: dict[str, str] | None = None,
        description_placeholders: dict[str, str] | None = None,
    ) -> FlowResult:
        return {
            "type": FlowResultType.FORM,
            "flow_id": self.flow_id,
            "handler": getattr(self, "DOMAIN", self.handler),
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
            "description_placeholders": description_placeholders or {},
        }

    def async_create_entry(self, title: str = "", data: dict[str, Any] | None = None) -> FlowResult:
        return {
            "type": FlowResultType.CREATE_ENTRY,
            "flow_id": self.flow_id,
            "handler": getattr(self, "DOMAIN", self.handler),
            "title": title,
            "data": data or {},
        }

# ============================================================================
# 4. SHIM DEVICE REGISTRY
# ============================================================================

CONNECTION_NETWORK_MAC = "mac"

@dataclass
class DeviceEntry:
    id: str
    config_entries: set[str]
    identifiers: set[tuple[str, str]]
    connections: set[tuple[str, str]]
    manufacturer: str
    model: str
    name: str
    sw_version: str | None = None
    hw_version: str | None = None

class DeviceRegistry:
    def __init__(self, hass: Any) -> None:
        self.hass = hass
        self.devices: dict[str, DeviceEntry] = {}
        self._identifiers_index: dict[tuple[str, str], str] = {}

    def async_get_or_create(
        self,
        config_entry_id: str,
        identifiers: set[tuple[str, str]],
        connections: set[tuple[str, str]] | None = None,
        manufacturer: str = "",
        model: str = "",
        name: str = "",
        sw_version: str | None = None,
        hw_version: str | None = None,
    ) -> DeviceEntry:
        for ident in identifiers:
            if ident in self._identifiers_index:
                dev_id = self._identifiers_index[ident]
                device = self.devices[dev_id]
                device.config_entries.add(config_entry_id)
                return device

        dev_id = f"dev_{len(self.devices) + 1}"
        device = DeviceEntry(
            id=dev_id,
            config_entries={config_entry_id},
            identifiers=identifiers,
            connections=connections or set(),
            manufacturer=manufacturer,
            model=model,
            name=name,
            sw_version=sw_version,
            hw_version=hw_version,
        )
        self.devices[dev_id] = device
        for ident in identifiers:
            self._identifiers_index[ident] = dev_id
        return device

    def async_get_device(self, identifiers: set[tuple[str, str]]) -> DeviceEntry | None:
        for ident in identifiers:
            if ident in self._identifiers_index:
                dev_id = self._identifiers_index[ident]
                return self.devices[dev_id]
        return None

# ============================================================================
# 5. SHIM ZEROCONF
# ============================================================================

@dataclass
class ZeroconfServiceInfo:
    host: str
    port: int
    hostname: str
    type: str
    name: str
    properties: dict[str | bytes, Any]
    addresses: list[str] = field(default_factory=list)

# ============================================================================
# 6. SHIM MOCK CONFIG ENTRIES MANAGER & HOMEASSISTANT CORE
# ============================================================================

class MockConfigEntriesManager:
    def __init__(self, hass: Any) -> None:
        self.hass = hass
        self._entries: dict[str, ConfigEntry] = {}
        self.reload_count: int = 0
        self.forwarded_platforms: dict[str, list[str]] = {}
        self.unloaded_platforms: dict[str, list[str]] = {}

    def async_entries(self, domain: str | None = None) -> list[ConfigEntry]:
        if domain:
            return [e for e in self._entries.values() if e.domain == domain]
        return list(self._entries.values())

    def async_get_entry(self, entry_id: str) -> ConfigEntry | None:
        return self._entries.get(entry_id)

    def async_add(self, entry: ConfigEntry) -> None:
        self._entries[entry.entry_id] = entry

    def async_update_entry(
        self,
        entry: ConfigEntry,
        data: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        unique_id: str | None = None,
    ) -> None:
        if data is not None:
            entry.data = data
        if options is not None:
            entry.options = options
            for listener in list(entry._update_listeners):
                self.hass.async_create_task(listener(self.hass, entry))
        if unique_id is not None:
            entry.unique_id = unique_id

    async def async_reload(self, entry_id: str) -> bool:
        self.reload_count += 1
        entry = self.async_get_entry(entry_id)
        if entry:
            if not hasattr(entry, "_reload_lock") or entry._reload_lock is None:
                entry._reload_lock = asyncio.Lock()
            async with entry._reload_lock:
                from custom_components.sevenhugs_smart_remote import async_setup_entry, async_unload_entry
                await async_unload_entry(self.hass, entry)
                try:
                    res = await async_setup_entry(self.hass, entry)
                    entry.state = ConfigEntryState.LOADED if res else ConfigEntryState.SETUP_ERROR
                    return bool(res)
                except ConfigEntryNotReady:
                    entry.state = ConfigEntryState.SETUP_RETRY
                    return False
                except Exception:
                    entry.state = ConfigEntryState.SETUP_ERROR
                    return False
        return True

    async def async_forward_entry_setups(self, entry: ConfigEntry, platforms: Sequence[str | Platform]) -> bool:
        self.forwarded_platforms[entry.entry_id] = list(platforms)
        return True

    async def async_forward_entry_setup(self, entry: ConfigEntry, platform: str | Platform) -> bool:
        if entry.entry_id not in self.forwarded_platforms:
            self.forwarded_platforms[entry.entry_id] = []
        self.forwarded_platforms[entry.entry_id].append(platform)
        return True

    async def async_unload_platforms(self, entry: ConfigEntry, platforms: Sequence[str | Platform]) -> bool:
        self.unloaded_platforms[entry.entry_id] = list(platforms)
        return True


ConfigEntriesMock = MockConfigEntriesManager


class State:
    """Représentation d'un état d'entité Home Assistant."""
    def __init__(self, entity_id: str, state: str, attributes: dict[str, Any] | None = None) -> None:
        self.entity_id = entity_id
        self.state = str(state)
        self.attributes = attributes or {}

    def __repr__(self) -> str:
        return f"<State {self.entity_id}={self.state}>"


class MockStateMachine:
    """Mock de hass.states permettant d'interroger les entités enregistrées."""
    def __init__(self) -> None:
        self._states: dict[str, State] = {}

    def get(self, entity_id: str) -> State | None:
        return self._states.get(entity_id)

    def set(self, entity_id: str, state: str, attributes: dict[str, Any] | None = None) -> None:
        self.async_set(entity_id, state, attributes)

    def async_set(self, entity_id: str, state: str, attributes: dict[str, Any] | None = None) -> None:
        self._states[entity_id] = State(entity_id, str(state), attributes)

    def all(self) -> list[State]:
        return list(self._states.values())

    def __contains__(self, entity_id: str) -> bool:
        return entity_id in self._states


# ============================================================================
# 6b. SHIM SERVICES, BUS, WEBHOOK & SELECTOR (Phase 4)
# ============================================================================

class MockConfig:
    """Mock de hass.config."""
    def __init__(self) -> None:
        self.internal_url: str = "http://127.0.0.1:8123"
        self.external_url: str | None = None
        self.config_dir: str = "/config"


class ServiceCall(dict):
    """Représentation d'un appel de service Home Assistant compatible dict et attributs."""
    def __init__(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
        service_data: dict[str, Any] | None = None,
        blocking: bool = False,
        return_response: bool = False,
        target: dict[str, Any] | None = None,
        context: Any = None,
    ) -> None:
        call_data = dict(service_data or data or {})
        super().__init__(
            domain=domain,
            service=service,
            data=call_data,
            service_data=call_data,
            blocking=blocking,
            return_response=return_response,
            target=target,
            context=context,
        )

    def __getitem__(self, item: str) -> Any:
        if item in self:
            return super().__getitem__(item)
        data = self.get("data")
        if isinstance(data, dict) and item in data:
            return data[item]
        raise KeyError(item)

    @property
    def domain(self) -> str:
        return self["domain"]

    @property
    def service(self) -> str:
        return self["service"]

    @property
    def data(self) -> dict[str, Any]:
        return self["data"]

    @property
    def service_data(self) -> dict[str, Any]:
        return self["service_data"]

    @property
    def blocking(self) -> bool:
        return self["blocking"]

    @property
    def return_response(self) -> bool:
        return self["return_response"]

    @property
    def target(self) -> dict[str, Any] | None:
        return self["target"]

    @property
    def context(self) -> Any:
        return self["context"]


class MockServices:
    """Mock du registre de services Home Assistant (hass.services)."""

    def __init__(self, hass: Any = None) -> None:
        self.hass = hass
        self.calls: list[ServiceCall] = []
        self._services: dict[tuple[str, str], Callable] = {}

    def async_register(
        self,
        domain: str,
        service: str,
        service_func: Callable,
        schema: Any = None,
    ) -> None:
        self._services[(domain, service)] = service_func

    register = async_register

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self._services

    def async_has_service(self, domain: str, service: str) -> bool:
        return self.has_service(domain, service)

    async def async_call(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        blocking: bool = False,
        return_response: bool = False,
        target: dict[str, Any] | None = None,
        context: Any = None,
    ) -> Any:
        call = ServiceCall(
            domain=domain,
            service=service,
            data=service_data,
            service_data=service_data,
            blocking=blocking,
            return_response=return_response,
            target=target,
            context=context,
        )
        self.calls.append(call)

        handler = self._services.get((domain, service))
        if handler:
            res = handler(call)
            if asyncio.iscoroutine(res):
                res = await res
            return res if return_response else None
        return None

    def call(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        blocking: bool = False,
        return_response: bool = False,
        target: dict[str, Any] | None = None,
        context: Any = None,
    ) -> Any:
        call = ServiceCall(
            domain=domain,
            service=service,
            data=service_data,
            service_data=service_data,
            blocking=blocking,
            return_response=return_response,
            target=target,
            context=context,
        )
        self.calls.append(call)
        handler = self._services.get((domain, service))
        if handler and not asyncio.iscoroutinefunction(handler):
            res = handler(call)
            if return_response:
                return res
        return None

    def async_remove(self, domain: str, service: str) -> None:
        self._services.pop((domain, service), None)

    remove = async_remove

    def reset(self) -> None:
        self.calls.clear()
        self._services.clear()


@dataclass
class Event:
    """Représentation d'un événement sur le bus Home Assistant."""
    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    origin: Any = None
    time_fired: Any = None
    context: Any = None

    def __post_init__(self) -> None:
        if self.time_fired is None:
            self.time_fired = datetime.datetime.now(datetime.timezone.utc)

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item) and item in ("event_type", "data", "origin", "time_fired", "context"):
            return getattr(self, item)
        if item in self.data:
            return self.data[item]
        raise KeyError(item)

    def get(self, item: str, default: Any = None) -> Any:
        if hasattr(self, item) and item in ("event_type", "data", "origin", "time_fired", "context"):
            return getattr(self, item)
        return self.data.get(item, default)

    def __contains__(self, item: str) -> bool:
        return item in ("event_type", "data", "origin", "time_fired", "context") or item in self.data


class MockBus:
    """Mock du bus d'événements Home Assistant (hass.bus)."""

    def __init__(self, hass: Any = None) -> None:
        self.hass = hass
        self.events: list[Event] = []
        self._listeners: dict[str, list[Callable]] = {}

    def async_listen(self, event_type: str, listener: Callable[[Event], Any]) -> Callable[[], None]:
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append(listener)

        def remove_listener() -> None:
            if event_type in self._listeners and listener in self._listeners[event_type]:
                self._listeners[event_type].remove(listener)

        return remove_listener

    listen = async_listen

    def async_listen_once(self, event_type: str, listener: Callable[[Event], Any]) -> Callable[[], None]:
        def _once_wrapper(event: Event) -> Any:
            remove_listener()
            return listener(event)

        remove_listener = self.async_listen(event_type, _once_wrapper)
        return remove_listener

    listen_once = async_listen_once

    def async_fire(
        self,
        event_type: str,
        event_data: dict[str, Any] | None = None,
        origin: Any = None,
        context: Any = None,
    ) -> None:
        event = Event(event_type=event_type, data=dict(event_data or {}), origin=origin, context=context)
        self.events.append(event)

        matching_listeners = list(self._listeners.get(event_type, [])) + list(self._listeners.get("*", []))
        for listener in matching_listeners:
            try:
                if asyncio.iscoroutinefunction(listener):
                    if self.hass and hasattr(self.hass, "async_create_task"):
                        self.hass.async_create_task(listener(event))
                    else:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            loop.create_task(listener(event))
                else:
                    res = listener(event)
                    if asyncio.iscoroutine(res):
                        if self.hass and hasattr(self.hass, "async_create_task"):
                            self.hass.async_create_task(res)
                        else:
                            loop = asyncio.get_event_loop()
                            if loop.is_running():
                                loop.create_task(res)
            except Exception:
                pass

    def fire(
        self,
        event_type: str,
        event_data: dict[str, Any] | None = None,
        origin: Any = None,
        context: Any = None,
    ) -> None:
        self.async_fire(event_type, event_data, origin, context)

    def reset(self) -> None:
        self.events.clear()
        self._listeners.clear()


DEFAULT_WEBHOOK_METHODS = ["POST", "PUT"]


@dataclass
class WebhookInfo:
    domain: str
    name: str
    webhook_id: str
    handler: Callable
    allowed_methods: Sequence[str] = field(default_factory=lambda: list(DEFAULT_WEBHOOK_METHODS))
    local_only: bool = False


class MockWebhookManager:
    """Mock du gestionnaire de webhooks Home Assistant."""

    def __init__(self, hass: Any = None) -> None:
        self.hass = hass
        self._webhooks: dict[str, WebhookInfo] = {}

    def async_register(
        self,
        domain: str,
        name: str,
        webhook_id: str,
        handler: Callable,
        allowed_methods: Sequence[str] | None = None,
        local_only: bool = False,
    ) -> None:
        methods = list(allowed_methods) if allowed_methods is not None else list(DEFAULT_WEBHOOK_METHODS)
        self._webhooks[webhook_id] = WebhookInfo(
            domain=domain,
            name=name,
            webhook_id=webhook_id,
            handler=handler,
            allowed_methods=methods,
            local_only=local_only,
        )

    def async_unregister(self, webhook_id: str) -> None:
        self._webhooks.pop(webhook_id, None)

    def async_generate_url(self, webhook_id: str) -> str:
        base_url = "http://127.0.0.1:8123"
        if self.hass and hasattr(self.hass, "config"):
            if getattr(self.hass.config, "internal_url", None):
                base_url = self.hass.config.internal_url
            elif hasattr(self.hass.config, "api") and isinstance(self.hass.config.api, dict):
                base_url = self.hass.config.api.get("base_url", base_url)
        return f"{base_url.rstrip('/')}/api/webhook/{webhook_id}"

    def async_generate_path(self, webhook_id: str) -> str:
        return f"/api/webhook/{webhook_id}"

    async def async_handle_webhook(self, webhook_id: str, request: Any) -> Any:
        entry = self._webhooks.get(webhook_id)
        web = sys.modules.get("aiohttp.web")
        if not entry:
            if web and hasattr(web, "Response"):
                return web.Response(status=404, text=f"Webhook {webhook_id} not found")
            return None

        if hasattr(request, "method") and request.method not in entry.allowed_methods:
            if web and hasattr(web, "Response"):
                return web.Response(status=405, text=f"Method {request.method} not allowed")
            return None

        return await entry.handler(self.hass, webhook_id, request)

    def get_webhook(self, webhook_id: str) -> WebhookInfo | None:
        return self._webhooks.get(webhook_id)

    def has_webhook(self, webhook_id: str) -> bool:
        return webhook_id in self._webhooks

    @property
    def webhooks(self) -> list[WebhookInfo]:
        return list(self._webhooks.values())


def _get_webhook_manager(hass: Any) -> MockWebhookManager:
    if hasattr(hass, "webhook") and isinstance(hass.webhook, MockWebhookManager):
        return hass.webhook
    if isinstance(getattr(hass, "data", None), dict) and "webhook" in hass.data:
        data_webhook = hass.data["webhook"]
        if isinstance(data_webhook, MockWebhookManager):
            return data_webhook
    mgr = MockWebhookManager(hass)
    try:
        hass.webhook = mgr
    except Exception:
        pass
    return mgr


def webhook_async_register(
    hass: Any,
    domain: str,
    name: str,
    webhook_id: str,
    handler: Callable,
    allowed_methods: Sequence[str] | None = None,
    local_only: bool = False,
) -> None:
    mgr = _get_webhook_manager(hass)
    mgr.async_register(domain, name, webhook_id, handler, allowed_methods, local_only)


def webhook_async_unregister(hass: Any, webhook_id: str) -> None:
    mgr = _get_webhook_manager(hass)
    mgr.async_unregister(webhook_id)


def webhook_async_generate_url(hass: Any, webhook_id: str) -> str:
    mgr = _get_webhook_manager(hass)
    return mgr.async_generate_url(webhook_id)


def webhook_async_generate_path(*args: Any, **kwargs: Any) -> str:
    if "webhook_id" in kwargs:
        webhook_id = kwargs["webhook_id"]
    elif len(args) == 1:
        webhook_id = args[0]
    elif len(args) >= 2:
        webhook_id = args[1]
    else:
        raise TypeError("async_generate_path missing required argument 'webhook_id'")
    return f"/api/webhook/{webhook_id}"


async def webhook_async_handle_webhook(hass: Any, webhook_id: str, request: Any) -> Any:
    mgr = _get_webhook_manager(hass)
    return await mgr.async_handle_webhook(webhook_id, request)


class Selector:
    """Classe de base pour les sélecteurs Home Assistant."""
    def __init__(self, config: Any = None) -> None:
        self.config = config

    def __call__(self, data: Any) -> Any:
        return data

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} config={self.config!r}>"


@dataclass
class EntitySelectorConfig:
    """Configuration du sélecteur d'entités."""
    include_entities: list[str] | None = None
    exclude_entities: list[str] | None = None
    domain: str | list[str] | None = None
    device_class: str | list[str] | None = None
    multiple: bool = False
    filter: Any = None

    def __init__(self, **kwargs: Any) -> None:
        self.include_entities = kwargs.get("include_entities")
        self.exclude_entities = kwargs.get("exclude_entities")
        self.domain = kwargs.get("domain")
        self.device_class = kwargs.get("device_class")
        self.multiple = kwargs.get("multiple", False)
        self.filter = kwargs.get("filter")
        for k, v in kwargs.items():
            setattr(self, k, v)


class EntitySelector(Selector):
    """Sélecteur d'entités compatible voluptuous."""
    def __init__(self, config: EntitySelectorConfig | dict[str, Any] | None = None) -> None:
        if isinstance(config, dict):
            config = EntitySelectorConfig(**config)
        super().__init__(config or EntitySelectorConfig())

    def __call__(self, data: Any) -> Any:
        if data is None:
            return [] if (self.config and getattr(self.config, "multiple", False)) else None
        if self.config and getattr(self.config, "multiple", False):
            if isinstance(data, (list, tuple, set)):
                return list(data)
            return [str(data)]
        if isinstance(data, (list, tuple)) and len(data) == 1:
            return data[0]
        return data


class SelectSelectorMode(str, Enum):
    LIST = "list"
    DROPDOWN = "dropdown"


@dataclass
class SelectSelectorConfig:
    options: list[Any] = field(default_factory=list)
    multiple: bool = False
    custom_value: bool = False
    mode: SelectSelectorMode = SelectSelectorMode.DROPDOWN
    translation_key: str | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.options = kwargs.get("options", [])
        self.multiple = kwargs.get("multiple", False)
        self.custom_value = kwargs.get("custom_value", False)
        self.mode = kwargs.get("mode", SelectSelectorMode.DROPDOWN)
        self.translation_key = kwargs.get("translation_key")
        for k, v in kwargs.items():
            setattr(self, k, v)


class SelectSelector(Selector):
    def __init__(self, config: SelectSelectorConfig | dict[str, Any] | None = None) -> None:
        if isinstance(config, dict):
            config = SelectSelectorConfig(**config)
        super().__init__(config or SelectSelectorConfig())


@dataclass
class BooleanSelectorConfig:
    pass


class BooleanSelector(Selector):
    def __init__(self, config: BooleanSelectorConfig | dict[str, Any] | None = None) -> None:
        if isinstance(config, dict):
            config = BooleanSelectorConfig()
        super().__init__(config or BooleanSelectorConfig())

    def __call__(self, data: Any) -> bool:
        return bool(data)


class NumberSelectorMode(str, Enum):
    BOX = "box"
    SLIDER = "slider"


@dataclass
class NumberSelectorConfig:
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit_of_measurement: str | None = None
    mode: NumberSelectorMode = NumberSelectorMode.BOX

    def __init__(self, **kwargs: Any) -> None:
        self.min = kwargs.get("min", 0.0)
        self.max = kwargs.get("max", 100.0)
        self.step = kwargs.get("step", 1.0)
        self.unit_of_measurement = kwargs.get("unit_of_measurement")
        self.mode = kwargs.get("mode", NumberSelectorMode.BOX)
        for k, v in kwargs.items():
            setattr(self, k, v)


class NumberSelector(Selector):
    def __init__(self, config: NumberSelectorConfig | dict[str, Any] | None = None) -> None:
        if isinstance(config, dict):
            config = NumberSelectorConfig(**config)
        super().__init__(config or NumberSelectorConfig())


def selector_factory(config: dict[str, Any]) -> Selector:
    """Fabrique générique de sélecteur à partir d'un dictionnaire."""
    if "entity" in config:
        cfg = config["entity"]
        if isinstance(cfg, dict):
            return EntitySelector(EntitySelectorConfig(**cfg))
        return EntitySelector(cfg)
    if "select" in config:
        cfg = config["select"]
        if isinstance(cfg, dict):
            return SelectSelector(SelectSelectorConfig(**cfg))
        return SelectSelector(cfg)
    if "boolean" in config:
        return BooleanSelector(BooleanSelectorConfig())
    if "number" in config:
        cfg = config["number"]
        if isinstance(cfg, dict):
            return NumberSelector(NumberSelectorConfig(**cfg))
        return NumberSelector(cfg)
    return Selector(config)


class MockWebResponse:
    """Mock de aiohttp.web.Response."""
    def __init__(
        self,
        *,
        body: bytes | str | None = None,
        text: str | None = None,
        status: int = 200,
        content_type: str = "text/plain",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        if text is not None:
            self._text = text
            self._body = text.encode("utf-8")
        elif isinstance(body, str):
            self._text = body
            self._body = body.encode("utf-8")
        elif isinstance(body, bytes):
            self._body = body
            self._text = body.decode("utf-8", errors="replace")
        else:
            self._text = ""
            self._body = b""
        self.content_type = content_type
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self._text

    @property
    def body(self) -> bytes:
        return self._body

    def __repr__(self) -> str:
        return f"<Response status={self.status} text={self.text!r}>"


def mock_json_response(data: Any = None, status: int = 200, **kwargs: Any) -> MockWebResponse:
    """Mock de aiohttp.web.json_response."""
    return MockWebResponse(
        text=json.dumps(data),
        status=status,
        content_type="application/json",
        **kwargs,
    )


class HomeAssistant:
    """Mock de l'instance HomeAssistant Core."""
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.states = MockStateMachine()
        self.config_entries = MockConfigEntriesManager(self)
        self.device_registry = DeviceRegistry(self)
        self.services = MockServices(self)
        self.bus = MockBus(self)
        self.webhook = MockWebhookManager(self)
        self.config = MockConfig()
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

    def async_create_task(self, coro: Any) -> asyncio.Task:
        return self.loop.create_task(coro)

    async def async_add_executor_job(self, fn: Callable, *args: Any) -> Any:
        # Exécute de manière synchrone dans le mock ou via to_thread
        return await asyncio.to_thread(fn, *args)


def callback(fn: Callable) -> Callable:
    """Décorateur callback no-op pour Home Assistant."""
    return fn

# ============================================================================
# 8. SHIM CONST & ENUMS (Phase 3)
# ============================================================================

class Platform(str, Enum):
    """Plateformes Home Assistant supportées."""
    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    BUTTON = "button"
    SWITCH = "switch"
    LIGHT = "light"
    MEDIA_PLAYER = "media_player"
    SCENE = "scene"


class EntityCategory(str, Enum):
    """Catégories d'entités Home Assistant."""
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


PERCENTAGE = "%"
SIGNAL_STRENGTH_DECIBELS_MILLIWATT = "dBm"


class UnitOfSignalStrength:
    """Unités de force de signal."""
    DECIBELS_MILLIWATT = "dBm"
    dBm = "dBm"


STATE_ON = "on"
STATE_OFF = "off"
STATE_UNAVAILABLE = "unavailable"
STATE_UNKNOWN = "unknown"


# ============================================================================
# 9. SHIM BASE ENTITY & DEVICE INFO (Phase 3)
# ============================================================================

@dataclass
class DeviceInfo:
    """Informations de rattachement au Device Registry Home Assistant."""
    identifiers: set[tuple[str, str]] = field(default_factory=set)
    connections: set[tuple[str, str]] = field(default_factory=set)
    manufacturer: str | None = None
    model: str | None = None
    name: str | None = None
    sw_version: str | None = None
    hw_version: str | None = None
    suggested_area: str | None = None
    configuration_url: str | None = None
    via_device: tuple[str, str] | None = None

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

    def __contains__(self, item: str) -> bool:
        return hasattr(self, item)


@dataclass
class EntityDescription:
    """Description de base pour une entité Home Assistant."""
    key: str
    name: str | None = None
    icon: str | None = None
    entity_category: EntityCategory | None = None
    has_entity_name: bool = True


class Entity:
    """Classe de base Entity conforme Home Assistant Core."""
    _attr_has_entity_name: bool = False
    _attr_name: str | None = None
    _attr_unique_id: str | None = None
    _attr_device_info: DeviceInfo | dict[str, Any] | None = None
    _attr_entity_category: EntityCategory | None = None
    _attr_extra_state_attributes: dict[str, Any] | None = None
    _attr_available: bool = True
    _attr_should_poll: bool = True
    _attr_translation_key: str | None = None
    _attr_icon: str | None = None

    def __init__(self) -> None:
        self.hass: Any = None
        self.entity_id: str | None = None
        self._remove_callbacks: list[Callable[[], None]] = []

    @property
    def has_entity_name(self) -> bool:
        return self._attr_has_entity_name

    @property
    def name(self) -> str | None:
        return self._attr_name

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    def device_info(self) -> DeviceInfo | dict[str, Any] | None:
        return self._attr_device_info

    @property
    def entity_category(self) -> EntityCategory | None:
        return self._attr_entity_category

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attr_extra_state_attributes or {}

    @property
    def available(self) -> bool:
        return self._attr_available

    @property
    def should_poll(self) -> bool:
        return self._attr_should_poll

    @property
    def translation_key(self) -> str | None:
        return self._attr_translation_key

    @property
    def icon(self) -> str | None:
        return self._attr_icon

    def async_write_ha_state(self) -> None:
        """Écrit l'état de l'entité dans hass.states si disponible."""
        if hasattr(self, "hass") and self.hass and hasattr(self.hass, "states"):
            if not self.available:
                state_val: Any = "unavailable"
            else:
                state_val = getattr(self, "state", None)
                if state_val is None:
                    state_val = getattr(self, "native_value", None)
                if state_val is None and hasattr(self, "is_on"):
                    is_on = getattr(self, "is_on")
                    state_val = "on" if is_on else ("off" if is_on is False else None)

            attrs = dict(getattr(self, "extra_state_attributes", {}) or {})
            if hasattr(self, "device_class") and self.device_class:
                attrs["device_class"] = str(self.device_class)
            if hasattr(self, "native_unit_of_measurement") and self.native_unit_of_measurement:
                attrs["unit_of_measurement"] = self.native_unit_of_measurement
            if hasattr(self, "state_class") and self.state_class:
                attrs["state_class"] = str(self.state_class)

            target_id = self.entity_id or getattr(self, "unique_id", "entity.unknown")
            self.hass.states.async_set(target_id, str(state_val) if state_val is not None else "unknown", attrs)

    def async_on_remove(self, func: Callable[[], None]) -> None:
        self._remove_callbacks.append(func)

    async def async_added_to_hass(self) -> None:
        """Hook asynchrone invoqué lors de l'enregistrement de l'entité."""
        pass

    async def async_will_remove_from_hass(self) -> None:
        """Hook asynchrone invoqué lors du retrait de l'entité."""
        for cb in list(self._remove_callbacks):
            cb()
        self._remove_callbacks.clear()


# ============================================================================
# 10. SHIM UPDATE COORDINATOR (Phase 3)
# ============================================================================

class UpdateFailed(HomeAssistantError):
    """Exception levée en cas d'échec de mise à jour des données."""


class DataUpdateCoordinator:
    """Coordinateur de mise à jour des données conforme Home Assistant Core."""

    def __class_getitem__(cls, item: Any) -> Any:
        return cls

    def __init__(
        self,
        hass: Any,
        logger: logging.Logger,
        *,
        name: str,
        update_interval: datetime.timedelta | None = None,
        update_method: Callable[[], Any] | None = None,
        request_refresh_debouncer: Any = None,
        always_update: bool = True,
    ) -> None:
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.update_method = update_method
        self.data: Any = None
        self.last_update_success: bool = True
        self.last_exception: Exception | None = None
        self._listeners: list[Callable[[], None]] = []

    def async_add_listener(self, update_callback: Callable[[], None], context: Any = None) -> Callable[[], None]:
        self._listeners.append(update_callback)
        def remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)
        return remove

    def async_update_listeners(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def _async_update_data(self) -> Any:
        if self.update_method is not None:
            return await self.update_method()
        raise NotImplementedError

    async def async_refresh(self) -> None:
        try:
            self.data = await self._async_update_data()
            self.last_update_success = True
            self.last_exception = None
        except Exception as err:
            self.last_update_success = False
            self.last_exception = err
            if not isinstance(err, UpdateFailed):
                if hasattr(self.logger, "error"):
                    self.logger.error("Erreur coordinator %s: %s", self.name, err)
        finally:
            self.async_update_listeners()

    async def async_request_refresh(self) -> None:
        await self.async_refresh()

    async def async_config_entry_first_refresh(self) -> None:
        await self.async_refresh()
        if not self.last_update_success:
            if isinstance(self.last_exception, ConfigEntryAuthFailed):
                raise self.last_exception
            raise ConfigEntryNotReady(f"Update failed for {self.name}") from self.last_exception


class CoordinatorEntity(Entity):
    """Entité liée à un DataUpdateCoordinator conforme Home Assistant."""

    def __class_getitem__(cls, item: Any) -> Any:
        return cls

    def __init__(self, coordinator: DataUpdateCoordinator, context: Any = None) -> None:
        super().__init__()
        self.coordinator = coordinator
        self._coordinator_context = context

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success and self._attr_available)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


# ============================================================================
# 11. SHIM SENSOR, BINARY SENSOR & BUTTON (Phase 3)
# ============================================================================

class SensorDeviceClass(str, Enum):
    """Classes d'appareils pour capteurs numériques."""
    BATTERY = "battery"
    SIGNAL_STRENGTH = "signal_strength"
    TIMESTAMP = "timestamp"
    POWER = "power"
    TEMPERATURE = "temperature"


class SensorStateClass(str, Enum):
    """Classes d'état pour capteurs statistiques."""
    MEASUREMENT = "measurement"
    TOTAL = "total"
    TOTAL_INCREASING = "total_increasing"


class SensorEntity(Entity):
    """Classe de base SensorEntity conforme Home Assistant."""
    _attr_native_value: Any = None
    _attr_native_unit_of_measurement: str | None = None
    _attr_device_class: SensorDeviceClass | None = None
    _attr_state_class: SensorStateClass | None = None
    _attr_suggested_display_precision: int | None = None

    @property
    def native_value(self) -> Any:
        return self._attr_native_value

    @property
    def native_unit_of_measurement(self) -> str | None:
        return self._attr_native_unit_of_measurement

    @property
    def device_class(self) -> SensorDeviceClass | None:
        return self._attr_device_class

    @property
    def state_class(self) -> SensorStateClass | None:
        return self._attr_state_class

    @property
    def suggested_display_precision(self) -> int | None:
        return self._attr_suggested_display_precision

    @property
    def state(self) -> Any:
        return self.native_value


@dataclass
class SensorEntityDescription(EntityDescription):
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    native_unit_of_measurement: str | None = None


class BinarySensorDeviceClass(str, Enum):
    """Classes d'appareils pour capteurs binaires."""
    BATTERY_CHARGING = "battery_charging"
    CONNECTIVITY = "connectivity"
    PROBLEM = "problem"
    PLUG = "plug"
    RUNNING = "running"
    POWER = "power"


class BinarySensorEntity(Entity):
    """Classe de base BinarySensorEntity conforme Home Assistant."""
    _attr_is_on: bool | None = None
    _attr_device_class: BinarySensorDeviceClass | None = None

    @property
    def is_on(self) -> bool | None:
        return self._attr_is_on

    @property
    def device_class(self) -> BinarySensorDeviceClass | None:
        return self._attr_device_class

    @property
    def state(self) -> str | None:
        if self.is_on is None:
            return None
        return "on" if self.is_on else "off"


@dataclass
class BinarySensorEntityDescription(EntityDescription):
    device_class: BinarySensorDeviceClass | None = None


class ButtonDeviceClass(str, Enum):
    """Classes d'appareils pour boutons d'action."""
    RESTART = "restart"
    UPDATE = "update"
    IDENTIFY = "identify"


class ButtonEntity(Entity):
    """Classe de base ButtonEntity conforme Home Assistant."""
    _attr_device_class: ButtonDeviceClass | None = None

    @property
    def device_class(self) -> ButtonDeviceClass | None:
        return self._attr_device_class

    async def async_press(self) -> None:
        if hasattr(self, "press"):
            if hasattr(self, "hass") and self.hass and hasattr(self.hass, "async_add_executor_job"):
                await self.hass.async_add_executor_job(self.press)
            else:
                self.press()
        else:
            raise NotImplementedError

    def press(self) -> None:
        raise NotImplementedError


@dataclass
class ButtonEntityDescription(EntityDescription):
    device_class: ButtonDeviceClass | None = None


# ============================================================================
# 7. INJECTION DANS SYS.MODULES
# ============================================================================

def install_ha_mocks() -> None:
    """Installe tous les shims Home Assistant et voluptuous dans sys.modules."""
    # voluptuous
    vol_mod = types.ModuleType("voluptuous")
    vol_mod.Schema = Schema
    vol_mod.Required = Required
    vol_mod.Optional = Optional
    vol_mod.All = All
    vol_mod.Coerce = Coerce
    vol_mod.Range = Range
    vol_mod.In = In
    vol_mod.Invalid = Invalid
    vol_mod.MultipleInvalid = MultipleInvalid
    sys.modules["voluptuous"] = vol_mod

    # homeassistant (Package marker required for subpackage imports)
    ha_mod = types.ModuleType("homeassistant")
    ha_mod.__path__ = []
    sys.modules["homeassistant"] = ha_mod

    # homeassistant.core
    core_mod = types.ModuleType("homeassistant.core")
    core_mod.HomeAssistant = HomeAssistant
    core_mod.callback = callback
    core_mod.State = State
    core_mod.MockStateMachine = MockStateMachine
    core_mod.ServiceCall = ServiceCall
    core_mod.MockServices = MockServices
    core_mod.ServiceRegistry = MockServices
    core_mod.Event = Event
    core_mod.MockBus = MockBus
    core_mod.EventBus = MockBus
    sys.modules["homeassistant.core"] = core_mod
    ha_mod.core = core_mod

    # homeassistant.exceptions
    exc_mod = types.ModuleType("homeassistant.exceptions")
    exc_mod.HomeAssistantError = HomeAssistantError
    exc_mod.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    exc_mod.ConfigEntryNotReady = ConfigEntryNotReady
    sys.modules["homeassistant.exceptions"] = exc_mod
    ha_mod.exceptions = exc_mod

    # homeassistant.data_entry_flow
    flow_mod = types.ModuleType("homeassistant.data_entry_flow")
    flow_mod.FlowResultType = FlowResultType
    flow_mod.FlowResult = FlowResult
    flow_mod.AbortFlow = AbortFlow
    flow_mod.FlowHandler = FlowHandler
    sys.modules["homeassistant.data_entry_flow"] = flow_mod
    ha_mod.data_entry_flow = flow_mod

    # homeassistant.config_entries
    cfg_mod = types.ModuleType("homeassistant.config_entries")
    cfg_mod.ConfigEntry = ConfigEntry
    cfg_mod.ConfigEntryState = ConfigEntryState
    cfg_mod.ConfigFlow = ConfigFlow
    cfg_mod.OptionsFlow = OptionsFlow
    cfg_mod.HANDLERS = HANDLERS
    cfg_mod.SOURCE_USER = "user"
    cfg_mod.SOURCE_ZEROCONF = "zeroconf"
    cfg_mod.SOURCE_RECONFIGURE = "reconfigure"
    cfg_mod.SOURCE_REAUTH = "reauth"
    cfg_mod.SOURCE_IMPORT = "import"
    sys.modules["homeassistant.config_entries"] = cfg_mod
    ha_mod.config_entries = cfg_mod

    # homeassistant.components (Package marker required)
    components_mod = types.ModuleType("homeassistant.components")
    components_mod.__path__ = []
    sys.modules["homeassistant.components"] = components_mod
    ha_mod.components = components_mod

    # homeassistant.components.zeroconf
    zc_mod = types.ModuleType("homeassistant.components.zeroconf")
    zc_mod.ZeroconfServiceInfo = ZeroconfServiceInfo
    sys.modules["homeassistant.components.zeroconf"] = zc_mod
    components_mod.zeroconf = zc_mod

    # homeassistant.components.webhook
    webhook_mod = types.ModuleType("homeassistant.components.webhook")
    webhook_mod.async_register = webhook_async_register
    webhook_mod.async_unregister = webhook_async_unregister
    webhook_mod.async_generate_url = webhook_async_generate_url
    webhook_mod.async_generate_path = webhook_async_generate_path
    webhook_mod.async_handle_webhook = webhook_async_handle_webhook
    webhook_mod.MockWebhookManager = MockWebhookManager
    webhook_mod.WebhookInfo = WebhookInfo
    webhook_mod.DEFAULT_METHODS = DEFAULT_WEBHOOK_METHODS
    sys.modules["homeassistant.components.webhook"] = webhook_mod
    components_mod.webhook = webhook_mod

    # homeassistant.helpers (Package marker required)
    helpers_mod = types.ModuleType("homeassistant.helpers")
    helpers_mod.__path__ = []
    sys.modules["homeassistant.helpers"] = helpers_mod
    ha_mod.helpers = helpers_mod

    # homeassistant.helpers.device_registry
    dr_mod = types.ModuleType("homeassistant.helpers.device_registry")
    dr_mod.CONNECTION_NETWORK_MAC = CONNECTION_NETWORK_MAC
    dr_mod.DeviceEntry = DeviceEntry
    dr_mod.DeviceRegistry = DeviceRegistry
    dr_mod.DeviceInfo = DeviceInfo
    dr_mod.async_get = lambda hass: getattr(hass, "device_registry", DeviceRegistry(hass))
    sys.modules["homeassistant.helpers.device_registry"] = dr_mod
    helpers_mod.device_registry = dr_mod

    # homeassistant.helpers.aiohttp_client
    http_mod = types.ModuleType("homeassistant.helpers.aiohttp_client")
    def _async_create_clientsession(hass: Any, verify_ssl: bool = True, auto_cleanup: bool = True, connector: Any = None) -> Any:
        mod = sys.modules.get("aiohttp")
        if mod and hasattr(mod, "ClientSession"):
            return mod.ClientSession(connector=connector)
        raise RuntimeError("aiohttp module not found in sys.modules")
    http_mod.async_create_clientsession = _async_create_clientsession
    http_mod.async_get_clientsession = lambda hass, verify_ssl=True: sys.modules["aiohttp"].ClientSession()
    sys.modules["homeassistant.helpers.aiohttp_client"] = http_mod
    helpers_mod.aiohttp_client = http_mod

    # homeassistant.helpers.selector
    selector_mod = types.ModuleType("homeassistant.helpers.selector")
    selector_mod.Selector = Selector
    selector_mod.EntitySelector = EntitySelector
    selector_mod.EntitySelectorConfig = EntitySelectorConfig
    selector_mod.SelectSelector = SelectSelector
    selector_mod.SelectSelectorConfig = SelectSelectorConfig
    selector_mod.SelectSelectorMode = SelectSelectorMode
    selector_mod.NumberSelector = NumberSelector
    selector_mod.NumberSelectorConfig = NumberSelectorConfig
    selector_mod.NumberSelectorMode = NumberSelectorMode
    selector_mod.BooleanSelector = BooleanSelector
    selector_mod.BooleanSelectorConfig = BooleanSelectorConfig
    selector_mod.selector = selector_factory
    sys.modules["homeassistant.helpers.selector"] = selector_mod
    helpers_mod.selector = selector_mod

    # homeassistant.helpers.service_info
    service_info_mod = types.ModuleType("homeassistant.helpers.service_info")
    service_info_mod.__path__ = []
    sys.modules["homeassistant.helpers.service_info"] = service_info_mod
    helpers_mod.service_info = service_info_mod

    service_info_zc_mod = types.ModuleType("homeassistant.helpers.service_info.zeroconf")
    service_info_zc_mod.ZeroconfServiceInfo = ZeroconfServiceInfo
    sys.modules["homeassistant.helpers.service_info.zeroconf"] = service_info_zc_mod
    service_info_mod.zeroconf = service_info_zc_mod

    # aiohttp.web enrichment
    web_mod = sys.modules.get("aiohttp.web")
    if web_mod is None:
        web_mod = types.ModuleType("aiohttp.web")
        sys.modules["aiohttp.web"] = web_mod
        if "aiohttp" in sys.modules:
            sys.modules["aiohttp"].web = web_mod
    if not hasattr(web_mod, "Response"):
        web_mod.Response = MockWebResponse
    if not hasattr(web_mod, "json_response"):
        web_mod.json_response = mock_json_response
    aiohttp_mod = sys.modules.get("aiohttp")
    if aiohttp_mod and not hasattr(aiohttp_mod, "web"):
        aiohttp_mod.web = web_mod

    # homeassistant.const
    const_mod = types.ModuleType("homeassistant.const")
    const_mod.Platform = Platform
    const_mod.EntityCategory = EntityCategory
    const_mod.CONF_HOST = "host"
    const_mod.CONF_PORT = "port"
    const_mod.PERCENTAGE = PERCENTAGE
    const_mod.SIGNAL_STRENGTH_DECIBELS_MILLIWATT = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    const_mod.UnitOfSignalStrength = UnitOfSignalStrength
    const_mod.STATE_ON = STATE_ON
    const_mod.STATE_OFF = STATE_OFF
    const_mod.STATE_UNAVAILABLE = STATE_UNAVAILABLE
    const_mod.STATE_UNKNOWN = STATE_UNKNOWN
    const_mod.MATCH_ALL = "*"
    const_mod.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
    const_mod.EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
    sys.modules["homeassistant.const"] = const_mod
    ha_mod.const = const_mod

    # homeassistant.helpers.entity
    entity_mod = types.ModuleType("homeassistant.helpers.entity")
    entity_mod.Entity = Entity
    entity_mod.DeviceInfo = DeviceInfo
    entity_mod.EntityCategory = EntityCategory
    entity_mod.EntityDescription = EntityDescription
    sys.modules["homeassistant.helpers.entity"] = entity_mod
    helpers_mod.entity = entity_mod

    # homeassistant.helpers.entity_platform
    ep_mod = types.ModuleType("homeassistant.helpers.entity_platform")
    ep_mod.AddEntitiesCallback = Callable[..., Any]
    sys.modules["homeassistant.helpers.entity_platform"] = ep_mod
    helpers_mod.entity_platform = ep_mod

    # homeassistant.helpers.update_coordinator
    coord_mod = types.ModuleType("homeassistant.helpers.update_coordinator")
    coord_mod.DataUpdateCoordinator = DataUpdateCoordinator
    coord_mod.CoordinatorEntity = CoordinatorEntity
    coord_mod.UpdateFailed = UpdateFailed
    sys.modules["homeassistant.helpers.update_coordinator"] = coord_mod
    helpers_mod.update_coordinator = coord_mod

    # homeassistant.components.sensor
    sensor_mod = types.ModuleType("homeassistant.components.sensor")
    sensor_mod.SensorEntity = SensorEntity
    sensor_mod.SensorDeviceClass = SensorDeviceClass
    sensor_mod.SensorStateClass = SensorStateClass
    sensor_mod.SensorEntityDescription = SensorEntityDescription
    sensor_mod.DOMAIN = "sensor"
    sys.modules["homeassistant.components.sensor"] = sensor_mod
    components_mod.sensor = sensor_mod

    # homeassistant.components.binary_sensor
    bs_mod = types.ModuleType("homeassistant.components.binary_sensor")
    bs_mod.BinarySensorEntity = BinarySensorEntity
    bs_mod.BinarySensorDeviceClass = BinarySensorDeviceClass
    bs_mod.BinarySensorEntityDescription = BinarySensorEntityDescription
    bs_mod.DOMAIN = "binary_sensor"
    sys.modules["homeassistant.components.binary_sensor"] = bs_mod
    components_mod.binary_sensor = bs_mod

    # homeassistant.components.button
    btn_mod = types.ModuleType("homeassistant.components.button")
    btn_mod.ButtonEntity = ButtonEntity
    btn_mod.ButtonDeviceClass = ButtonDeviceClass
    btn_mod.ButtonEntityDescription = ButtonEntityDescription
    btn_mod.DOMAIN = "button"
    sys.modules["homeassistant.components.button"] = btn_mod
    components_mod.button = btn_mod

install_ha_mocks()
