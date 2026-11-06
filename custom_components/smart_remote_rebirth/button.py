"""Plateforme Button pour l'intégration Sevenhugs Smart Remote (SR1A).

Fournit les boutons d'action diagnostique :
- SevenhugsForceSyncButton : Forcer la synchronisation immédiate de la télémétrie.
- SevenhugsPingButton : Émettre un ping /api/hello et rafraîchir la connexion.
- SevenhugsBackupConfigButton : Télécharger un dump de configuration via /api/config.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import SevenhugsAuthError, SevenhugsConnectionError
from .const import DOMAIN
from .coordinator import SevenhugsDataUpdateCoordinator
from .entity import SevenhugsBaseEntity

_LOGGER = logging.getLogger(__name__)

# Désactive les mises à jour en parallèle pour les entités coordinateur/boutons.
PARALLEL_UPDATES = 0

# Erreurs réseau et système interceptées lors des actions de boutons
_COMMUNICATION_ERRORS = (
    SevenhugsConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
)


def _resolve_coordinator(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> SevenhugsDataUpdateCoordinator | None:
    """Extrait de manière sécurisée l'instance du coordinateur."""
    # 1. Vérification dans entry.runtime_data (HA 2024+)
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is not None:
        if isinstance(runtime_data, SevenhugsDataUpdateCoordinator):
            return runtime_data
        coord = getattr(runtime_data, "coordinator", None)
        if coord is not None:
            return coord
        if isinstance(runtime_data, dict) and "coordinator" in runtime_data and runtime_data["coordinator"] is not None:
            return runtime_data["coordinator"]

    # 2. Vérification dans hass.data[DOMAIN][entry.entry_id] (compatibilité standard)
    domain_data = getattr(hass, "data", {}).get(DOMAIN, {}) if hasattr(hass, "data") and isinstance(hass.data, dict) else {}
    if isinstance(domain_data, dict) and entry.entry_id in domain_data:
        stored_data = domain_data[entry.entry_id]
        if isinstance(stored_data, SevenhugsDataUpdateCoordinator):
            return stored_data
        coord = getattr(stored_data, "coordinator", None)
        if coord is not None:
            return coord
        if isinstance(stored_data, dict) and "coordinator" in stored_data and stored_data["coordinator"] is not None:
            return stored_data["coordinator"]

    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Initialise la plateforme button à partir d'une ConfigEntry.

    Récupère le coordinateur via double stockage (entry.runtime_data ou hass.data)
    et enregistre l'ensemble des boutons de diagnostic.
    """
    coordinator: SevenhugsDataUpdateCoordinator | None = _resolve_coordinator(hass, entry)

    if coordinator is None:
        _LOGGER.error(
            "Impossible d'initialiser la plateforme button pour %s : coordinateur Sevenhugs introuvable",
            entry.title or entry.entry_id,
        )
        return

    entities: list[ButtonEntity] = [
        SevenhugsForceSyncButton(coordinator, entry),
        SevenhugsPingButton(coordinator, entry),
        SevenhugsBackupConfigButton(coordinator, entry),
    ]

    async_add_entities(entities)


class SevenhugsForceSyncButton(SevenhugsBaseEntity, ButtonEntity):
    """Bouton pour forcer la synchronisation immédiate de la télémétrie."""

    _attr_entity_category: EntityCategory = EntityCategory.DIAGNOSTIC
    _attr_icon: str = "mdi:sync"

    def __init__(
        self,
        coordinator: SevenhugsDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise le bouton de synchronisation forcée."""
        super().__init__(coordinator, entry, "force_sync")

    async def async_press(self) -> None:
        """Exécute une mise à jour immédiate des données de la télécommande."""
        _LOGGER.debug("Demande de synchronisation forcée pour %s", self.entity_id or self.name)
        try:
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error(
                "Erreur lors de la synchronisation forcée de %s : %s",
                self.entity_id or self.name,
                err,
            )
            raise HomeAssistantError(
                f"Échec de la synchronisation forcée Sevenhugs : {err}"
            ) from err


class SevenhugsPingButton(SevenhugsBaseEntity, ButtonEntity):
    """Bouton pour tester la connectivité réseau et réveiller la télécommande."""

    _attr_entity_category: EntityCategory = EntityCategory.DIAGNOSTIC
    _attr_icon: str = "mdi:network-outline"

    def __init__(
        self,
        coordinator: SevenhugsDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise le bouton ping / reconnexion."""
        super().__init__(coordinator, entry, "ping")

    async def async_press(self) -> None:
        """Envoie une requête /api/hello et rafraîchit le coordinateur."""
        _LOGGER.debug("Émission du ping vers la télécommande (%s)", self.entry.title)
        try:
            # 1. Vérification de connectivité via l'endpoint ultra-léger /api/hello
            await self.client.async_request("GET", "/api/hello")
            # 2. Rafraîchissement des données de télémétrie
            await self.coordinator.async_request_refresh()
            _LOGGER.debug("Ping et rafraîchissement réussis pour %s", self.entry.title)
        except SevenhugsAuthError as err:
            _LOGGER.error("Session non authentifiée lors du ping sur %s : %s", self.entry.title, err)
            raise HomeAssistantError(
                f"Échec d'authentification lors du ping Sevenhugs : {err}"
            ) from err
        except _COMMUNICATION_ERRORS as err:
            _LOGGER.warning("Télécommande inaccessible lors du ping (%s) : %s", self.entry.title, err)
            raise HomeAssistantError(
                f"Impossible de joindre la télécommande Sevenhugs lors du ping : {err}"
            ) from err
        except Exception as err:
            _LOGGER.error("Erreur inattendue lors du ping de %s : %s", self.entry.title, err, exc_info=True)
            raise HomeAssistantError(
                f"Erreur inattendue lors du ping Sevenhugs : {err}"
            ) from err


class SevenhugsBackupConfigButton(SevenhugsBaseEntity, ButtonEntity):
    """Bouton pour sauvegarder et inspecter la configuration de la télécommande."""

    _attr_entity_category: EntityCategory = EntityCategory.DIAGNOSTIC
    _attr_icon: str = "mdi:cloud-download"

    def __init__(
        self,
        coordinator: SevenhugsDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise le bouton de sauvegarde de configuration."""
        super().__init__(coordinator, entry, "backup_config")
        self._last_backup: dict[str, Any] | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose les métadonnées de la dernière sauvegarde effectuée."""
        attrs: dict[str, Any] = {}
        if self._last_backup is not None:
            attrs["last_backup_sections"] = list(self._last_backup.keys())
            attrs["last_backup_size"] = len(self._last_backup)
        return attrs

    async def async_press(self) -> None:
        """Récupère la configuration complète de la télécommande via /api/config."""
        _LOGGER.debug("Récupération de la configuration pour %s", self.entry.title)
        try:
            config_data = await self.client.async_request("GET", "/api/config")
            self._last_backup = config_data if isinstance(config_data, dict) else {}
            _LOGGER.info(
                "Sauvegarde de configuration Sevenhugs réussie pour %s (%d sections)",
                self.entry.title,
                len(self._last_backup),
            )
        except SevenhugsAuthError as err:
            _LOGGER.error("Session non authentifiée lors du backup sur %s : %s", self.entry.title, err)
            raise HomeAssistantError(
                f"Échec d'authentification lors de la sauvegarde Sevenhugs : {err}"
            ) from err
        except _COMMUNICATION_ERRORS as err:
            _LOGGER.warning("Télécommande inaccessible lors du backup (%s) : %s", self.entry.title, err)
            raise HomeAssistantError(
                f"Impossible de joindre la télécommande Sevenhugs pour sauvegarder la configuration : {err}"
            ) from err
        except Exception as err:
            _LOGGER.error("Erreur inattendue lors de la sauvegarde de %s : %s", self.entry.title, err, exc_info=True)
            raise HomeAssistantError(
                f"Erreur inattendue lors de la sauvegarde Sevenhugs : {err}"
            ) from err
