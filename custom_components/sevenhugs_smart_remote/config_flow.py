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

                # Inscription dans le Device Registry de Home Assistant
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
                    name=f"Sevenhugs Smart Remote ({resolved_id})",
                    sw_version=fw_version or "0.76.4",
                    hw_version=HARDWARE_VERSION,
                )

                if self._reauth_entry:
                    return self.async_update_reload_and_abort(
                        self._reauth_entry,
                        data_updates=entry_data,
                        reason="reauth_successful",
                    )

                return self.async_create_entry(
                    title=f"Sevenhugs Smart Remote ({resolved_id})",
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
            except SevenhugsAuthError:
                errors["base"] = ERROR_INVALID_PIN
            except SevenhugsConnectionError:
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
    """Gestionnaire d'options avancées pour Sevenhugs Smart Remote."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialise le gestionnaire d'options."""
        super().__init__(config_entry)
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Formulaire de gestion des options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options or {}

        default_entities = options.get(CONF_ENTITIES, DEFAULT_ENTITIES)
        if default_entities is None:
            default_entities = []
        elif isinstance(default_entities, (list, tuple, set)):
            default_entities = list(default_entities)
        else:
            default_entities = []

        data_schema = vol.Schema({
            vol.Optional(
                CONF_ENTITIES,
                default=default_entities,
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    multiple=True,
                    domain=DEFAULT_EXPOSED_DOMAINS,
                )
            ),
            vol.Optional(
                CONF_TRIGGER_RESYNC,
                default=False,
            ): selector.BooleanSelector() if hasattr(selector, "BooleanSelector") else bool,
            vol.Optional(
                CONF_POLLING_INTERVAL,
                default=options.get(CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_POLLING_INTERVAL, max=MAX_POLLING_INTERVAL)),
            vol.Optional(
                CONF_AUTO_SYNC_WEBHOOKS,
                default=options.get(CONF_AUTO_SYNC_WEBHOOKS, DEFAULT_AUTO_SYNC_WEBHOOKS),
            ): bool,
            vol.Optional(
                CONF_TIMEOUT,
                default=options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
            ): int,
        })

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
        )
