"""Composant Home Assistant Sevenhugs Smart Remote (SR1A).

Phase 4 : Initialisation, cycle de vie, coordinateur de données,
gestion des webhooks locaux, injecteur Server-Driven UI Lua et
forwarding multi-plateformes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

import aiohttp

# Assure la disponibilité des dépendances vendorisées dans .pylib si présent
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYLIB_DIR = os.path.join(_BASE_DIR, ".pylib")
if os.path.isdir(_PYLIB_DIR) and _PYLIB_DIR not in sys.path:
    sys.path.insert(0, _PYLIB_DIR)

try:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
    from homeassistant.helpers.aiohttp_client import async_create_clientsession
except ImportError:
    ConfigEntry = Any  # type: ignore[misc,assignment]
    HomeAssistant = Any  # type: ignore[misc,assignment]
    ConfigEntryAuthFailed = Exception  # type: ignore[misc,assignment]
    ConfigEntryNotReady = Exception  # type: ignore[misc,assignment]

    def async_create_clientsession(*args: Any, **kwargs: Any) -> Any:
        return aiohttp.ClientSession()

from .client import AsyncSmartRemoteClient, SevenhugsAuthError, SevenhugsConnectionError
from .const import (
    CONF_AUTO_SYNC_WEBHOOKS,
    CONF_HOST,
    CONF_PORT,
    CONF_TRIGGER_RESYNC,
    DEFAULT_AUTO_SYNC_WEBHOOKS,
    DEFAULT_PORT,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import SevenhugsDataUpdateCoordinator
from .lua_injector import SevenhugsLuaInjector
from .webhook import async_register_webhook, async_unregister_webhook

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "DOMAIN",
    "PLATFORMS",
    "AsyncSmartRemoteClient",
    "SevenhugsAuthError",
    "SevenhugsConnectionError",
    "SevenhugsData",
    "SevenhugsDataUpdateCoordinator",
    "SevenhugsLuaInjector",
    "async_setup",
    "async_setup_entry",
    "async_unload_entry",
    "update_listener",
]


class SevenhugsData(dict):
    """Conteneur des données d'exécution de la télécommande.

    Garantit la compatibilité ascendante et descendante totale (Phases 1, 2, 3 et 4) :
    - Accès attribut direct : runtime_data.client, runtime_data.session, runtime_data.coordinator,
      runtime_data.webhook_id, runtime_data.injector
    - Accès dictionnaire : runtime_data["client"], runtime_data["session"], runtime_data["coordinator"],
      runtime_data["webhook_id"], runtime_data["injector"]
    - Délégation transparente d'attributs proxy vers client (ex. runtime_data.host)
    - Synchronisation bidirectionnelle automatique entre dictionnaire et attributs
    """

    def __init__(
        self,
        client: AsyncSmartRemoteClient,
        session: aiohttp.ClientSession,
        coordinator: Any | None = None,
        webhook_id: str | None = None,
        injector: Any | None = None,
    ) -> None:
        super().__init__({
            "client": client,
            "session": session,
            "coordinator": coordinator,
            "webhook_id": webhook_id,
            "injector": injector,
        })
        self.client = client
        self.session = session
        self.coordinator = coordinator
        self.webhook_id = webhook_id
        self.injector = injector

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        if key in ("client", "session", "coordinator", "webhook_id", "injector"):
            super().__setattr__(key, value)

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name in ("client", "session", "coordinator", "webhook_id", "injector"):
            super().__setitem__(name, value)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            pass

        client = getattr(self, "client", None)
        if client is not None:
            return getattr(client, name)

        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Initialise le composant Sevenhugs Smart Remote (mode déclaratif YAML non supporté, UI uniquement)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Initialise l'intégration Sevenhugs Smart Remote à partir d'une ConfigEntry."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    target_host = f"{host}:{port}" if port and port != 80 else host

    # ClientSession asynchrone dédiée managed par Home Assistant
    session = async_create_clientsession(
        hass,
        verify_ssl=False,
        auto_cleanup=False,
    )

    client = AsyncSmartRemoteClient(
        host=target_host,
        session=session,
        keys=dict(entry.data),
    )

    # Coordinateur de données (Requirement R1 & R4)
    coordinator = SevenhugsDataUpdateCoordinator(hass, client, entry)

    # Injecteur Server-Driven UI Lua (Phase 4 / Requirement R2)
    injector = SevenhugsLuaInjector(hass, entry, client)

    # Double stockage préalable : hass.data (requis par R1) et entry.runtime_data (HA 2024+)
    # Permet à async_unload_entry de nettoyer proprement les ressources si first_refresh échoue
    runtime_data = SevenhugsData(
        client=client,
        session=session,
        coordinator=coordinator,
        injector=injector,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime_data
    if hasattr(entry, "runtime_data"):
        entry.runtime_data = runtime_data

    # Premier rafraîchissement d'état initial, webhook, écouteur d'options, aiguillage des plateformes et sync
    try:
        # Enregistrement du webhook local sécurisé (Phase 4 / Requirement R1)
        webhook_id = async_register_webhook(hass, entry)
        runtime_data.webhook_id = webhook_id
        runtime_data["webhook_id"] = webhook_id

        await coordinator.async_config_entry_first_refresh()
        if not getattr(coordinator, "last_update_success", True):
            exc = getattr(coordinator, "last_exception", None)
            if isinstance(exc, ConfigEntryAuthFailed):
                raise exc
            raise ConfigEntryNotReady(f"Initial refresh failed: {exc}")

        # Écouteur de rechargement lors de la modification des options
        if hasattr(entry, "add_update_listener") and hasattr(entry, "async_on_unload"):
            entry.async_on_unload(entry.add_update_listener(update_listener))

        # Forward entry setups vers PLATFORMS (sensor, binary_sensor, button)
        if PLATFORMS and hasattr(hass, "config_entries") and hasattr(hass.config_entries, "async_forward_entry_setups"):
            await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Synchronisation en arrière-plan non-bloquante des entités vers la télécommande (Phase 4 / R2, R3, R4)
        options = getattr(entry, "options", {}) or {}
        auto_sync = options.get(CONF_AUTO_SYNC_WEBHOOKS, DEFAULT_AUTO_SYNC_WEBHOOKS)
        trigger_resync = options.get(CONF_TRIGGER_RESYNC, False)
        if trigger_resync or auto_sync:
            _LOGGER.info(
                "Planification de la synchronisation des entités en arrière-plan pour %s (trigger_resync=%s, auto_sync=%s)",
                entry.entry_id,
                trigger_resync,
                auto_sync,
            )
            if hasattr(hass, "async_create_task"):
                hass.async_create_task(injector.async_sync_entities())
            else:
                asyncio.create_task(injector.async_sync_entities())

    except Exception:
        await async_unload_entry(hass, entry)
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharge l'intégration et libère proprement le webhook, les plateformes, la session et le socket TCP."""
    # 1. Désenregistrement immédiat du webhook local pour rejeter tout nouvel appel entrant
    async_unregister_webhook(hass, entry)

    # 2. Déchargement ordonné des plateformes
    unload_ok = True
    if PLATFORMS and hasattr(hass, "config_entries") and hasattr(hass.config_entries, "async_unload_platforms"):
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if not unload_ok:
        return False

    # 3. Récupération et nettoyage des descripteurs de connexion et sessions TCP
    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not data and hasattr(entry, "runtime_data"):
        data = getattr(entry, "runtime_data", None)

    if data:
        client = getattr(data, "client", data.get("client") if isinstance(data, dict) else data)
        if client and hasattr(client, "close"):
            await client.close()
        session = getattr(data, "session", data.get("session") if isinstance(data, dict) else None)
        if session and hasattr(session, "close") and not getattr(session, "closed", False):
            await session.close()

    # 4. Réinitialisation explicite de runtime_data
    if hasattr(entry, "runtime_data"):
        entry.runtime_data = None

    # 5. Nettoyage de l'espace de domaine si vide
    if DOMAIN in hass.data and not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN, None)

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Met à jour les options et recharge la configuration avec re-synchronisation."""
    if hasattr(hass, "config_entries") and hasattr(hass.config_entries, "async_reload"):
        await hass.config_entries.async_reload(entry.entry_id)
        return

    # Repli de secours sans reload : déclenchement direct de la synchronisation si disponible
    data_map = getattr(hass, "data", None)
    if isinstance(data_map, dict):
        runtime_data = data_map.get(DOMAIN, {}).get(entry.entry_id)
        if runtime_data:
            injector = getattr(runtime_data, "injector", None) or (
                runtime_data.get("injector") if isinstance(runtime_data, dict) else None
            )
            if injector and hasattr(injector, "async_sync_entities"):
                if hasattr(hass, "async_create_task"):
                    hass.async_create_task(injector.async_sync_entities())
                else:
                    asyncio.create_task(injector.async_sync_entities())
