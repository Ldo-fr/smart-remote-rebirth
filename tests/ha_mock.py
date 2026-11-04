"""Shims et mocks Home Assistant complets pour exécution autonome des tests (Phase 2).

Fournit les modules et classes nécessaires pour tester config_flow, lifecycle,
reconfiguration, reauthentification, options flow et device registry sans dépendance
vers Home Assistant Core ou voluptuous externe.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import sys
import types
from typing import Any, Callable, Mapping, Sequence

try:
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
        if unique_id is not None:
            entry.unique_id = unique_id
        for listener in list(entry._update_listeners):
            self.hass.async_create_task(listener(self.hass, entry))

    async def async_reload(self, entry_id: str) -> bool:
        self.reload_count += 1
        entry = self.async_get_entry(entry_id)
        if entry:
            # Si le composant a été initialisé, simuler unload puis setup
            from custom_components.sevenhugs_smart_remote import async_setup_entry, async_unload_entry
            await async_unload_entry(self.hass, entry)
            await async_setup_entry(self.hass, entry)
        return True

    async def async_forward_entry_setups(self, entry: ConfigEntry, platforms: list[str]) -> bool:
        return True


class HomeAssistant:
    """Mock de l'instance HomeAssistant Core."""
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.config_entries = MockConfigEntriesManager(self)
        self.device_registry = DeviceRegistry(self)
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

    # homeassistant
    ha_mod = types.ModuleType("homeassistant")
    sys.modules["homeassistant"] = ha_mod

    # homeassistant.core
    core_mod = types.ModuleType("homeassistant.core")
    core_mod.HomeAssistant = HomeAssistant
    core_mod.callback = callback
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

    # homeassistant.components
    components_mod = types.ModuleType("homeassistant.components")
    sys.modules["homeassistant.components"] = components_mod
    ha_mod.components = components_mod

    # homeassistant.components.zeroconf
    zc_mod = types.ModuleType("homeassistant.components.zeroconf")
    zc_mod.ZeroconfServiceInfo = ZeroconfServiceInfo
    sys.modules["homeassistant.components.zeroconf"] = zc_mod
    components_mod.zeroconf = zc_mod

    # homeassistant.helpers
    helpers_mod = types.ModuleType("homeassistant.helpers")
    sys.modules["homeassistant.helpers"] = helpers_mod
    ha_mod.helpers = helpers_mod

    # homeassistant.helpers.device_registry
    dr_mod = types.ModuleType("homeassistant.helpers.device_registry")
    dr_mod.CONNECTION_NETWORK_MAC = CONNECTION_NETWORK_MAC
    dr_mod.DeviceEntry = DeviceEntry
    dr_mod.DeviceRegistry = DeviceRegistry
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

install_ha_mocks()
