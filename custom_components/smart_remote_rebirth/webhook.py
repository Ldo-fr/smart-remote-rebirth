"""Module de gestion des webhooks locaux pour Sevenhugs Smart Remote.

Fournit l'enregistrement du webhook local sécurisé, la validation stricte du payload
entrant (domaines, actions et entités autorisés), l'exécution des services Home Assistant,
l'émission d'événements sur le bus système et le réveil optimiste du coordinateur.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import inspect
import logging
from typing import Any

from aiohttp import web
import voluptuous as vol

try:
    from homeassistant.components import webhook
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
except ImportError:
    webhook = Any  # type: ignore[misc,assignment]
    ConfigEntry = Any  # type: ignore[misc,assignment]
    HomeAssistant = Any  # type: ignore[misc,assignment]

from .const import (
    CONF_ENTITIES,
    CONF_REMOTE_ID,
    CONF_SERIAL_NUMBER,
    DEFAULT_EXPOSED_DOMAINS,
    DOMAIN,
    EVENT_BUTTON_PRESSED,
    EVENT_SEVENHUGS_BUTTON_PRESSED,
    WEBHOOK_ID_PREFIX,
)

_LOGGER = logging.getLogger(__name__)

# Liste blanche stricte des domaines et services autorisés via le webhook
ALLOWED_DOMAIN_ACTIONS: dict[str, set[str]] = {
    "light": {"toggle", "turn_on", "turn_off"},
    "switch": {"toggle", "turn_on", "turn_off"},
    "scene": {"turn_on", "apply"},
    "script": {"turn_on", "turn_off", "toggle"},
    "button": {"press"},
    "cover": {
        "toggle",
        "open_cover",
        "close_cover",
        "stop_cover",
        "set_cover_position",
        "set_cover_tilt_position",
        "open_cover_tilt",
        "close_cover_tilt",
        "stop_cover_tilt",
        "toggle_cover_tilt",
    },
    "fan": {
        "toggle",
        "turn_on",
        "turn_off",
        "set_percentage",
        "set_preset_mode",
        "oscillate",
        "increase_speed",
        "decrease_speed",
        "set_direction",
    },
    "media_player": {
        "toggle",
        "turn_on",
        "turn_off",
        "media_play_pause",
        "media_play",
        "media_pause",
        "media_stop",
        "media_next_track",
        "media_previous_track",
        "volume_up",
        "volume_down",
        "volume_set",
        "volume_mute",
        "select_source",
        "select_sound_mode",
        "play_media",
    },
    "input_boolean": {"toggle", "turn_on", "turn_off"},
    "automation": {"trigger", "toggle", "turn_on", "turn_off"},
}

ALLOWED_WEBHOOK_DOMAINS: set[str] = set(DEFAULT_EXPOSED_DOMAINS) & set(ALLOWED_DOMAIN_ACTIONS.keys())

# Clés de ciblage interdites dans les sous-dictionnaires de paramètres (anti-parameter pollution)
FORBIDDEN_TARGET_OVERRIDE_KEYS: set[str] = {
    "entity_id",
    "device_id",
    "area_id",
    "floor_id",
    "label_id",
    "target",
}

__all__ = [
    "ALLOWED_DOMAIN_ACTIONS",
    "ALLOWED_WEBHOOK_DOMAINS",
    "async_generate_webhook_url",
    "async_get_webhook_id",
    "async_handle_webhook",
    "async_register_webhook",
    "async_unregister_webhook",
    "validate_dict",
    "validate_entity_id",
    "validate_string",
    "validate_webhook_payload",
]


# ============================================================================
# 1. VALIDATEURS VOLUPTUOUS POUR PAYLOAD WEBHOOK
# ============================================================================

def validate_string(value: object) -> str:
    """Valide qu'une valeur est une chaîne non vide."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise vol.Invalid(f"Valeur attendue de type chaîne non vide, reçu {value!r}")


def validate_entity_id(value: object) -> str | list[str]:
    """Valide qu'un entity_id est une chaîne ou une liste de chaînes non vides."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list) and value and all(isinstance(x, str) and x.strip() for x in value):
        return [x.strip() for x in value]
    raise vol.Invalid(
        f"entity_id doit être une chaîne ou une liste de chaînes non vides, reçu {value!r}"
    )


def validate_dict(value: object) -> dict[str, Any]:
    """Valide qu'une valeur est un dictionnaire."""
    if isinstance(value, dict):
        return value
    raise vol.Invalid(f"Dictionnaire attendu, reçu {type(value).__name__}")


WEBHOOK_PAYLOAD_SCHEMA = vol.Schema(
    {
        vol.Required("domain"): validate_string,
        vol.Optional("action"): validate_string,
        vol.Optional("service"): validate_string,
        vol.Optional("entity_id"): validate_entity_id,
        vol.Optional("params"): validate_dict,
        vol.Optional("parameters"): validate_dict,
        vol.Optional("data"): validate_dict,
        vol.Optional("service_data"): validate_dict,
        vol.Optional("event"): validate_string,
        vol.Optional("source_remote"): validate_string,
    },
    extra=getattr(vol, "ALLOW_EXTRA", 1),
)


def validate_webhook_payload(payload: Any) -> dict[str, Any]:
    """Valide le format et les contraintes structurelles du payload webhook entrant."""
    if not isinstance(payload, dict):
        raise vol.Invalid(
            f"Le payload doit être un objet JSON / dictionnaire, reçu {type(payload).__name__}"
        )

    validated = WEBHOOK_PAYLOAD_SCHEMA(payload)
    action = validated.get("action") or validated.get("service")
    if not action:
        raise vol.Invalid("Missing or invalid required field: 'action' or 'service'")

    # Préserve les clés supplémentaires non déclarées dans le schéma
    result = dict(payload)
    result.update(validated)
    return result


# ============================================================================
# 2. IDENTIFIANTS ET URLS
# ============================================================================

def async_get_webhook_id(entry_or_id: ConfigEntry | str) -> str:
    """Retourne l'identifiant normalisé du webhook pour une ConfigEntry ou un identifiant."""
    if isinstance(entry_or_id, str):
        if entry_or_id.startswith(WEBHOOK_ID_PREFIX):
            return entry_or_id
        return f"{WEBHOOK_ID_PREFIX}{entry_or_id}"
    return f"{WEBHOOK_ID_PREFIX}{entry_or_id.entry_id}"


def async_generate_webhook_url(hass: HomeAssistant, entry_or_id: ConfigEntry | str) -> str:
    """Génère l'URL complète d'accès local au webhook pour la télécommande."""
    webhook_id = async_get_webhook_id(entry_or_id)
    try:
        return webhook.async_generate_url(
            hass,
            webhook_id,
            allow_internal=True,
            allow_external=True,
            allow_ip=True,
            prefer_external=False,
        )
    except TypeError:
        return webhook.async_generate_url(hass, webhook_id)


# ============================================================================
# 3. ENREGISTREMENT ET DÉSENREGISTREMENT DU WEBHOOK
# ============================================================================

def async_register_webhook(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Enregistre le webhook local sécurisé pour une ConfigEntry.

    Paramètres enregistrés :
    - local_only=True : Restreint l'exécution au réseau local (RFC 1918 / LAN).
    - allowed_methods=["POST", "PUT"] : Restreint les méthodes HTTP acceptées.
    """
    webhook_id = async_get_webhook_id(entry)

    # Désenregistrement préventif idempotent pour éviter toute collision lors d'un rechargement
    try:
        webhook.async_unregister(hass, webhook_id)
    except Exception:
        pass

    title = getattr(entry, "title", None) or (
        entry.data.get(CONF_SERIAL_NUMBER, entry.entry_id)
        if hasattr(entry, "data") and isinstance(entry.data, Mapping)
        else entry.entry_id
    )
    webhook_name = f"Sevenhugs Smart Remote ({title})"
    webhook.async_register(
        hass=hass,
        domain=DOMAIN,
        name=webhook_name,
        webhook_id=webhook_id,
        handler=async_handle_webhook,
        allowed_methods=["POST", "PUT"],
        local_only=True,
    )

    # Mémorisation optionnelle dans runtime_data si déjà initialisé
    runtime_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime_data is not None:
        if isinstance(runtime_data, dict):
            runtime_data["webhook_id"] = webhook_id
        if hasattr(runtime_data, "webhook_id"):
            runtime_data.webhook_id = webhook_id

    _LOGGER.info(
        "Webhook local Sevenhugs enregistré avec succès (id=%s, local_only=True)",
        webhook_id,
    )
    return webhook_id


def async_unregister_webhook(hass: HomeAssistant, entry_or_id: ConfigEntry | str) -> None:
    """Désenregistre proprement le webhook d'une ConfigEntry avec tolérance d'erreur."""
    webhook_id = async_get_webhook_id(entry_or_id)
    try:
        webhook.async_unregister(hass, webhook_id)
        _LOGGER.info("Webhook local Sevenhugs désenregistré avec succès (id=%s)", webhook_id)
    except Exception as err:
        _LOGGER.debug(
            "Erreur non critique lors du désenregistrement du webhook %s: %s",
            webhook_id,
            err,
        )


def _extract_configured_entities(entry: Any) -> set[str] | None:
    """Extrait l'ensemble normalisé des entity_id autorisés pour cette ConfigEntry.

    Retourne un set non-vide si des entités ont été explicitement configurées par
    l'utilisateur, ou None si aucune restriction d'entités n'est encore enregistrée.
    """
    if entry is None:
        return None

    for source_attr in ("options", "data"):
        source = getattr(entry, source_attr, None)
        if isinstance(source, Mapping) and CONF_ENTITIES in source:
            raw_list = source.get(CONF_ENTITIES)
            if isinstance(raw_list, (list, tuple, set)):
                normalized = {
                    str(item).strip().lower()
                    for item in raw_list
                    if isinstance(item, str) and item.strip() and not item.strip().startswith("sevenhugs.")
                }
                if normalized:
                    return normalized
    return None


# ============================================================================
# 4. TRAITEMENT DE LA REQUÊTE HTTP ENTRANTE (HANDLER)
# ============================================================================

async def async_handle_webhook(
    hass: HomeAssistant,
    webhook_id: str,
    request: Any,
) -> web.Response:
    """Gère une requête HTTP envoyée par la télécommande vers le webhook local.

    Étapes :
    1. Décodage du corps JSON (HTTP 400 si JSON invalide).
    2. Validation du schéma avec Voluptuous (HTTP 400 si validation échouée).
    3. Vérification de sécurité : domaine autorisé, service autorisé, cohérence domaine/entité
       et appartenance à la liste d'entités exposées par la ConfigEntry (HTTP 403 si refusé).
    4. Émission de l'événement système 'smart_remote_button_pressed' sur hass.bus.
    5. Réveil optimiste immédiat du coordinateur en tâche de fond.
    6. Appel du service Home Assistant correspondant (hass.services.async_call).
    7. Retour HTTP 200 OK.
    """
    # 1. Décodage du JSON entrant
    try:
        callable_json = getattr(request, "json", None)
        if callable(callable_json):
            json_res = callable_json()
            if inspect.isawaitable(json_res):
                raw_payload = await json_res
            else:
                raw_payload = json_res
        elif hasattr(request, "_json_data"):
            raw_payload = request._json_data
        else:
            raw_payload = {}
    except Exception as err:
        _LOGGER.warning("Échec du décodage JSON pour le webhook %s: %s", webhook_id, err)
        return web.Response(
            status=400,
            text=f"Invalid JSON payload: {err}",
            content_type="text/plain",
        )

    # 2. Validation du schéma
    try:
        payload = validate_webhook_payload(raw_payload)
    except vol.Invalid as err:
        _LOGGER.warning("Validation du schéma échouée pour le webhook %s: %s", webhook_id, err)
        return web.Response(
            status=400,
            text=f"Invalid payload schema: {err}",
            content_type="text/plain",
        )

    domain = payload["domain"].lower()
    raw_action = (payload.get("action") or payload.get("service") or "").strip().lower()
    action = raw_action
    if action.startswith(f"{domain}."):
        action = action[len(domain) + 1 :]

    # 3. Contrôles de sécurité (Allowlist Domaine, Service & Entités)
    if domain not in ALLOWED_WEBHOOK_DOMAINS:
        _LOGGER.warning(
            "Rejet de sécurité sur le webhook %s : domaine '%s' non autorisé",
            webhook_id,
            domain,
        )
        return web.Response(
            status=403,
            text=f"Forbidden domain: '{domain}' is not allowed",
            content_type="text/plain",
        )

    allowed_actions = ALLOWED_DOMAIN_ACTIONS.get(domain, set())
    if action not in allowed_actions:
        _LOGGER.warning(
            "Rejet de sécurité sur le webhook %s : action '%s.%s' non autorisée",
            webhook_id,
            domain,
            action,
        )
        return web.Response(
            status=403,
            text=f"Forbidden service: '{domain}.{action}' is not allowed",
            content_type="text/plain",
        )

    entity_id = payload.get("entity_id")

    # 4. Résolution de l'entry_id, de la ConfigEntry et du coordinateur
    entry_id = webhook_id.removeprefix(WEBHOOK_ID_PREFIX)

    remote_id = ""
    serial_number = ""
    coordinator = None
    entry = None

    runtime_data = hass.data.get(DOMAIN, {}).get(entry_id)
    if runtime_data is not None:
        coordinator = getattr(runtime_data, "coordinator", None) or (
            runtime_data.get("coordinator") if isinstance(runtime_data, dict) else None
        )
        if coordinator is not None:
            for attr_name in ("entry", "config_entry"):
                cand = getattr(coordinator, attr_name, None)
                if cand is not None and (
                    isinstance(getattr(cand, "options", None), Mapping)
                    or isinstance(getattr(cand, "data", None), Mapping)
                ):
                    entry = cand
                    break
        client = getattr(runtime_data, "client", None) or (
            runtime_data.get("client") if isinstance(runtime_data, dict) else None
        )
        if client and hasattr(client, "keys") and isinstance(client.keys, dict):
            remote_id = client.keys.get(CONF_REMOTE_ID, "")
            serial_number = client.keys.get(CONF_SERIAL_NUMBER, "")

    if hasattr(hass, "config_entries") and hasattr(hass.config_entries, "async_get_entry"):
        resolved_entry = hass.config_entries.async_get_entry(entry_id)
        if resolved_entry is not None:
            entry = resolved_entry
            if hasattr(entry, "data") and isinstance(entry.data, Mapping):
                remote_id = remote_id or entry.data.get(CONF_REMOTE_ID, "")
                serial_number = serial_number or entry.data.get(CONF_SERIAL_NUMBER, "")

    # Validation de cohérence domaine/entity_id et contrôle de la liste d'entités configurées
    configured_entities = _extract_configured_entities(entry)
    if entity_id is not None:
        target_list = [entity_id] if isinstance(entity_id, str) else list(entity_id)
        for target_ent in target_list:
            norm_ent = target_ent.strip().lower()
            if not norm_ent.startswith(f"{domain}."):
                _LOGGER.warning(
                    "Rejet de sécurité sur le webhook %s : l'entité '%s' ne correspond pas au domaine '%s'",
                    webhook_id,
                    target_ent,
                    domain,
                )
                return web.Response(
                    status=403,
                    text=f"Entity '{target_ent}' does not match domain '{domain}'",
                    content_type="text/plain",
                )
            if configured_entities is not None and norm_ent not in configured_entities:
                _LOGGER.warning(
                    "Rejet de sécurité sur le webhook %s : l'entité '%s' ne fait pas partie des entités exposées",
                    webhook_id,
                    target_ent,
                )
                return web.Response(
                    status=403,
                    text=f"Forbidden entity: '{target_ent}' is not exposed for this remote",
                    content_type="text/plain",
                )
    elif configured_entities is not None:
        # Si l'utilisateur a restreint la télécommande à une liste d'entités, un appel sans entity_id est refusé
        _LOGGER.warning(
            "Rejet de sécurité sur le webhook %s : entity_id manquant alors qu'une liste d'entités est configurée",
            webhook_id,
        )
        return web.Response(
            status=403,
            text="Forbidden: 'entity_id' is required when exposed entities are configured",
            content_type="text/plain",
        )

    # 5. Assemblage sécurisé des paramètres de service (sans écrasement de cible)
    service_data: dict[str, Any] = {}
    for param_key in ("params", "parameters", "service_data", "data"):
        param_val = payload.get(param_key)
        if isinstance(param_val, dict):
            for k, v in param_val.items():
                if k not in FORBIDDEN_TARGET_OVERRIDE_KEYS:
                    service_data[k] = v

    # Extraction des clés supplémentaires de premier niveau (non réservées et non interdites)
    reserved_keys = {
        "domain",
        "action",
        "service",
        "entity_id",
        "event",
        "source_remote",
        "params",
        "parameters",
        "service_data",
        "data",
    } | FORBIDDEN_TARGET_OVERRIDE_KEYS
    for k, v in raw_payload.items():
        if k not in reserved_keys and k not in service_data:
            service_data[k] = v

    if entity_id is not None:
        service_data["entity_id"] = entity_id

    # 6. Émission d'événement sur le bus Home Assistant (déclencheur universel)
    event_data = dict(raw_payload)
    event_data.setdefault("entry_id", entry_id)
    if remote_id:
        event_data.setdefault("remote_id", str(remote_id))
    else:
        event_data.setdefault("remote_id", "")

    if serial_number:
        event_data.setdefault("serial_number", str(serial_number))
    else:
        event_data.setdefault("serial_number", "")

    event_data.setdefault("webhook_id", webhook_id)
    event_data["domain"] = domain
    event_data["action"] = action
    if entity_id is not None:
        event_data["entity_id"] = entity_id
    event_data.setdefault("payload", dict(raw_payload))

    if hasattr(hass, "bus") and hasattr(hass.bus, "async_fire"):
        try:
            hass.bus.async_fire(EVENT_BUTTON_PRESSED, event_data)
            _LOGGER.debug(
                "Événement %s émis pour le webhook %s: %s",
                EVENT_BUTTON_PRESSED,
                webhook_id,
                event_data,
            )
        except Exception as err:
            _LOGGER.warning("Échec de l'émission d'événement sur le bus: %s", err)

    # 7. Réveil optimiste du coordinateur de données (sans bloquer la réponse HTTP)
    if coordinator is not None and hasattr(coordinator, "async_handle_wake_up"):
        try:
            wake_coro = coordinator.async_handle_wake_up("webhook")
            if hasattr(hass, "async_create_task"):
                hass.async_create_task(wake_coro)
            else:
                asyncio.create_task(wake_coro)
        except Exception as err:
            _LOGGER.debug("Notification de réveil au coordinateur ignorée: %s", err)

    # 8. Exécution asynchrone du service ciblé
    if hasattr(hass, "services") and hasattr(hass.services, "async_call"):
        try:
            await hass.services.async_call(
                domain=domain,
                service=action,
                service_data=service_data,
                blocking=False,
            )
            _LOGGER.info(
                "Service %s.%s appelé avec succès par le webhook %s (%s)",
                domain,
                action,
                webhook_id,
                service_data,
            )
        except Exception as err:
            _LOGGER.error(
                "Erreur lors de l'appel du service %s.%s pour le webhook %s: %s",
                domain,
                action,
                webhook_id,
                err,
            )

    return web.Response(status=200, text="OK", content_type="text/plain")
