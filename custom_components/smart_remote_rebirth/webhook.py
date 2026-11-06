"""Module de gestion des webhooks locaux pour Sevenhugs Smart Remote.

Fournit l'enregistrement du webhook local sécurisé, la validation du payload
entrant, l'exécution directe des services Home Assistant, l'émission d'événements
sur le bus système et le réveil optimiste du coordinateur de données.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import inspect
import logging
from typing import Any

import voluptuous as vol
from aiohttp import web

try:
    from homeassistant.components import webhook
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
except ImportError:
    webhook = Any  # type: ignore[misc,assignment]
    ConfigEntry = Any  # type: ignore[misc,assignment]
    HomeAssistant = Any  # type: ignore[misc,assignment]

from .const import (
    CONF_REMOTE_ID,
    CONF_SERIAL_NUMBER,
    DOMAIN,
    EVENT_BUTTON_PRESSED,
    EVENT_SEVENHUGS_BUTTON_PRESSED,
    WEBHOOK_ID_PREFIX,
)

_LOGGER = logging.getLogger(__name__)

__all__ = [
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
    3. Résolution des métadonnées d'entrée et du coordinateur.
    4. Émission de l'événement système 'sevenhugs_button_pressed' sur hass.bus.
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

    domain = payload["domain"]
    raw_action = payload.get("action") or payload.get("service")
    action = raw_action
    if action.startswith(f"{domain}."):
        action = action[len(domain) + 1 :]

    entity_id = payload.get("entity_id")

    # 3. Assemblage des paramètres de service
    service_data: dict[str, Any] = {}
    if entity_id is not None:
        service_data["entity_id"] = entity_id

    for param_key in ("params", "parameters", "service_data", "data"):
        param_val = payload.get(param_key)
        if isinstance(param_val, dict):
            service_data.update(param_val)

    # Extraction des clés supplémentaires de premier niveau (non réservées)
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
    }
    for k, v in raw_payload.items():
        if k not in reserved_keys and k not in service_data:
            service_data[k] = v

    # 4. Résolution de l'entry_id, du remote_id et du coordinateur
    entry_id = (
        webhook_id.removeprefix(WEBHOOK_ID_PREFIX)
    )

    remote_id = ""
    serial_number = ""
    coordinator = None

    runtime_data = hass.data.get(DOMAIN, {}).get(entry_id)
    if runtime_data is not None:
        coordinator = getattr(runtime_data, "coordinator", None) or (
            runtime_data.get("coordinator") if isinstance(runtime_data, dict) else None
        )
        client = getattr(runtime_data, "client", None) or (
            runtime_data.get("client") if isinstance(runtime_data, dict) else None
        )
        if client and hasattr(client, "keys") and isinstance(client.keys, dict):
            remote_id = client.keys.get(CONF_REMOTE_ID, "")
            serial_number = client.keys.get(CONF_SERIAL_NUMBER, "")

    if hasattr(hass, "config_entries") and hasattr(hass.config_entries, "async_get_entry"):
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and hasattr(entry, "data") and isinstance(entry.data, Mapping):
            remote_id = remote_id or entry.data.get(CONF_REMOTE_ID, "")
            serial_number = serial_number or entry.data.get(CONF_SERIAL_NUMBER, "")

    # 5. Émission d'événement sur le bus Home Assistant (déclencheur universel)
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
    event_data.setdefault("domain", domain)
    event_data.setdefault("action", action)
    if entity_id is not None:
        event_data.setdefault("entity_id", entity_id)
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

    # 6. Réveil optimiste du coordinateur de données (sans bloquer la réponse HTTP)
    if coordinator is not None and hasattr(coordinator, "async_handle_wake_up"):
        try:
            wake_coro = coordinator.async_handle_wake_up("webhook")
            if hasattr(hass, "async_create_task"):
                hass.async_create_task(wake_coro)
            else:
                asyncio.create_task(wake_coro)
        except Exception as err:
            _LOGGER.debug("Notification de réveil au coordinateur ignorée: %s", err)

    # 7. Exécution asynchrone du service ciblé
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
