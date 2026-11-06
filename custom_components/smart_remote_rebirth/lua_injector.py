"""Module d'injection Server-Driven UI Lua pour Sevenhugs Smart Remote (SR1A).

Traduit les entités Home Assistant (light, switch, scene, media_player) en
déclencheurs de boutons HTTP pour l'assistant Lua embarqué de la télécommande
(/api/devices/sevenhugs.control.webhooks/setup).

Gère la sérialisation des synchronisations, la tolérance aux pannes réseau
et aux cycles de veille du matériel, et l'intégration avec le webhook local.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any

try:
    import aiohttp
except ImportError:
    class _AiohttpModule:
        ClientError = Exception
    aiohttp = _AiohttpModule()  # type: ignore[assignment]

try:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, State
except ImportError:
    ConfigEntry = Any  # type: ignore[misc,assignment]
    HomeAssistant = Any  # type: ignore[misc,assignment]
    State = Any  # type: ignore[misc,assignment]

try:
    from .client import (
        AsyncSmartRemoteClient,
        SevenhugsAuthError,
        SevenhugsConnectionError,
    )
    from .const import (
        CONF_ENTITIES,
        CONF_REMOTE_ID,
        CONF_SERIAL_NUMBER,
        DOMAIN,
    )
    from .webhook import async_generate_webhook_url
except (ImportError, ValueError):
    from custom_components.smart_remote_rebirth.client import (
        AsyncSmartRemoteClient,
        SevenhugsAuthError,
        SevenhugsConnectionError,
    )
    from custom_components.smart_remote_rebirth.const import (
        CONF_ENTITIES,
        CONF_REMOTE_ID,
        CONF_SERIAL_NUMBER,
        DOMAIN,
    )
    from custom_components.smart_remote_rebirth.webhook import (
        async_generate_webhook_url,
    )

_LOGGER = logging.getLogger(__name__)

# Erreurs réseau et transport tolérées lors des cycles de veille
_NETWORK_ERRORS: tuple[type[Exception], ...] = (
    SevenhugsConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
    getattr(aiohttp, "ClientError", Exception),
)

# Mappage par défaut des domaines vers l'action de service Home Assistant
DEFAULT_DOMAIN_ACTIONS: dict[str, str] = {
    "light": "toggle",
    "switch": "toggle",
    "scene": "turn_on",
    "script": "turn_on",
    "button": "press",
    "cover": "toggle",
    "fan": "toggle",
    "media_player": "toggle",
    "input_boolean": "toggle",
    "automation": "trigger",
}

__all__ = [
    "DEFAULT_DOMAIN_ACTIONS",
    "SevenhugsLuaInjector",
]


class _AwaitableList(list):
    """Liste compatible synchrone et asynchrone (supporte à la fois appel direct et await)."""

    def __await__(self):
        async def _resolve():
            return self

        return _resolve().__await__()


class SevenhugsLuaInjector:
    """Gestionnaire de traduction et d'injection des boutons UI vers la télécommande."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry | Any,
        client: AsyncSmartRemoteClient | Any | None = None,
    ) -> None:
        """Initialise l'injecteur Lua.

        Prend en charge l'ordre standard (hass, entry, client) ainsi que l'ordre
        alternatif (hass, client, entry) pour une flexibilité maximale.
        """
        if hasattr(entry, "async_push_webhooks") and hasattr(client, "data"):
            actual_client = entry
            actual_entry = client
        else:
            actual_entry = entry
            actual_client = client

        self.hass = hass
        self.entry = actual_entry
        self.client = actual_client
        self._lock = asyncio.Lock()
        self._sync_lock = self._lock
        self._pending_resync: bool = False

    @property
    def _entry_title(self) -> str:
        """Retourne le titre ou l'identifiant lisible de l'entrée."""
        return (
            getattr(self.entry, "title", None)
            or (
                self.entry.data.get(CONF_SERIAL_NUMBER)
                if hasattr(self.entry, "data") and isinstance(self.entry.data, Mapping)
                else None
            )
            or getattr(self.entry, "entry_id", "smart_remote_rebirth")
        )

    @property
    def remote_id(self) -> str:
        """Résout l'identifiant matériel de la télécommande pour le payload."""
        # 1. Vérification dans client.keys
        if hasattr(self.client, "keys") and isinstance(self.client.keys, Mapping):
            val = self.client.keys.get(CONF_REMOTE_ID)
            if isinstance(val, str) and val:
                return val
        # 2. Vérification dans client._keys
        if hasattr(self.client, "_keys") and isinstance(self.client._keys, Mapping):
            val = self.client._keys.get(CONF_REMOTE_ID)
            if isinstance(val, str) and val:
                return val
        # 3. Attribut direct sur le client (chaîne uniquement)
        for attr in ("_remote_id", "remote_id"):
            val = getattr(self.client, attr, None)
            if isinstance(val, str) and val:
                return val
        # 4. Données de ConfigEntry
        if hasattr(self.entry, "data") and isinstance(self.entry.data, Mapping):
            val = self.entry.data.get(CONF_REMOTE_ID) or self.entry.data.get(CONF_SERIAL_NUMBER)
            if isinstance(val, str) and val:
                return str(val)
        return ""

    @property
    def webhook_url(self) -> str:
        """Résout l'URL complète du webhook local Home Assistant."""
        return async_generate_webhook_url(self.hass, self.entry)

    def get_webhook_url(self) -> str:
        """Alias pour webhook_url."""
        return self.webhook_url

    def convert_entity_to_trigger(
        self,
        entity_id: str,
        webhook_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Convertit un identifiant d'entité en spécification de déclencheur HTTP."""
        if not isinstance(entity_id, str):
            _LOGGER.warning("Entité ignorée : attendu str, reçu %r", entity_id)
            return None

        entity_id = entity_id.strip()
        if not entity_id or "." not in entity_id:
            _LOGGER.warning("Identifiant d'entité malformé ignoré : %r", entity_id)
            return None

        domain, _, object_id = entity_id.partition(".")
        domain = domain.strip().lower()
        object_id = object_id.strip()

        if not domain or not object_id or object_id.startswith(".") or object_id.endswith("."):
            _LOGGER.warning("Entité malformée ignorée (domaine ou objet vide) : %r", entity_id)
            return None

        default_action = DEFAULT_DOMAIN_ACTIONS.get(domain)
        if default_action is None:
            _LOGGER.warning(
                "Domaine '%s' non supporté pour l'entité '%s'. Domaines supportés: %s",
                domain,
                entity_id,
                list(DEFAULT_DOMAIN_ACTIONS.keys()),
            )
            return None

        target_url = webhook_url or self.webhook_url
        normalized_entity_id = f"{domain}.{object_id}"

        # Récupération du friendly_name depuis hass.states avec fallback vers entity_id
        friendly_name = normalized_entity_id
        if hasattr(self.hass, "states") and hasattr(self.hass.states, "get"):
            try:
                state = self.hass.states.get(entity_id) or self.hass.states.get(normalized_entity_id)
                if state is not None and hasattr(state, "attributes") and isinstance(state.attributes, Mapping):
                    raw_fn = state.attributes.get("friendly_name")
                    if isinstance(raw_fn, str) and raw_fn.strip():
                        friendly_name = raw_fn.strip()
            except Exception as err:
                _LOGGER.debug(
                    "Erreur lors de la récupération de l'état pour %s: %s",
                    entity_id,
                    err,
                )

        payload_dict = {
            "event": "button_press",
            "domain": domain,
            "action": default_action,
            "entity_id": normalized_entity_id,
            "source_remote": self.remote_id,
        }

        return {
            "name": friendly_name,
            "method": "POST",
            "url": target_url,
            "body": json.dumps(payload_dict),
        }

    def async_build_triggers_from_entities(
        self,
        entity_ids: list[str] | Iterable[str] | None,
    ) -> list[dict[str, Any]]:
        """Construit la liste des déclencheurs de boutons à partir des entités fournies.

        Filtre les entités invalides ou doublons, extrait le friendly name et assemble
        le dictionnaire attendu par AsyncSmartRemoteClient.async_push_webhooks.
        Compatible à la fois en appel synchrone direct et via 'await'.
        """
        if not entity_ids:
            return _AwaitableList()

        if not isinstance(entity_ids, (list, tuple, set)) or isinstance(entity_ids, (str, bytes, Mapping)):
            _LOGGER.warning("Type de collection d'entités non supporté : %r", type(entity_ids))
            return _AwaitableList()

        webhook_url = self.webhook_url
        triggers: list[dict[str, Any]] = []
        seen_entities: set[str] = set()

        for raw_entity in entity_ids:
            if not isinstance(raw_entity, str):
                continue
            entity_id = raw_entity.strip()
            if not entity_id or "." not in entity_id:
                continue

            normalized_id = entity_id.lower()
            if normalized_id in seen_entities:
                continue

            trigger = self.convert_entity_to_trigger(entity_id, webhook_url=webhook_url)
            if trigger is not None:
                seen_entities.add(normalized_id)
                triggers.append(trigger)

        return _AwaitableList(triggers)

    def _get_existing_webhook_device_ids(self) -> list[str]:
        """Retourne les identifiants d'appareils sevenhugs.control.webhooks déjà présents dans le carrousel."""
        try:
            domain_data = getattr(self.hass, "data", {}).get(DOMAIN, {}) if hasattr(self.hass, "data") else {}
            runtime_data = domain_data.get(self.entry.entry_id) if isinstance(domain_data, Mapping) else None
            coordinator = getattr(runtime_data, "coordinator", None) if runtime_data else None
            if coordinator is None and isinstance(runtime_data, Mapping):
                coordinator = runtime_data.get("coordinator")
            coord_data = getattr(coordinator, "data", None) if coordinator else None
            if not isinstance(coord_data, Mapping):
                return []
            raw_settings = coord_data.get("raw_settings", {})
            if not isinstance(raw_settings, Mapping):
                return []
            carousel = raw_settings.get("carousel_order", [])
            if not isinstance(carousel, list):
                return []
            return [
                str(item["id"])
                for item in carousel
                if isinstance(item, Mapping) and str(item.get("id", "")).startswith("sevenhugs.control.webhooks:")
            ]
        except Exception:
            return []

    async def async_sync_entities(
        self,
        entity_ids: list[str] | None = None,
    ) -> bool:
        """Synchronise la liste des entités exposées vers la télécommande Sevenhugs.

        Résout les entités (paramètre explicite, options ou données d'entrée),
        gère gracieusement les listes vides (0 boutons), sérialise les requêtes
        concurrentes via un verrou asynchrone, absorbe les erreurs de veille
        sans lever d'exception et notifie le coordinateur lors d'un succès.

        Retourne :
            bool : True si la synchronisation a réussi ou si 0 déclencheur requis,
                   False en cas d'erreur de communication, de veille ou d'authentification.
        """
        async with self._lock:
            # 1. Résolution de la liste des entités cibles
            if entity_ids is None:
                options = getattr(self.entry, "options", None)
                data = getattr(self.entry, "data", None)
                if isinstance(options, Mapping) and CONF_ENTITIES in options:
                    entity_ids = options.get(CONF_ENTITIES, [])
                elif isinstance(data, Mapping) and CONF_ENTITIES in data:
                    entity_ids = data.get(CONF_ENTITIES, [])
                else:
                    entity_ids = []

            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]
            elif not isinstance(entity_ids, (list, tuple, set)):
                entity_ids = []

            # 2. Construction des déclencheurs
            build_res = self.async_build_triggers_from_entities(entity_ids)
            triggers = await build_res if inspect.isawaitable(build_res) else build_res

            existing_wh_ids = self._get_existing_webhook_device_ids()

            # 3. Gestion gracieuse des déclencheurs vides (0 boutons)
            if not triggers:
                if existing_wh_ids and hasattr(self.client, "async_delete_remote_items"):
                    try:
                        await self.client.async_delete_remote_items(existing_wh_ids)
                        self._notify_coordinator_wake_up("lua_sync_clear")
                    except Exception as del_err:
                        _LOGGER.debug("Nettoyage des anciens webhooks ignoré : %s", del_err)
                _LOGGER.info(
                    "Aucun déclencheur à synchroniser pour %s : liste d'entités vide ou non valide",
                    self._entry_title,
                )
                self._pending_resync = False
                return True

            # 4. Envoi effectif vers l'assistant Lua avec tolérance de veille et d'erreurs
            _LOGGER.info(
                "Envoi de %d déclencheurs vers l'assistant Lua Sevenhugs (%s)",
                len(triggers),
                self._entry_title,
            )

            try:
                if existing_wh_ids and hasattr(self.client, "async_delete_remote_items"):
                    try:
                        await self.client.async_delete_remote_items(existing_wh_ids)
                    except Exception as del_err:
                        _LOGGER.debug("Remplacement préalable des anciens webhooks ignoré : %s", del_err)

                success = await self.client.async_push_webhooks(triggers)
                if success:
                    _LOGGER.info(
                        "Synchronisation Lua réussie de %d déclencheurs vers Sevenhugs (%s)",
                        len(triggers),
                        self._entry_title,
                    )
                    self._pending_resync = False
                    self._notify_coordinator_wake_up("lua_sync")
                    return True

                _LOGGER.warning(
                    "L'assistant Lua Sevenhugs (%s) a retourné un échec lors de la synchronisation",
                    self._entry_title,
                )
                self._pending_resync = True
                return False

            except SevenhugsAuthError as err:
                _LOGGER.warning(
                    "Échec d'authentification lors de la synchronisation Lua vers Sevenhugs (%s) : %s",
                    self._entry_title,
                    err,
                )
                self._pending_resync = True
                return False

            except _NETWORK_ERRORS as err:
                _LOGGER.warning(
                    "Télécommande Sevenhugs inaccessible ou en veille lors de la synchronisation Lua (%s) : %s",
                    self._entry_title,
                    err,
                )
                self._pending_resync = True
                return False

            except asyncio.CancelledError:
                _LOGGER.debug("Synchronisation Lua annulée pour %s", self._entry_title)
                raise

            except Exception as err:
                _LOGGER.error(
                    "Erreur inattendue lors de la synchronisation des entités vers Sevenhugs (%s) : %s",
                    self._entry_title,
                    err,
                    exc_info=True,
                )
                self._pending_resync = True
                return False

    def _notify_coordinator_wake_up(self, reason: str = "lua_sync") -> None:
        """Notifie le coordinateur d'un réveil confirmé suite à une synchronisation réussie."""
        try:
            runtime_data = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
            coordinator = getattr(runtime_data, "coordinator", None) if runtime_data else None
            if coordinator is None and isinstance(runtime_data, dict):
                coordinator = runtime_data.get("coordinator")
            if coordinator is not None and hasattr(coordinator, "async_handle_wake_up"):
                self.hass.async_create_task(coordinator.async_handle_wake_up(reason))
        except Exception as err:
            _LOGGER.debug("Notification coordinateur ignorée : %s", err)
