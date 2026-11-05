"""Entité de base pour l'intégration Sevenhugs Smart Remote (SR1A).

Fournit SevenhugsBaseEntity qui gère :
- L'héritage de CoordinatorEntity[SevenhugsDataUpdateCoordinator]
- La conformité avec la règle has_entity_name = True (Bronze standard Home Assistant)
- Le rattachement au Device Registry de Home Assistant (DeviceInfo)
- La génération d'un identifiant unique immuable (_attr_unique_id)
- La synchronisation de l'état de disponibilité avec la résilience de veille du coordinateur
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import AsyncSmartRemoteClient
from .const import (
    CONF_REMOTE_ID,
    CONF_SERIAL_NUMBER,
    DEFAULT_NAME,
    DOMAIN,
    HARDWARE_VERSION,
    MANUFACTURER,
    MODEL_SR1A,
)
from .coordinator import SevenhugsDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class SevenhugsBaseEntity(CoordinatorEntity[SevenhugsDataUpdateCoordinator]):
    """Classe de base pour toutes les entités Sevenhugs Smart Remote.

    Hérite de CoordinatorEntity pour synchroniser automatiquement les entités
    avec les cycles de rafraîchissement et la résilience de veille de
    SevenhugsDataUpdateCoordinator.
    """

    _attr_has_entity_name: bool = True
    _attr_should_poll: bool = False

    def __init__(
        self,
        coordinator: SevenhugsDataUpdateCoordinator,
        entry: ConfigEntry,
        entity_key: str,
        description: Any = None,
    ) -> None:
        """Initialise l'entité de base Sevenhugs.

        Args:
            coordinator: Coordinateur de données SevenhugsDataUpdateCoordinator.
            entry: ConfigEntry associée à cette instance de télécommande.
            entity_key: Clé distinctive de l'entité (ex: 'battery', 'charging').
            description: Description typée optionnelle (EntityDescription).
        """
        super().__init__(coordinator)
        self.entry = entry
        self.entity_key = entity_key
        if description is not None:
            self.entity_description = description

        # Récupération sécurisée des attributs matériels
        serial_number: str = (
            getattr(coordinator, "serial_number", None)
            or entry.data.get(CONF_SERIAL_NUMBER, entry.entry_id)
        )
        remote_id: str = (
            getattr(coordinator, "remote_id", None)
            or entry.data.get(CONF_REMOTE_ID, serial_number)
        )
        firmware_version: str = (
            getattr(coordinator, "firmware_version", None)
            or entry.data.get("firmware_version", "0.76.4")
        )
        mac_address: str | None = (
            getattr(coordinator, "mac_address", None)
            or entry.data.get("mac_address")
        )

        # Identifiant unique de l'entité
        self._attr_unique_id = f"{serial_number}_{entity_key}"

        # Clé de traduction pour localisation multilingue automatique
        self._attr_translation_key = entity_key

        # Liaisons réseau pour le Device Registry
        connections: set[tuple[str, str]] = set()
        if mac_address:
            connections.add((CONNECTION_NETWORK_MAC, mac_address))

        # Rattachement au Device Registry Home Assistant
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial_number)},
            connections=connections,
            manufacturer=MANUFACTURER,
            model=MODEL_SR1A,
            name=f"{DEFAULT_NAME} ({remote_id})",
            sw_version=firmware_version,
            hw_version=HARDWARE_VERSION,
        )

    @property
    def available(self) -> bool:
        """Indique si l'entité est disponible.

        Prend en compte à la fois le statut de mise à jour du coordinateur
        (qui reste True pendant les échecs de veille tolérés) et l'attribut
        _attr_available de l'entité.
        """
        return bool(self.coordinator.last_update_success and self._attr_available)

    @property
    def client(self) -> AsyncSmartRemoteClient:
        """Renvoie le client Smart Remote associé au coordinateur."""
        return self.coordinator.client

    @property
    def telemetry_data(self) -> dict[str, Any]:
        """Renvoie les données de télémétrie actuelles en cache ou un dictionnaire vide."""
        return self.coordinator.data or {}
