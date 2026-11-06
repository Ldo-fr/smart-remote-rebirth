"""Flux de configuration Home Assistant pour Sevenhugs Smart Remote."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries

try:
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except (ImportError, ModuleNotFoundError):
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow, FlowResult

try:
    from homeassistant.data_entry_flow import section as ha_section
except ImportError:
    ha_section = None  # type: ignore[assignment]

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)

from .client import AsyncSmartRemoteClient, SevenhugsAuthError, SevenhugsConnectionError
from .const import (
    CONF_AUTO_SYNC_WEBHOOKS,
    CONF_CLIENT_PRIVATE_KEY,
    CONF_CLIENT_PUBLIC_KEY,
    CONF_CONTROLLER_ID,
    CONF_DELETE_DEVICES,
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
    DEFAULT_TRIGGER_RESYNC,
    DOMAIN,
    ERROR_CANNOT_CONNECT,
    ERROR_INVALID_PIN,
    ERROR_PAIRING_FAILED,
    ERROR_REMOTE_BUSY,
    ERROR_TIMEOUT_CONNECT,
    HARDWARE_VERSION,
    MANUFACTURER,
    MAX_POLLING_INTERVAL,
    MIN_POLLING_INTERVAL,
    MODEL_SR1A,
    TIMEOUT_CONNECT_CHECK,
)

_LOGGER = logging.getLogger(__name__)


def _decode_prop(val: Any) -> str:
    """Décode une propriété Zeroconf bytes ou str."""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val) if val is not None else ""


class SevenhugsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Gestionnaire du flux de configuration Sevenhugs Smart Remote."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise l'état interne du flux."""
        super().__init__()
        self.host: str = ""
        self.port: int = DEFAULT_PORT
        self.serial_number: str = ""
        self.remote_id: str = ""
        self.fw_version: str = ""
        self.mac_address: str = ""
        self.salt: bytes = b""
        self.b_bytes: bytes = b""
        self._pair_client: AsyncSmartRemoteClient | None = None
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Étape manuelle de saisie de l'hôte et du port."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))

            try:
                # Vérification de connectivité HTTP/TCP port 80
                if await self._async_probe_host(host, port):
                    self.host = host
                    self.port = port
                    return await self.async_step_pair_init()

                errors["base"] = ERROR_CANNOT_CONNECT
            except Exception as err:
                _LOGGER.exception("Erreur inattendue lors de la saisie utilisateur: %s", err)
                errors["base"] = ERROR_CANNOT_CONNECT

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST): str,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
            }),
            errors=errors,
        )

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> FlowResult:
        """Étape de découverte automatique via Zeroconf mDNS."""
        props = discovery_info.properties or {}

        sn = _decode_prop(props.get("sn") or props.get(b"sn"))
        remote_id = _decode_prop(props.get("id") or props.get(b"id"))
        fw = _decode_prop(props.get("fw") or props.get(b"fw"))
        host = discovery_info.host
        port = discovery_info.port or DEFAULT_PORT

        if sn:
            await self.async_set_unique_id(sn)
            # Règle Gold discovery-update-connection : mise à jour dynamique de l'IP
            self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})

        self.host = host
        self.port = port
        self.serial_number = sn
        self.remote_id = remote_id
        self.fw_version = fw

        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Dialogue de confirmation pour la télécommande découverte."""
        if user_input is not None:
            return await self.async_step_pair_init()

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": f"Smart Remote ({self.remote_id or 'Sevenhugs'})",
                "host": self.host,
                "sn": self.serial_number or "Inconnu",
            },
        )

    async def async_step_pair_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Initialisation de l'appairage non-bloquante avec écran d'attente (~16s)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            target_host = f"{self.host}:{self.port}" if self.port and self.port != 80 else self.host

            session = async_create_clientsession(
                self.hass,
                verify_ssl=False,
                auto_cleanup=False,
            )
            client = AsyncSmartRemoteClient(
                host=target_host,
                session=session,
                keys={},
            )

            try:
                # Requête M1 tolérant les ~16s de calcul du microcontrôleur sans bloquer l'Event Loop
                salt, b_bytes = await client.async_pair_setup_m1()
                self._pair_client = client
                self.salt = salt
                self.b_bytes = b_bytes
                return await self.async_step_pair_pin()

            except SevenhugsConnectionError as err:
                err_msg = str(err).lower()
                if "500" in err_msg or "busy" in err_msg:
                    errors["base"] = ERROR_REMOTE_BUSY
                elif "timeout" in err_msg:
                    errors["base"] = ERROR_TIMEOUT_CONNECT
                else:
                    errors["base"] = ERROR_CANNOT_CONNECT
                await client.close()

            except TimeoutError:
                errors["base"] = ERROR_TIMEOUT_CONNECT
                await client.close()

            except Exception as err:
                _LOGGER.exception("Erreur inattendue lors de pair_setup_m1: %s", err)
                errors["base"] = ERROR_PAIRING_FAILED
                await client.close()

        return self.async_show_form(
            step_id="pair_init",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def async_step_pair_pin(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Saisie et validation du code PIN, échange cryptographique et création ConfigEntry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            raw_pin = str(user_input.get("pin", ""))
            clean_pin = re.sub(r"[^0-9]", "", raw_pin)

            # Rejet immédiat si format invalide (doit contenir exactement 8 chiffres)
            if len(clean_pin) != 8:
                errors["base"] = ERROR_INVALID_PIN
                return self.async_show_form(
                    step_id="pair_pin",
                    data_schema=vol.Schema({vol.Required("pin", default=raw_pin): str}),
                    errors=errors,
                )

            formatted_pin = f"{clean_pin[:3]}-{clean_pin[3:5]}-{clean_pin[5:]}"

            if not self._pair_client:
                errors["base"] = ERROR_PAIRING_FAILED
                return self.async_show_form(
                    step_id="pair_pin",
                    data_schema=vol.Schema({vol.Required("pin"): str}),
                    errors=errors,
                )

            try:
                # SRP-6a M3-M4 et Ed25519 M5-M6
                remote_id, remote_pk_bytes = await self._pair_client.async_pair_setup_finish(
                    formatted_pin, self.salt, self.b_bytes
                )

                # Pair-Verify M1-M4 immédiat sur la socket Keep-Alive
                await self._pair_client.async_pair_verify()

                # Collecte des métadonnées matérielles réelles
                serial_number = self.serial_number
                mac_address = self.mac_address
                fw_version = self.fw_version
                resolved_id = remote_id or self.remote_id or "5726"

                try:
                    settings = await self._pair_client.async_request("GET", "/api/settings")
                    if isinstance(settings, dict):
                        remotes_list = settings.get("remotes")
                        if isinstance(remotes_list, list) and remotes_list:
                            first_remote = remotes_list[0]
                            serial_number = first_remote.get("serial_number") or serial_number
                            resolved_id = first_remote.get("id") or resolved_id
                            fw_version = first_remote.get("firmware_version") or fw_version
                            mac_address = first_remote.get("mac_address") or mac_address
                        if not serial_number and "serial_number" in settings:
                            serial_number = settings["serial_number"]
                        if not mac_address and "mac_address" in settings:
                            mac_address = settings["mac_address"]
                        if not fw_version and "firmware_version" in settings:
                            fw_version = settings["firmware_version"]
                except Exception as err:
                    _LOGGER.warning("Impossible de récupérer /api/settings: %s", err)

                if serial_number:
                    await self.async_set_unique_id(serial_number, raise_on_progress=False)
                    if not self._reauth_entry:
                        self._abort_if_unique_id_configured()

                keys = getattr(self._pair_client, "keys", getattr(self._pair_client, "_keys", {}))
                entry_data = {
                    CONF_HOST: self.host,
                    CONF_PORT: self.port,
                    CONF_SERIAL_NUMBER: serial_number or "unknown",
                    CONF_CONTROLLER_ID: keys.get(CONF_CONTROLLER_ID, ""),
                    CONF_CLIENT_PRIVATE_KEY: keys.get(CONF_CLIENT_PRIVATE_KEY, ""),
                    CONF_CLIENT_PUBLIC_KEY: keys.get(CONF_CLIENT_PUBLIC_KEY, ""),
                    CONF_REMOTE_ID: resolved_id,
                    CONF_REMOTE_PUBLIC_KEY: keys.get(CONF_REMOTE_PUBLIC_KEY, ""),
                    CONF_SESSION_WRITE_KEY: keys.get(CONF_SESSION_WRITE_KEY, ""),
                    CONF_SESSION_READ_KEY: keys.get(CONF_SESSION_READ_KEY, ""),
                }

                # Fermeture du client temporaire de jumelage
                await self._pair_client.close()
                self._pair_client = None

                # Inscription conditionnelle dans le Device Registry de Home Assistant
                try:
                    entry_id = self._reauth_entry.entry_id if self._reauth_entry else f"7hugs_{serial_number or resolved_id}"
                    device_registry = dr.async_get(self.hass)
                    connections = set()
                    if mac_address:
                        connections.add((dr.CONNECTION_NETWORK_MAC, mac_address))

                    device_registry.async_get_or_create(
                        config_entry_id=entry_id,
                        identifiers={(DOMAIN, serial_number or resolved_id)},
                        connections=connections,
                        manufacturer=MANUFACTURER,
                        model=MODEL_SR1A,
                        name=f"Smart Remote ({resolved_id})",
                        sw_version=fw_version or "0.76.4",
                        hw_version=HARDWARE_VERSION,
                    )
                except Exception as dr_err:
                    _LOGGER.debug("Pré-enregistrement Device Registry ignoré dans le flux (sera finalisé lors du setup): %s", dr_err)

                if self._reauth_entry:
                    return self.async_update_reload_and_abort(
                        self._reauth_entry,
                        data_updates=entry_data,
                        reason="reauth_successful",
                    )

                return self.async_create_entry(
                    title=f"Smart Remote ({resolved_id})",
                    data=entry_data,
                    options={
                        CONF_ENTITIES: list(DEFAULT_ENTITIES),
                        CONF_POLLING_INTERVAL: DEFAULT_POLLING_INTERVAL,
                        CONF_AUTO_SYNC_WEBHOOKS: DEFAULT_AUTO_SYNC_WEBHOOKS,
                        CONF_TIMEOUT: DEFAULT_TIMEOUT,
                        CONF_TRIGGER_RESYNC: DEFAULT_TRIGGER_RESYNC,
                    },
                )

            except AbortFlow:
                if self._pair_client:
                    await self._pair_client.close()
                    self._pair_client = None
                raise
            except SevenhugsAuthError as err:
                _LOGGER.warning("Authentification refusée lors de pair_pin: %s", err)
                errors["base"] = ERROR_INVALID_PIN
            except SevenhugsConnectionError as err:
                _LOGGER.error("Erreur de connexion lors de pair_pin: %s", err)
                errors["base"] = ERROR_PAIRING_FAILED
            except Exception as err:
                _LOGGER.exception("Erreur inattendue lors de pair_pin: %s", err)
                errors["base"] = ERROR_PAIRING_FAILED

        return self.async_show_form(
            step_id="pair_pin",
            data_schema=vol.Schema({vol.Required("pin"): str}),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Reconfiguration de l'adresse IP / hôte sans ré-appairage cryptographique."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            new_host = user_input[CONF_HOST].strip()
            new_port = int(user_input.get(CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_PORT)))

            # Test de connectivité
            if await self._async_probe_host(new_host, new_port):
                # Vérification que le numéro de série correspond au matériel attendu
                if await self._async_verify_device_match(entry, new_host, new_port):
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={CONF_HOST: new_host, CONF_PORT: new_port},
                        reason="reconfigure_successful",
                    )
                errors["base"] = ERROR_CANNOT_CONNECT
            else:
                errors["base"] = ERROR_CANNOT_CONNECT

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=entry.data.get(CONF_HOST, "")): str,
                vol.Optional(CONF_PORT, default=entry.data.get(CONF_PORT, DEFAULT_PORT)): int,
            }),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """Déclenche la ré-authentification pour renouveler les clés de session."""
        self._reauth_entry = self._get_reauth_entry()
        self.host = self._reauth_entry.data.get(CONF_HOST, "")
        self.port = self._reauth_entry.data.get(CONF_PORT, DEFAULT_PORT)
        self.serial_number = self._reauth_entry.data.get(CONF_SERIAL_NUMBER, "")
        self.remote_id = self._reauth_entry.data.get(CONF_REMOTE_ID, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Confirmation de la réauthentification."""
        if user_input is not None:
            return await self.async_step_pair_init({})

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"host": self.host},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Retourne le gestionnaire d'options."""
        return SevenhugsOptionsFlowHandler(config_entry)

    async def _async_probe_host(self, host: str, port: int) -> bool:
        """Vérifie la joignabilité d'un hôte sur le port HTTP 80."""
        target = f"{host}:{port}" if port != 80 else host
        url = f"http://{target}/api/hello"

        try:
            session = async_get_clientsession(self.hass, verify_ssl=False)
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT_CONNECT_CHECK)) as resp:
                # La télécommande retourne 200 sur /api/hello (ou 204/404)
                return resp.status in (200, 204, 404)
        except Exception as err:
            _LOGGER.debug("Échec de la sonde HTTP sur %s: %s", url, err)
            return False

    async def _async_verify_device_match(self, entry: config_entries.ConfigEntry, host: str, port: int) -> bool:
        """Vérifie que le périphérique à la nouvelle adresse correspond au même numéro de série."""
        expected_sn = entry.data.get(CONF_SERIAL_NUMBER)
        if not expected_sn:
            return True

        target = f"{host}:{port}" if port != 80 else host
        session = async_create_clientsession(self.hass, verify_ssl=False, auto_cleanup=False)
        client = AsyncSmartRemoteClient(host=target, session=session, keys=dict(entry.data))

        try:
            await client.async_pair_verify()
            settings = await client.async_request("GET", "/api/settings")
            remotes = settings.get("remotes", [])
            remote_sn = remotes[0].get("serial_number") if remotes else settings.get("serial_number")
            if remote_sn and remote_sn != expected_sn:
                return False
            return True
        except Exception:
            # Si le périphérique a été réinitialisé ou les clés diffèrent, on n'invalide pas par défaut
            return True
        finally:
            await client.close()


class SevenhugsOptionsFlowHandler(config_entries.OptionsFlow):
    """Gestionnaire d'options avancées pour Smart Remote Rebirth."""

    def __init__(self, config_entry: config_entries.ConfigEntry | None = None) -> None:
        """Initialise le gestionnaire d'options (compatible HA 2024.11+ / 2025+)."""
        super().__init__()
        self._config_entry_ref = config_entry
        self._initial_remote_ids: list[str] | None = None
        try:
            self.config_entry = config_entry  # type: ignore[misc]
        except Exception:
            pass

    @property
    def _entry(self) -> Any:
        """Retourne la ConfigEntry associée de manière compatible toutes versions HA."""
        if self._config_entry_ref is not None:
            return self._config_entry_ref
        try:
            return self.config_entry
        except Exception:
            return None

    def _get_remote_carousel_options(self, exclude_webhooks: bool = False) -> list[dict[str, str]]:
        """Extrait la liste des appareils/groupes présents dans le carrousel de la télécommande."""
        hass = getattr(self, "hass", None)
        entry = self._entry
        if not hass or not entry or not hasattr(hass, "data") or not isinstance(hass.data, Mapping):
            return []

        domain_data = hass.data.get(DOMAIN, {})
        runtime_data = domain_data.get(entry.entry_id) if isinstance(domain_data, Mapping) else None
        if not runtime_data:
            return []

        coordinator = getattr(runtime_data, "coordinator", None) or (
            runtime_data.get("coordinator") if isinstance(runtime_data, Mapping) else None
        )
        coord_data = getattr(coordinator, "data", None) if coordinator else None
        if not isinstance(coord_data, Mapping):
            return []

        raw_settings = coord_data.get("raw_settings", {})
        if not isinstance(raw_settings, Mapping):
            return []

        carousel = raw_settings.get("carousel_order", [])
        if not isinstance(carousel, list):
            return []

        select_options: list[dict[str, str]] = []
        for item in carousel:
            if not isinstance(item, Mapping) or not item.get("id"):
                continue
            item_id = str(item["id"])
            if exclude_webhooks and item_id.startswith("sevenhugs.control.webhooks:"):
                continue
            name = str(item.get("name") or item_id)
            category = str(item.get("category") or "device")
            select_options.append({
                "value": item_id,
                "label": f"📟 {name} ({category})",
            })
        return select_options

    def _get_ha_entity_options(self, selected_entities: list[str]) -> list[dict[str, str]]:
        """Construit les options de sélection pour toutes les entités Home Assistant éligibles."""
        hass = getattr(self, "hass", None)
        allowed_domains = set(DEFAULT_EXPOSED_DOMAINS)
        entity_map: dict[str, str] = {}

        if hass is not None and hasattr(hass, "states"):
            states_obj = hass.states
            all_states: list[Any] = []
            try:
                if hasattr(states_obj, "async_all"):
                    res = states_obj.async_all()
                    if isinstance(res, list):
                        all_states = res
                elif hasattr(states_obj, "_states") and isinstance(states_obj._states, Mapping):
                    all_states = list(states_obj._states.values())
            except Exception:
                all_states = []

            for state in all_states:
                ent_id = getattr(state, "entity_id", None)
                if not isinstance(ent_id, str) or "." not in ent_id:
                    continue
                domain = ent_id.partition(".")[0].lower()
                if domain not in allowed_domains:
                    continue
                attrs = getattr(state, "attributes", None)
                friendly = (
                    attrs.get("friendly_name")
                    if isinstance(attrs, Mapping) and attrs.get("friendly_name")
                    else getattr(state, "name", None)
                )
                if isinstance(friendly, str) and friendly.strip() and friendly.strip() != ent_id:
                    entity_map[ent_id] = f"🏠 {friendly.strip()} ({ent_id})"
                else:
                    entity_map[ent_id] = f"🏠 {ent_id}"

        # Garantit que les entités déjà sélectionnées sont toujours présentes dans les options
        for ent_id in selected_entities:
            if isinstance(ent_id, str) and ent_id and ent_id not in entity_map:
                entity_map[ent_id] = f"🏠 {ent_id}"

        selected_set = set(selected_entities)
        selected_opts = [
            {"value": eid, "label": entity_map[eid]}
            for eid in selected_entities
            if eid in entity_map
        ]
        other_opts = sorted(
            (
                {"value": eid, "label": label}
                for eid, label in entity_map.items()
                if eid not in selected_set
            ),
            key=lambda x: x["label"].lower(),
        )
        return selected_opts + other_opts

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Formulaire unifié de gestion des éléments de la télécommande et options avancées."""
        if user_input is not None:
            processed_input = dict(user_input)

            # Dépliage éventuel de la section repliable "advanced_settings" (HA 2024.4+)
            adv = processed_input.pop("advanced_settings", None)
            if isinstance(adv, Mapping):
                for k, v in adv.items():
                    processed_input.setdefault(k, v)
                processed_input.setdefault(CONF_AUTO_SYNC_WEBHOOKS, True)
                processed_input.setdefault(CONF_TRIGGER_RESYNC, True)

            # Séparation automatique entre entités HA et appareils internes de la télécommande
            if CONF_ENTITIES in processed_input and isinstance(
                processed_input[CONF_ENTITIES], (list, tuple, set)
            ):
                submitted_items = [str(x) for x in processed_input[CONF_ENTITIES] if x]
                initial_remote_ids = self._initial_remote_ids
                if initial_remote_ids is None:
                    initial_remote_ids = [
                        opt["value"]
                        for opt in self._get_remote_carousel_options(exclude_webhooks=False)
                    ]

                if initial_remote_ids or any(x.startswith("sevenhugs.") for x in submitted_items):
                    removed_remote = [
                        rid for rid in initial_remote_ids if rid not in submitted_items
                    ]
                    ha_entities = [
                        x for x in submitted_items if not x.startswith("sevenhugs.")
                    ]
                    processed_input[CONF_ENTITIES] = ha_entities
                    if removed_remote:
                        existing_del = list(processed_input.get(CONF_DELETE_DEVICES) or [])
                        processed_input[CONF_DELETE_DEVICES] = list(
                            dict.fromkeys(existing_del + removed_remote)
                        )

            return self.async_create_entry(title="", data=processed_input)

        entry = self._entry
        raw_opts = getattr(entry, "options", None) if entry is not None else None
        options = dict(raw_opts) if isinstance(raw_opts, Mapping) else {}

        raw_entities = options.get(CONF_ENTITIES, DEFAULT_ENTITIES)
        if raw_entities is None:
            ha_entities: list[str] = []
        elif isinstance(raw_entities, (list, tuple, set)):
            ha_entities = [
                str(x)
                for x in raw_entities
                if isinstance(x, str) and x.strip() and not x.strip().startswith("sevenhugs.")
            ]
        else:
            ha_entities = []

        remote_options = self._get_remote_carousel_options(exclude_webhooks=bool(ha_entities))
        remote_ids = [opt["value"] for opt in remote_options]
        self._initial_remote_ids = list(remote_ids)

        ha_options = self._get_ha_entity_options(ha_entities)
        unified_options = remote_options + ha_options
        default_selected = remote_ids + ha_entities

        dropdown_mode = (
            selector.SelectSelectorMode.DROPDOWN
            if hasattr(selector, "SelectSelectorMode")
            else "dropdown"
        )

        raw_polling = options.get(CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL)
        default_polling = (
            raw_polling
            if isinstance(raw_polling, int) and MIN_POLLING_INTERVAL <= raw_polling <= MAX_POLLING_INTERVAL
            else DEFAULT_POLLING_INTERVAL
        )
        raw_timeout = options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        default_timeout = raw_timeout if isinstance(raw_timeout, int) and raw_timeout > 0 else DEFAULT_TIMEOUT
        raw_trigger = options.get(CONF_TRIGGER_RESYNC, False)
        default_trigger = raw_trigger if isinstance(raw_trigger, bool) else False
        raw_auto_sync = options.get(CONF_AUTO_SYNC_WEBHOOKS, DEFAULT_AUTO_SYNC_WEBHOOKS)
        default_auto_sync = raw_auto_sync if isinstance(raw_auto_sync, bool) else DEFAULT_AUTO_SYNC_WEBHOOKS

        unified_selector = (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=unified_options,
                    multiple=True,
                    custom_value=True,
                    mode=dropdown_mode,
                )
            )
            if unified_options
            else selector.EntitySelector(
                selector.EntitySelectorConfig(
                    multiple=True,
                    domain=list(DEFAULT_EXPOSED_DOMAINS),
                )
            )
        )

        if ha_section is not None:
            data_schema = vol.Schema({
                vol.Optional(
                    CONF_ENTITIES,
                    default=default_selected,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=unified_options,
                        multiple=True,
                        custom_value=True,
                        mode=dropdown_mode,
                    )
                ),
                vol.Optional("advanced_settings"): ha_section(
                    vol.Schema({
                        vol.Optional(
                            CONF_POLLING_INTERVAL,
                            default=default_polling,
                        ): vol.All(
                            vol.Coerce(int),
                            vol.Range(min=MIN_POLLING_INTERVAL, max=MAX_POLLING_INTERVAL),
                        ),
                        vol.Optional(
                            CONF_TIMEOUT,
                            default=default_timeout,
                        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
                    }),
                    {"collapsed": True},
                ),
            })
        else:
            data_schema = vol.Schema({
                vol.Optional(
                    CONF_ENTITIES,
                    default=default_selected,
                ): unified_selector,
                vol.Optional(
                    CONF_DELETE_DEVICES,
                    default=[],
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=remote_options,
                        multiple=True,
                        custom_value=True,
                        mode=dropdown_mode,
                    )
                ),
                vol.Optional(
                    CONF_TRIGGER_RESYNC,
                    default=default_trigger,
                ): selector.BooleanSelector() if hasattr(selector, "BooleanSelector") else bool,
                vol.Optional(
                    CONF_POLLING_INTERVAL,
                    default=default_polling,
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_POLLING_INTERVAL, max=MAX_POLLING_INTERVAL),
                ),
                vol.Optional(
                    CONF_AUTO_SYNC_WEBHOOKS,
                    default=default_auto_sync,
                ): bool,
                vol.Optional(
                    CONF_TIMEOUT,
                    default=default_timeout,
                ): int,
            })

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
        )
