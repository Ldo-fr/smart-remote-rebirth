"""Composant Home Assistant Sevenhugs Smart Remote (SR1A).

Phase 2 : Initialisation, cycle de vie et gestion du composant.
"""

from __future__ import annotations

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
    from homeassistant.helpers.aiohttp_client import async_create_clientsession
except ImportError:
    ConfigEntry = Any  # type: ignore[misc,assignment]
    HomeAssistant = Any  # type: ignore[misc,assignment]

    def async_create_clientsession(*args: Any, **kwargs: Any) -> Any:
        connector = kwargs.get("connector")
        return aiohttp.ClientSession(connector=connector)

from .client import AsyncSmartRemoteClient, SevenhugsAuthError, SevenhugsConnectionError
from .const import CONF_HOST, CONF_PORT, DEFAULT_PORT, DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "DOMAIN",
    "AsyncSmartRemoteClient",
    "SevenhugsAuthError",
    "SevenhugsConnectionError",
    "async_setup_entry",
    "async_unload_entry",
]


class SevenhugsData(dict):
    """Conteneur des données d'exécution de la télécommande."""

    def __init__(self, client: AsyncSmartRemoteClient, session: aiohttp.ClientSession) -> None:
        super().__init__({"client": client, "session": session})
        self.client = client
        self.session = session

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            return getattr(self.client, name)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Initialise l'intégration Sevenhugs Smart Remote à partir d'une ConfigEntry."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    target_host = f"{host}:{port}" if port and port != 80 else host

    # Socket TCP Keep-Alive exclusive avec limitation stricte à 1 socket active
    connector = aiohttp.TCPConnector(
        force_close=False,
        limit=1,
        limit_per_host=1,
        enable_cleanup_closed=True,
        keepalive_timeout=60.0,
    )
    session = async_create_clientsession(
        hass,
        verify_ssl=False,
        auto_cleanup=False,
        connector=connector,
    )

    client = AsyncSmartRemoteClient(
        host=target_host,
        session=session,
        keys=dict(entry.data),
    )

    # Double stockage : hass.data (requis par R1) et entry.runtime_data (HA 2024+)
    runtime_data = SevenhugsData(client=client, session=session)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime_data
    if hasattr(entry, "runtime_data"):
        entry.runtime_data = runtime_data

    # Écouteur de rechargement lors de la modification des options
    if hasattr(entry, "add_update_listener") and hasattr(entry, "async_on_unload"):
        entry.async_on_unload(entry.add_update_listener(update_listener))

    # Forward platform setups si supporté par l'instance Home Assistant (Phase 3)
    if PLATFORMS and hasattr(hass, "config_entries") and hasattr(hass.config_entries, "async_forward_entry_setups"):
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharge l'intégration et libère proprement la session et le socket TCP."""
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

    if hasattr(entry, "runtime_data"):
        entry.runtime_data = None

    if DOMAIN in hass.data and not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN, None)

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Met à jour les options et recharge la configuration."""
    if hasattr(hass, "config_entries") and hasattr(hass.config_entries, "async_reload"):
        await hass.config_entries.async_reload(entry.entry_id)
